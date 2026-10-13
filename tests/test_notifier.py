# The sample HTML below is synthetic: it mimics the shapes the NPS closures
# page has used (a milepost table, bullet lists, prose), not a captured copy.
from pathlib import Path

import pytest

import notifier
from notifier import Config, Notice, ParseError

TABLE_PAGE = """
<html><body><div id="main">
<h1>Blue Ridge Parkway Road Closures</h1>
<h2>Virginia</h2>
<table>
  <tr><th>Milepost</th><th>Location</th><th>Status</th></tr>
  <tr><td>0.0 - 2.2</td><td>Rockfish Gap to Afton Overlook</td><td>Closed</td></tr>
  <tr><td>95.9 – 106</td><td>Montvale Overlook to US 460</td><td>Closed for construction</td></tr>
</table>
<h2>North Carolina</h2>
<ul>
  <li>Milepost 382 to 393.6: Folk Art Center to NC 191, closed due to storm damage.</li>
  <li>MP 408.6-423.2 Mount Pisgah to NC 215 closed for winter.</li>
</ul>
<p>Craggy Gardens Visitor Center is closed for the season.</p>
<p>Updated 10/13/2025.</p>
</div></body></html>
"""


def cfg(ranges=((380, 394),), keywords=()):
    return Config(area_name="Test", ranges=list(ranges), keywords=list(keywords), url="u")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Closed from milepost 95.9 to 106.", [(95.9, 106.0)]),
        ("between milepost 106 and milepost 121", [(106.0, 121.0)]),
        ("MP 0.0-2.2", [(0.0, 2.2)]),
        ("Mileposts 170.3 – 198.4 including Mabry Mill", [(170.3, 198.4)]),
        ("Gate closed at MP 292.", [(292.0, 292.0)]),
        ("No mileposts here, just 2025-10-13", []),
        ("MP 500 is not on the Parkway", []),
    ],
)
def test_extract_ranges(text, expected):
    assert notifier.extract_ranges(text) == expected


def test_parse_table_list_and_prose():
    notices = notifier.parse_notices(TABLE_PAGE)
    ranges = [n.ranges for n in notices]
    assert [(0.0, 2.2)] in ranges
    assert [(95.9, 106.0)] in ranges
    assert [(382.0, 393.6)] in ranges
    assert [(408.6, 423.2)] in ranges
    assert len(notices) == 4  # header row and the "Updated" date are ignored


def test_filters_to_area():
    got = notifier.relevant_notices_from_page(TABLE_PAGE, cfg())
    assert [n.ranges for n in got] == [[(382.0, 393.6)]]


def test_overlap_at_edges_counts():
    got = notifier.relevant_notices_from_page(TABLE_PAGE, cfg(ranges=[(106, 120)]))
    assert len(got) == 1 and "Montvale" in got[0].text


def test_keywords_match_without_milepost():
    got = notifier.relevant_notices_from_page(TABLE_PAGE, cfg(ranges=[], keywords=["craggy"]))
    assert len(got) == 1 and "Craggy Gardens" in got[0].text


def test_layout_change_is_an_error_not_silent_all_clear():
    with pytest.raises(ParseError):
        notifier.relevant_notices_from_page("<html><body>Blue Ridge Parkway</body></html>", cfg())


def test_explicit_no_closures_is_ok():
    html = "<html><body><p>Blue Ridge Parkway: there are no current closures.</p></body></html>"
    assert notifier.relevant_notices_from_page(html, cfg()) == []


def test_diff_and_state_roundtrip(tmp_path: Path):
    state = tmp_path / "state.json"
    a, b = Notice("MP 382-393 closed"), Notice("MP 390 closed")
    notifier.save_state(state, [a])
    added, removed = notifier.diff(notifier.load_state(state), [b])
    assert added == [b.text] and removed == [a.text]
    assert notifier.diff(notifier.load_state(state), [a]) == ([], [])


def test_main_dry_run_first_run(tmp_path: Path, capsys):
    page = tmp_path / "page.html"
    page.write_text(TABLE_PAGE)
    conf = tmp_path / "config.toml"
    conf.write_text('[area]\nname = "Asheville"\nranges = [[380, 394]]\n')
    state = tmp_path / "state.json"
    rc = notifier.main(["--config", str(conf), "--state", str(state), "--html", str(page), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BRP watch started: 1 notice(s) for Asheville" in out
    assert "Folk Art Center" in out
    assert not state.exists()  # dry run never saves


def test_main_sends_and_saves(tmp_path: Path, monkeypatch):
    page = tmp_path / "page.html"
    page.write_text(TABLE_PAGE)
    conf = tmp_path / "config.toml"
    conf.write_text('[area]\nranges = [[380, 394]]\n')
    state = tmp_path / "state.json"
    sent = []
    monkeypatch.setattr(notifier, "send_email", lambda s, b: sent.append(s))
    args = ["--config", str(conf), "--state", str(state), "--html", str(page)]
    assert notifier.main(args) == 0 and len(sent) == 1 and state.exists()
    assert notifier.main(args) == 0 and len(sent) == 1  # unchanged page: no second email
