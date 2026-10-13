"""Watch the Blue Ridge Parkway road closures page and email when closures
in your section of the Parkway change.

Usage:
    python notifier.py                 # fetch, compare, notify, save state
    python notifier.py --dry-run       # print what would be sent; don't email or save
    python notifier.py --html page.htm # parse a saved copy of the page instead of fetching
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import smtplib
import sys
import time
import tomllib
from dataclasses import dataclass, field
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
# "Milepost 95.9 to 106", "MP 0.0-2.2", "mileposts 66.3 – 76.4", "between milepost 106 and milepost 121"
KEYWORD_RANGE_RE = re.compile(
    rf"\b(?:mile\s*posts?|mileposts?|MPs?|MM)\.?\s*{NUM}"
    rf"(?:{CONNECTOR}(?:(?:mile\s*posts?|mileposts?|MPs?|MM)\.?\s*)?{NUM}\b)?",
    re.IGNORECASE,
)
# Bare "0.0 - 2.2" — only used inside a table column whose header says milepost.
BARE_RANGE_RE = re.compile(rf"{NUM}(?:{CONNECTOR}{NUM})?")
MILEPOST_HEADER_RE = re.compile(r"mile\s*post|\bMP\b", re.IGNORECASE)
NO_CLOSURES_RE = re.compile(
    r"no (?:current )?(?:road )?closures|(?:entire|whole) parkway is (?:currently )?open",
    re.IGNORECASE,
)


class ParseError(Exception):
    """The page didn't look like the closures page we know how to read."""


@dataclass
class Notice:
    text: str
    ranges: list[tuple[float, float]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return hashlib.sha256(self.text.lower().encode()).hexdigest()[:16]


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
    return re.sub(r"\s+", " ", text).strip()


def _valid_range(a: str, b: str | None) -> tuple[float, float] | None:
    lo, hi = float(a), float(b) if b else float(a)
    lo, hi = min(lo, hi), max(lo, hi)
    if hi > PARKWAY_MAX_MILEPOST:
        return None
    return (lo, hi)


def extract_ranges(text: str) -> list[tuple[float, float]]:
    found = []
    for m in KEYWORD_RANGE_RE.finditer(text):
        r = _valid_range(m.group(1), m.group(2))
        if r:
            found.append(r)
    return found


def _milepost_column(table: Tag) -> int | None:
    header_row = table.find("tr")
    if not header_row:
        return None
    for i, cell in enumerate(header_row.find_all(["th", "td"])):
        if MILEPOST_HEADER_RE.search(cell.get_text(" ")):
            return i
    return None


def _table_row_ranges(row: Tag) -> list[tuple[float, float]]:
    table = row.find_parent("table")
    col = _milepost_column(table) if table else None
    if col is None:
        return []
    cells = row.find_all(["td", "th"], recursive=False)
    if col >= len(cells) or cells[col].name == "th" and row is table.find("tr"):
        return []
    found = []
    for m in BARE_RANGE_RE.finditer(cells[col].get_text(" ")):
        r = _valid_range(m.group(1), m.group(2))
        if r:
            found.append(r)
    return found


def parse_notices(html: str) -> list[Notice]:
    """Split the page into text blocks (table rows, list items, paragraphs)
    and return the ones that mention a milepost."""
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        junk.decompose()
    root = soup.find(id="main") or soup.find("main") or soup.body or soup

    notices: list[Notice] = []
    seen: set[str] = set()
    for block in root.find_all(["tr", "li", "p", "h3", "h4"]):
        # Don't double count a <p> that lives inside a <li> or table row.
        if block.find_parent(["tr", "li"]):
            continue
        text = _clean(block.get_text(" "))
        if not text:
            continue
        ranges = extract_ranges(text)
        if block.name == "tr":
            ranges += [r for r in _table_row_ranges(block) if r not in ranges]
        if not ranges:
            continue
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        notices.append(Notice(text=text, ranges=ranges))
    return notices


def check_page_sanity(html: str, notices: list[Notice]) -> None:
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    if "parkway" not in text.lower():
        raise ParseError("Fetched page doesn't mention the Parkway; the URL may have changed.")
    if not notices and not NO_CLOSURES_RE.search(text):
        raise ParseError(
            "Found no milepost references on the closures page. The page layout may "
            "have changed; check it manually and update the parser."
        )


def relevant(notice: Notice, config: Config) -> bool:
    for lo, hi in notice.ranges:
        for alo, ahi in config.ranges:
            if lo <= ahi and alo <= hi:
                return True
    lowered = notice.text.lower()
    return any(k.lower() in lowered for k in config.keywords)


def relevant_notices_from_page(html: str, config: Config) -> list[Notice]:
    """Notices in the configured area. Keyword-only matches need no milepost,
    so they're searched for across all blocks."""
    notices = parse_notices(html)
    check_page_sanity(html, notices)
    matched = [n for n in notices if relevant(n, config)]
    if config.keywords:
        soup = BeautifulSoup(html, "html.parser")
        root = soup.find(id="main") or soup.find("main") or soup.body or soup
        have = {n.text.lower() for n in matched}
        for block in root.find_all(["tr", "li", "p"]):
            if block.find_parent(["tr", "li"]):
                continue
            text = _clean(block.get_text(" "))
            if text and text.lower() not in have and relevant(Notice(text), config):
                matched.append(Notice(text=text))
                have.add(text.lower())
    return matched


# --------------------------------------------------------------------------- state & diff


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_state(path: Path, notices: list[Notice]) -> None:
    items = {n.key: n.text for n in notices}
    path.write_text(json.dumps({"notices": items}, indent=2, sort_keys=True) + "\n")


def diff(previous: dict | None, current: list[Notice]) -> tuple[list[str], list[str]]:
    old = (previous or {}).get("notices", {})
    cur = {n.key: n.text for n in current}
    added = [cur[k] for k in cur if k not in old]
    removed = [old[k] for k in old if k not in cur]
    return added, removed


# --------------------------------------------------------------------------- email


def build_message(
    config: Config, added: list[str], removed: list[str], first_run: bool, current_count: int
) -> tuple[str, str]:
    if first_run:
        subject = f"BRP watch started: {current_count} notice(s) for {config.area_name}"
    elif added and not removed:
        subject = f"BRP: new closure notice for {config.area_name}"
    elif removed and not added:
        subject = f"BRP: closure notice lifted for {config.area_name}"
    else:
        subject = f"BRP: closures changed for {config.area_name}"

    lines = []
    if added:
        lines.append("Currently listed:" if first_run else "NEW / UPDATED:")
        lines += [f"- {t}" for t in added]
    if first_run and not added:
        lines.append("No closures currently listed for your area.")
    if removed:
        if lines:
            lines.append("")
        lines.append("NO LONGER LISTED (likely reopened):")
        lines += [f"- {t}" for t in removed]
    lines += ["", config.url]
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
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
    msg.set_content(body)

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
    args = ap.parse_args(argv)

    config = Config.load(args.config)
    html = args.html.read_text() if args.html else fetch(config.url)

    try:
        notices = relevant_notices_from_page(html, config)
    except ParseError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    previous = load_state(args.state)
    added, removed = diff(previous, notices)
    first_run = previous is None

    print(f"{len(notices)} notice(s) in {config.area_name}; {len(added)} new, {len(removed)} removed.")
    if not (added or removed or first_run):
        return 0

    subject, body = build_message(config, added, removed, first_run, len(notices))
    if args.dry_run:
        print(f"\nSubject: {subject}\n\n{body}")
        return 0

    send_email(subject, body)
    save_state(args.state, notices)
    print("Email sent and state saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
