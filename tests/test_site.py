"""Dashboard builder tests against synthetic fixtures (tests/fixtures/site_*.json).

The fixture set follows docs/CONTRACTS.md exactly. Crafted facts the tests rely on:
- Timeline day 1 ranks Bob/Ann/Cal; day 2 (current) ranks Ann/Bob/Cal, so the
  movement column must read Ann ▲1, Bob ▼1, Cal –.
- Ann's MEX is out at R16; Cal's BRA is out in the groups.
- Cal's best_possible (8) is below the leader's points (9): mathematically eliminated.
- Cal drafted unknown code "XXX" to exercise graceful degradation.
"""

import json
import re
from pathlib import Path

from wcpool.site import build_site, main

FIXTURES = Path(__file__).parent / "fixtures"
FILES = ("standings", "pool", "teams", "matches")


def _build(tmp_path, **mutators):
    """Copy fixtures into tmp_path/data (applying per-file mutators), build, return HTML."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in FILES:
        obj = json.loads((FIXTURES / f"site_{name}.json").read_text(encoding="utf-8"))
        if name in mutators:
            mutators[name](obj)
        (data_dir / f"{name}.json").write_text(json.dumps(obj), encoding="utf-8")
    out_path = build_site(data_dir, tmp_path / "site")
    assert out_path == tmp_path / "site" / "index.html"
    return out_path.read_text(encoding="utf-8")


def _line_with(html, marker):
    (line,) = [ln for ln in html.splitlines() if marker in ln]
    return line


def test_meta_tags_present(tmp_path):
    html = _build(tmp_path)
    assert '<meta name="robots" content="noindex">' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html


def test_one_polyline_per_player(tmp_path):
    html = _build(tmp_path)
    assert html.count("<polyline") == 3
    # Each polyline gets a distinct color, and every player labels their own line.
    colors = re.findall(r'<polyline[^>]*stroke="(#[0-9a-f]{6})"', html)
    assert len(set(colors)) == 3
    for name in ("Ann", "Bob", "Cal"):
        assert re.search(rf"<text[^>]*>{name}</text>", html)


def test_movement_arrows(tmp_path):
    html = _build(tmp_path)
    assert "▲1" in _line_with(html, 'class="standing" data-player="Ann"')
    assert "▼1" in _line_with(html, 'class="standing" data-player="Bob"')
    assert '<span class="movement same">–</span>' in _line_with(
        html, 'class="standing" data-player="Cal"'
    )


def test_movement_dash_with_single_timeline_day(tmp_path):
    def keep_first_day(standings):
        standings["timeline"] = standings["timeline"][:1]

    html = _build(tmp_path, standings=keep_first_day)
    assert html.count('<span class="movement same">–</span>') == 3
    assert "▲" not in html and "▼" not in html


def test_eliminated_team_muted_with_stage(tmp_path):
    html = _build(tmp_path)
    mex = _line_with(html, 'data-team="MEX"')
    assert 'class="team out"' in mex
    assert "out — R16" in mex
    bra = _line_with(html, 'data-team="BRA"')
    assert 'class="team out"' in bra
    assert "out — Groups" in bra
    # Alive teams are not muted.
    assert 'class="team"' in _line_with(html, 'data-team="FRA"')
    # The muting is visible: strikethrough + grey styling exists for .team.out.
    assert "line-through" in html


def test_unknown_team_code_degrades_gracefully(tmp_path):
    html = _build(tmp_path)
    xxx = _line_with(html, 'data-team="XXX"')
    assert ">XXX<" in xxx  # falls back to the raw code as the display name


def test_mathematically_eliminated_badge(tmp_path):
    html = _build(tmp_path)
    assert html.count("mathematically eliminated") == 1
    assert "mathematically eliminated" in _line_with(html, 'class="bp" data-player="Cal"')


def test_no_badge_when_best_possible_ties_leader(tmp_path):
    def cal_can_tie(standings):
        standings["players"][2]["best_possible"] = 9  # equal to leader: still alive

    html = _build(tmp_path, standings=cal_can_tie)
    assert "mathematically eliminated" not in html


def test_placeholder_banner_toggles(tmp_path):
    html = _build(tmp_path)
    assert "draft pending" not in html

    def placeholder_on(pool):
        pool["placeholder"] = True

    # build into a fresh tmp subdir to avoid clashing with the first build
    sub = tmp_path / "ph"
    sub.mkdir()
    html = _build(sub, pool=placeholder_on)
    assert "Placeholder picks — draft pending" in html


def test_empty_timeline_pre_tournament(tmp_path):
    def pre_tournament(standings):
        standings["timeline"] = []

    html = _build(tmp_path, standings=pre_tournament)
    assert "<polyline" not in html
    assert "No matches yet" in html


def test_third_place_scoring_line_only_when_enabled(tmp_path):
    html = _build(tmp_path)
    assert "Third-place final win" not in html

    def toggle_on(pool):
        pool["third_place_final"] = True

    sub = tmp_path / "tp"
    sub.mkdir()
    html = _build(sub, pool=toggle_on)
    assert "Third-place final win: 4 pts" in html


def test_footer_generated_at_and_scoring_summary(tmp_path):
    html = _build(tmp_path)
    assert "2026-06-13T04:31:00Z" in html
    assert "Group stage: win 3 pts, draw 1 pt" in html
    assert "Advance to knockouts: 3 pts" in html
    assert "R32 4" in html and "Final 14" in html
    assert "1 of 2 matches played" in html


def test_cli_main(tmp_path, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in FILES:
        (data_dir / f"{name}.json").write_text(
            (FIXTURES / f"site_{name}.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
    out_dir = tmp_path / "site"
    main(["--data-dir", str(data_dir), "--out", str(out_dir)])
    assert (out_dir / "index.html").exists()
    assert str(out_dir / "index.html") in capsys.readouterr().out


def test_matches_json_optional(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ("standings", "pool", "teams"):
        (data_dir / f"{name}.json").write_text(
            (FIXTURES / f"site_{name}.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
    html = build_site(data_dir, tmp_path / "site").read_text(encoding="utf-8")
    assert "matches played" not in html
    assert '<meta name="robots" content="noindex">' in html
