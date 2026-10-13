from pathlib import Path

import pytest

import notifier
from notifier import CLOSED, FACILITY, RESTRICTED, Config, Notice, ParseError

# Real copy of https://www.nps.gov/blri/planyourvisit/roadclosures.htm
REAL_PAGE = (Path(__file__).parent / "fixtures" / "roadclosures-2026-10-13.html").read_text()


def cfg(ranges=((355, 443),), keywords=()):
    return Config(area_name="Test", ranges=list(ranges), keywords=list(keywords), url="u")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Closed from milepost 95.9 to 106.", [(95.9, 106.0)]),
        ("between milepost 106 and milepost 121", [(106.0, 121.0)]),
        ("CLOSED: MP 63.5 - 63.9 around the bridge", [(63.5, 63.9)]),
        ("Otter Creek Restaurant (Milepost 60.8) is closed.", [(60.8, 60.8)]),
        ("No mileposts here, just 2025-10-13", []),
        ("MP 500 is not on the Parkway", []),
    ],
)
def test_extract_ranges(text, expected):
    assert notifier.extract_ranges(text) == expected


def test_real_page_reads_every_section_table():
    page = notifier.parse_page(REAL_PAGE)
    assert page.status_as_of == "Road status as of 7:35 A.M, Tuesday, October 13, 2026"
    by_mp = {n.milepost: n for n in page.notices}
    assert by_mp["76.4 – 85.9"].kind == CLOSED  # Peaks of Otter, VA table
    assert by_mp["344.2 – 355.3"].kind == CLOSED  # Mount Mitchell, NC table
    assert by_mp["120.3"].place == "Roanoke Mountain Loop"
    # Open section whose notes contain a closure, matched on the note's mileposts too
    james_river = by_mp["61.4 – 66.3"]
    assert james_river.kind == RESTRICTED and (63.5, 63.9) in james_river.ranges
    assert any(n.kind == FACILITY and "Otter Creek" in n.place for n in page.notices)


def test_real_page_skips_open_and_ungated_sections():
    page = notifier.parse_page(REAL_PAGE)
    mps = {n.milepost for n in page.notices}
    assert "0 – 2.2" not in mps  # Open
    assert "2.2 – 4.4" not in mps  # Ungated
    assert "469.1" not in mps  # "Check with the Smokies for their road closure information"


def test_real_page_filtered_to_area():
    got = notifier.notices_for_area(notifier.parse_page(REAL_PAGE), cfg())
    assert [(n.kind, n.milepost) for n in got] == [
        (CLOSED, "344.2 – 355.3"),
        (CLOSED, "367.6"),
        (RESTRICTED, "388.8 – 393.6"),
    ]


def test_keywords_match_facilities_without_milepost():
    got = notifier.notices_for_area(notifier.parse_page(REAL_PAGE), cfg(ranges=[], keywords=["linn cove"]))
    assert [n.place for n in got] == ["Linn Cove Visitor Center is temporarily closed."]


def test_layout_change_is_an_error_not_silent_all_clear():
    with pytest.raises(ParseError):
        notifier.parse_page("<html><body><p>Blue Ridge Parkway</p></body></html>")


def _road(mp, status="Closed", notes=""):
    lo, _, hi = mp.partition("-")
    return Notice(CLOSED if status == "Closed" else RESTRICTED, mp, f"Place {mp}", status, notes,
                  [(float(lo), float(hi or lo))])


def test_diff_new_changed_cleared(tmp_path: Path):
    state = tmp_path / "state.json"
    a, b = _road("360-365"), _road("370-375")
    notifier.save_state(state, [a, b])
    a_reopened_partly = _road("360-365", status="Open with single lane closures")
    c = _road("380-381")
    changes = notifier.diff(notifier.load_state(state), [a_reopened_partly, c])
    assert [n.milepost for n in changes.new] == ["380-381"]
    assert [(o.status, n.status) for o, n in changes.updated] == [("Closed", "Open with single lane closures")]
    assert [n.milepost for n in changes.cleared] == ["370-375"]
    assert not notifier.diff(notifier.load_state(state), [a, b])


def test_old_state_format_counts_as_first_run(tmp_path: Path):
    state = tmp_path / "state.json"
    state.write_text('{"notices": {"abc": "some text"}}')
    assert notifier.load_state(state) is None


def test_html_email_escapes_and_labels():
    page = notifier.Page(notices=[], status_as_of="as of now")
    n = _road("360-365", notes="<script>x</script>")
    changes = notifier.Changes(new=[n], updated=[], cleared=[_road("370-375")])
    html = notifier.build_html(cfg(), page, changes, False, [n])
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "NEW" in html and "Reopened / no longer listed" in html


def _run(tmp_path, monkeypatch, page_html, extra=()):
    page = tmp_path / "page.html"
    page.write_text(page_html)
    conf = tmp_path / "config.toml"
    conf.write_text('[area]\nname = "Asheville"\nranges = [[355, 443]]\n')
    sent = []
    monkeypatch.setattr(notifier, "send_email", lambda s, t, h: sent.append((s, t, h)))
    rc = notifier.main(["--config", str(conf), "--state", str(tmp_path / "state.json"), "--html", str(page), *extra])
    return rc, sent


def test_main_dry_run_first_run(tmp_path: Path, monkeypatch, capsys):
    rc, sent = _run(tmp_path, monkeypatch, REAL_PAGE, ["--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and not sent
    assert "BRP alerts on for Asheville: 2 road closure(s), 1 other notice(s)" in out
    assert "NC Highway 80 to Mount Mitchell State Park" in out
    assert not (tmp_path / "state.json").exists()


def test_main_sends_once_then_on_change(tmp_path: Path, monkeypatch):
    rc, sent = _run(tmp_path, monkeypatch, REAL_PAGE)
    assert rc == 0 and len(sent) == 1
    rc, sent = _run(tmp_path, monkeypatch, REAL_PAGE)
    assert rc == 0 and not sent  # unchanged page: no email

    # Stoney Fork Road reopens: its status goes Closed -> Open and the note goes away.
    stoney = (
        '<p>USFS Road 63 (Stoney Fork Access Rd)</p>\n</td>\n<td style="text-align: center;">\n'
        '<p style="text-align: left;">Closed </p>\n</td>\n<td>\n<p>Until Further Notice (USFS Gate)</p>'
    )
    assert stoney in REAL_PAGE
    reopened = REAL_PAGE.replace(stoney, stoney.replace("Closed ", "Open").replace("Until Further Notice (USFS Gate)", ""))
    rc, sent = _run(tmp_path, monkeypatch, reopened)
    assert rc == 0 and len(sent) == 1
    subject, text, html = sent[0]
    assert subject == "BRP reopened: MP 367.6 (Asheville)"
    assert "REOPENED / NO LONGER LISTED\n- MP 367.6 | USFS Road 63" in text


def test_welcome_resends_summary_even_without_changes(tmp_path: Path, monkeypatch):
    _run(tmp_path, monkeypatch, REAL_PAGE)
    rc, sent = _run(tmp_path, monkeypatch, REAL_PAGE, ["--welcome"])
    assert rc == 0 and len(sent) == 1
    subject, text, html = sent[0]
    assert subject.startswith("BRP alerts on for Asheville")
    assert "Alerts are on" in html and "NC Highway 80 to Mount Mitchell State Park" in text
