"""Watch the Blue Ridge Parkway road closures page and email when closures
in your section of the Parkway change.

Usage:
    python notifier.py                 # fetch, compare, notify, save state
    python notifier.py --dry-run       # print what would be sent; don't email or save
    python notifier.py --html page.htm # parse a saved copy of the page instead of fetching
    python notifier.py --preview out.html  # also write the HTML email to a file
    python notifier.py --welcome       # send the "alerts are on" summary even if nothing changed
"""

from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import os
import re
import smtplib
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

PARKWAY_MAX_MILEPOST = 469.1
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 parkway-notifications"
)

NUM = r"(\d{1,3}(?:\.\d+)?)(?!\.?\d)"
CONNECTOR = r"\s*(?:-|–|—|to|through|thru|and)\s*"
MP_WORD = r"(?:mile\s*posts?|mileposts?|MPs?|MM)\.?\s*"
# "Milepost 95.9 to 106", "MP 0.0-2.2", "between milepost 106 and milepost 121"
KEYWORD_RANGE_RE = re.compile(
    rf"\b{MP_WORD}{NUM}(?:{CONNECTOR}(?:{MP_WORD})?{NUM}\b)?", re.IGNORECASE
)
# Bare "344.2 - 355.3" in the table's milepost column.
BARE_RANGE_RE = re.compile(rf"{NUM}(?:{CONNECTOR}{NUM})?")
# Words in a row's status/notes that mean some kind of closure or restriction.
# "road closure information" (the Smokies row) is a pointer, not a closure.
RESTRICTION_RE = re.compile(
    r"\bclosed\b|\bclosures?\b(?! information)|prohibited|single[- ]lane|traffic control|detour",
    re.IGNORECASE,
)
STATUS_AS_OF_RE = re.compile(r"road status as of", re.IGNORECASE)

# Severity order, most serious first.
CLOSED, RESTRICTED, FACILITY = "closed", "restricted", "facility"
KIND_LABELS = {
    CLOSED: "Road closed",
    RESTRICTED: "Open with restrictions",
    FACILITY: "Facility / trail closure",
}


class ParseError(Exception):
    """The page didn't look like the closures page we know how to read."""


@dataclass
class Notice:
    kind: str
    milepost: str  # as displayed, e.g. "344.2 – 355.3"
    place: str  # crossroads / location, or the full item text for facilities
    status: str = ""
    notes: str = ""
    ranges: list[tuple[float, float]] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Identifies the section/item, so a status change reads as an update."""
        ident = f"{self.kind if self.kind == FACILITY else 'road'}|{self.milepost}|{self.place}"
        return hashlib.sha256(ident.lower().encode()).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        return f"{self.kind}|{self.status}|{self.notes}".lower()

    @classmethod
    def from_dict(cls, d: dict) -> "Notice":
        d = dict(d)
        d["ranges"] = [tuple(r) for r in d.get("ranges", [])]
        return cls(**d)


@dataclass
class Page:
    notices: list[Notice]  # every closure/restriction on the page, any milepost
    status_as_of: str = ""


@dataclass
class Config:
    area_name: str
    ranges: list[tuple[float, float]]
    keywords: list[str]
    url: str

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = tomllib.loads(path.read_text())
        area = data.get("area", {})
        ranges = [tuple(sorted((float(a), float(b)))) for a, b in area.get("ranges", [])]
        keywords = [k for k in data.get("keywords", area.get("keywords", [])) if k.strip()]
        if not ranges and not keywords:
            raise SystemExit(f"{path}: set at least one milepost range or keyword under [area]")
        return cls(
            area_name=area.get("name", "your area"),
            ranges=ranges,
            keywords=keywords,
            url=data.get("source", {}).get(
                "url", "https://www.nps.gov/blri/planyourvisit/roadclosures.htm"
            ),
        )


# --------------------------------------------------------------------------- parsing


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace("*", "").strip()


def _valid_range(a: str, b: str | None) -> tuple[float, float] | None:
    lo, hi = float(a), float(b) if b else float(a)
    lo, hi = min(lo, hi), max(lo, hi)
    if hi > PARKWAY_MAX_MILEPOST:
        return None
    return (lo, hi)


def extract_ranges(text: str, pattern: re.Pattern = KEYWORD_RANGE_RE) -> list[tuple[float, float]]:
    found = []
    for m in pattern.finditer(text):
        r = _valid_range(m.group(1), m.group(2))
        if r and r not in found:
            found.append(r)
    return found


def _format_milepost(ranges: list[tuple[float, float]]) -> str:
    def num(x: float) -> str:
        return f"{x:g}"

    return ", ".join(num(lo) if lo == hi else f"{num(lo)} – {num(hi)}" for lo, hi in ranges)


def _columns(header_cells: list[str]) -> dict[str, int] | None:
    """Map the closures table's header row to column indexes."""
    cols: dict[str, int] = {}
    for i, text in enumerate(header_cells):
        t = text.lower()
        if "milepost" in t and "milepost" not in cols:
            cols["milepost"] = i
        elif "crossroad" in t or "location" in t:
            cols["place"] = i
        elif "status" in t:
            cols["status"] = i
        elif "information" in t or "notes" in t or "update" in t:
            cols["notes"] = i
    return cols if {"milepost", "status"} <= cols.keys() else None


def _cells(row: Tag) -> list[str]:
    return [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"], recursive=False)]


def _parse_road_tables(root: Tag) -> list[Notice]:
    notices = []
    # Only innermost tables: the page is wrapped in a layout table.
    for table in root.find_all("table"):
        if table.find("table"):
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        cols = _columns(_cells(rows[0]))
        if not cols:
            continue
        for row in rows[1:]:
            cells = _cells(row)
            if len(cells) <= cols["status"]:
                continue

            def get(name: str) -> str:
                i = cols.get(name)
                return cells[i] if i is not None and i < len(cells) else ""

            ranges = extract_ranges(get("milepost"), BARE_RANGE_RE)
            if not ranges:
                continue
            status, notes = get("status"), get("notes")
            if "closed" in status.lower():
                kind = CLOSED
            elif RESTRICTION_RE.search(f"{status} {notes}"):
                kind = RESTRICTED
            else:
                continue  # open / ungated with nothing noteworthy
            # Closures noted inside an open section ("CLOSED: MP 63.5 - 63.9")
            # are matched on their own mileposts too.
            ranges += [r for r in extract_ranges(notes) if r not in ranges]
            notices.append(
                Notice(
                    kind=kind,
                    milepost=_format_milepost(ranges[:1]),
                    place=get("place"),
                    status=status,
                    notes=notes,
                    ranges=ranges,
                )
            )
    return notices


def _parse_facility_lists(root: Tag) -> list[Notice]:
    """List items under "Facility Closures" / "Trail and Backcountry Closures"."""
    notices = []
    for heading in root.find_all(["h2", "h3", "h4"]):
        if "closure" not in heading.get_text().lower():
            continue
        for sib in heading.find_next_siblings():
            if sib.name in ("h2", "h3", "h4"):
                break
            items = sib.find_all("li") if sib.name != "li" else [sib]
            for li in items:
                text = _clean(li.get_text(" "))
                if not text:
                    continue
                ranges = extract_ranges(text)
                notices.append(
                    Notice(
                        kind=FACILITY,
                        milepost=_format_milepost(ranges),
                        place=text,
                        ranges=ranges,
                    )
                )
    return notices


def parse_page(html: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        junk.decompose()
    root = soup.find(id="main") or soup.find("main") or soup.body or soup

    if "parkway" not in root.get_text(" ").lower():
        raise ParseError("Fetched page doesn't mention the Parkway; the URL may have changed.")
    has_table = any(
        _columns(_cells(t.find("tr"))) for t in root.find_all("table") if t.find("tr")
    )
    if not has_table:
        raise ParseError(
            "Couldn't find the closures table (columns 'Parkway Mileposts by Section' and "
            "'Status'). The page layout has probably changed; check it and update the parser."
        )

    as_of = root.find(string=STATUS_AS_OF_RE)
    return Page(
        notices=_parse_road_tables(root) + _parse_facility_lists(root),
        status_as_of=_clean(as_of.parent.get_text(" ")).rstrip(".") if as_of else "",
    )


def relevant(notice: Notice, config: Config) -> bool:
    for lo, hi in notice.ranges:
        for alo, ahi in config.ranges:
            if lo <= ahi and alo <= hi:
                return True
    text = f"{notice.place} {notice.notes}".lower()
    return any(k.lower() in text for k in config.keywords)


def notices_for_area(page: Page, config: Config) -> list[Notice]:
    order = [CLOSED, RESTRICTED, FACILITY]
    matched = [n for n in page.notices if relevant(n, config)]
    return sorted(matched, key=lambda n: (order.index(n.kind), n.ranges[0] if n.ranges else (999, 999)))


# --------------------------------------------------------------------------- state & diff


STATE_VERSION = 2


def load_state(path: Path) -> dict[str, Notice] | None:
    """Saved notices by key, or None on first run (or an older state format)."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get("version") != STATE_VERSION:
        return None
    return {k: Notice.from_dict(v) for k, v in data["notices"].items()}


def save_state(path: Path, notices: list[Notice]) -> None:
    data = {"version": STATE_VERSION, "notices": {n.key: asdict(n) for n in notices}}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


@dataclass
class Changes:
    new: list[Notice]
    updated: list[tuple[Notice, Notice]]  # (old, new)
    cleared: list[Notice]

    def __bool__(self) -> bool:
        return bool(self.new or self.updated or self.cleared)


def diff(previous: dict[str, Notice] | None, current: list[Notice]) -> Changes:
    old = previous or {}
    cur = {n.key: n for n in current}
    return Changes(
        new=[n for k, n in cur.items() if k not in old],
        updated=[(old[k], n) for k, n in cur.items() if k in old and old[k].fingerprint != n.fingerprint],
        cleared=[n for k, n in old.items() if k not in cur],
    )


# --------------------------------------------------------------------------- email


def _subject(config: Config, changes: Changes, first_run: bool, current: list[Notice]) -> str:
    area = config.area_name
    if first_run:
        closed = sum(n.kind == CLOSED for n in current)
        return f"BRP alerts on for {area}: {closed} road closure(s), {len(current) - closed} other notice(s)"
    new_closed = [n for n in changes.new if n.kind == CLOSED] + [
        n for _, n in changes.updated if n.kind == CLOSED
    ]
    if new_closed:
        return f"BRP CLOSED: MP {new_closed[0].milepost} ({area})"
    if changes.cleared and not changes.new and not changes.updated:
        return f"BRP reopened: MP {changes.cleared[0].milepost or changes.cleared[0].place[:40]} ({area})"
    return f"BRP update for {area}"


def _notice_line(n: Notice) -> str:
    if n.kind == FACILITY:
        return n.place
    parts = [f"MP {n.milepost}", n.place, n.status]
    line = " | ".join(p for p in parts if p)
    return f"{line}\n    {n.notes}" if n.notes else line


def build_text(config: Config, page: Page, changes: Changes, first_run: bool, current: list[Notice]) -> str:
    lines = [f"Blue Ridge Parkway: {config.area_name}"]
    if page.status_as_of:
        lines.append(page.status_as_of)
    lines.append("")

    def section(title: str, items: list[str]) -> None:
        if items:
            lines.append(title.upper())
            lines.extend(f"- {i}" for i in items)
            lines.append("")

    if not first_run:
        section("New", [_notice_line(n) for n in changes.new])
        section("Changed", [f"{_notice_line(n)}\n    (was: {o.status or 'listed'})" for o, n in changes.updated])
        section("Reopened / no longer listed", [_notice_line(n) for n in changes.cleared])
    if current:
        section("Everything currently listed for your area", [_notice_line(n) for n in current])
    else:
        lines += ["No closures are listed for your area right now.", ""]
    lines.append(config.url)
    return "\n".join(lines)


# Colors per kind: (accent, badge background, badge text)
KIND_COLORS = {
    CLOSED: ("#b91c1c", "#fee2e2", "#991b1b"),
    RESTRICTED: ("#b45309", "#fef3c7", "#92400e"),
    FACILITY: ("#475569", "#e2e8f0", "#334155"),
    "reopened": ("#15803d", "#dcfce7", "#166534"),
}


def _card(n: Notice, tag: str = "", was: str = "", reopened: bool = False) -> str:
    e = htmllib.escape
    accent, badge_bg, badge_fg = KIND_COLORS["reopened" if reopened else n.kind]
    label = "Reopened / no longer listed" if reopened else KIND_LABELS[n.kind]
    if n.kind == FACILITY:
        title = e(n.place)
        sub = f"Milepost {e(n.milepost)}" if n.milepost else ""
    else:
        title = f"Milepost {e(n.milepost)}"
        sub = e(n.place)
    tag_html = (
        f'<span style="font-size:11px;font-weight:700;letter-spacing:.04em;color:#1d4ed8;'
        f'margin-left:6px">{e(tag)}</span>'
        if tag
        else ""
    )
    status = (
        f'<div style="margin-top:6px;font-size:14px;color:#111827"><b>Status:</b> {e(n.status)}'
        + (f' <span style="color:#6b7280">(was: {e(was)})</span>' if was else "")
        + "</div>"
        if n.status and not reopened
        else ""
    )
    notes = (
        f'<div style="margin-top:6px;font-size:14px;line-height:1.45;color:#374151">{e(n.notes)}</div>'
        if n.notes and not reopened
        else ""
    )
    return f"""
<tr><td style="padding:0 0 12px 0">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:separate;background:#ffffff;border:1px solid #e5e7eb;border-left:5px solid {accent};border-radius:8px">
    <tr><td style="padding:12px 14px">
      <span style="display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;background:{badge_bg};color:{badge_fg};padding:2px 8px;border-radius:10px">{label}</span>{tag_html}
      <div style="margin-top:8px;font-size:17px;font-weight:700;color:#111827">{title}</div>
      {f'<div style="margin-top:2px;font-size:14px;color:#4b5563">{sub}</div>' if sub else ""}
      {status}{notes}
    </td></tr>
  </table>
</td></tr>"""


def build_html(config: Config, page: Page, changes: Changes, first_run: bool, current: list[Notice]) -> str:
    e = htmllib.escape
    blocks: list[str] = []

    def heading(text: str) -> None:
        blocks.append(
            f'<tr><td style="padding:14px 0 8px 0;font-size:13px;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:.06em;color:#6b7280">{e(text)}</td></tr>'
        )

    if first_run:
        intro = "Alerts are on. Here's everything currently listed for your section of the Parkway. From now on you'll only hear from this when something changes."
    else:
        intro = "The road status for your section of the Parkway changed."
    if not first_run and changes:
        heading("What changed")
        blocks += [_card(n, tag="NEW") for n in changes.new]
        blocks += [_card(n, tag="UPDATED", was=o.status) for o, n in changes.updated]
        blocks += [_card(n, reopened=True) for n in changes.cleared]

    changed_keys = {n.key for n in changes.new} | {n.key for _, n in changes.updated}
    rest = current if first_run else [n for n in current if n.key not in changed_keys]
    if rest:
        heading("Currently listed for your area" if first_run else "Still in effect")
        blocks += [_card(n) for n in rest]
    elif not current:
        blocks.append(
            '<tr><td style="padding:12px 14px;background:#dcfce7;border-radius:8px;color:#166534;'
            'font-size:15px">No closures are listed for your area right now.</td></tr>'
        )

    as_of = f'<div style="margin-top:4px;font-size:13px;color:#d1fae5">{e(page.status_as_of)}</div>' if page.status_as_of else ""
    ranges = ", ".join(f"MP {lo:g}–{hi:g}" for lo, hi in config.ranges)
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6">
<tr><td align="center" style="padding:20px 12px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
    <tr><td style="background:#14532d;border-radius:10px 10px 0 0;padding:18px 20px">
      <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#bbf7d0">Blue Ridge Parkway road status</div>
      <div style="margin-top:4px;font-size:22px;font-weight:700;color:#ffffff">{e(config.area_name)}</div>
      {as_of}
    </td></tr>
    <tr><td style="background:#f9fafb;padding:16px 20px 8px 20px;border:1px solid #e5e7eb;border-top:0;border-radius:0 0 10px 10px">
      <div style="font-size:15px;line-height:1.5;color:#374151">{e(intro)}</div>
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(blocks)}</table>
      <div style="padding:8px 0 16px 0">
        <a href="{e(config.url)}" style="display:inline-block;background:#14532d;color:#ffffff;text-decoration:none;font-weight:600;font-size:14px;padding:10px 16px;border-radius:6px">View the full closures page</a>
      </div>
      <div style="padding-bottom:12px;font-size:12px;color:#9ca3af">Watching {e(ranges)}. Change this in config.toml.</div>
    </td></tr>
  </table>
</td></tr>
</table>
</body></html>"""


def send_email(subject: str, text: str, html: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_PASSWORD"]
    sender = os.environ.get("EMAIL_FROM") or user
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",") if r.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)  # plain text for SMS gateways and text-only clients
    msg.add_alternative(html, subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)


# --------------------------------------------------------------------------- main


def fetch(url: str, attempts: int = 3) -> str:
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException:
            if attempt == attempts:
                raise
            time.sleep(10 * attempt)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", type=Path, default=Path("config.toml"))
    ap.add_argument("--state", type=Path, default=Path("state.json"))
    ap.add_argument("--html", type=Path, help="parse this saved HTML file instead of fetching")
    ap.add_argument("--dry-run", action="store_true", help="print instead of emailing; don't save state")
    ap.add_argument("--preview", type=Path, help="write the HTML email to this file")
    ap.add_argument(
        "--welcome",
        action="store_true",
        help='send the "alerts are on" summary of current closures, e.g. after adding recipients',
    )
    args = ap.parse_args(argv)

    config = Config.load(args.config)
    raw = args.html.read_text() if args.html else fetch(config.url)

    try:
        page = parse_page(raw)
    except ParseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    current = notices_for_area(page, config)
    previous = load_state(args.state)
    first_run = previous is None or args.welcome
    changes = diff(previous, current)

    print(
        f"{len(current)} notice(s) in {config.area_name}; {len(changes.new)} new, "
        f"{len(changes.updated)} changed, {len(changes.cleared)} cleared."
    )
    if not (changes or first_run):
        return 0

    subject = _subject(config, changes, first_run, current)
    text = build_text(config, page, changes, first_run, current)
    html = build_html(config, page, changes, first_run, current)
    if args.preview:
        args.preview.write_text(html)
    if args.dry_run:
        print(f"\nSubject: {subject}\n\n{text}")
        return 0

    send_email(subject, text, html)
    save_state(args.state, current)
    print("Email sent and state saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
