"""Tests for the share-card renderer (issue #5). No network, no real data dir."""

import json
from pathlib import Path

import pytest
from PIL import Image, ImageStat

from wcpool.card import HEIGHT, WIDTH, main, matchday_label, movement_markers, pick_hook

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def standings():
    return _load("card_standings.json")


@pytest.fixture
def data_dir(tmp_path, standings):
    """A data dir wired up from the card_* fixtures."""
    (tmp_path / "standings.json").write_text(json.dumps(standings))
    (tmp_path / "pool.json").write_text((FIXTURES / "card_pool.json").read_text())
    (tmp_path / "teams.json").write_text((FIXTURES / "card_teams.json").read_text())
    return tmp_path


def _render(data_dir, tmp_path, name="card.png"):
    out = tmp_path / "site" / name
    assert main(["--data-dir", str(data_dir), "--out", str(out)]) == 0
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_creates_png_of_exact_size(data_dir, tmp_path):
    out = _render(data_dir, tmp_path)
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(out) as img:
        assert img.format == "PNG"
        assert img.size == (WIDTH, HEIGHT)
        assert (WIDTH, HEIGHT) == (1080, 1350)


def test_render_is_not_blank(data_dir, tmp_path):
    out = _render(data_dir, tmp_path)
    with Image.open(out) as img:
        variance = ImageStat.Stat(img.convert("L")).var[0]
    assert variance > 0


def test_placeholder_watermark_path_runs(data_dir, tmp_path):
    pool = json.loads((data_dir / "pool.json").read_text())
    pool["placeholder"] = True
    (data_dir / "pool.json").write_text(json.dumps(pool))
    out = _render(data_dir, tmp_path, "card_placeholder.png")
    with Image.open(out) as img:
        assert img.size == (1080, 1350)
        assert ImageStat.Stat(img.convert("L")).var[0] > 0


def test_pre_tournament_empty_timeline_renders(data_dir, tmp_path, standings):
    standings["timeline"] = []
    (data_dir / "standings.json").write_text(json.dumps(standings))
    out = _render(data_dir, tmp_path, "card_pre.png")
    with Image.open(out) as img:
        assert img.size == (1080, 1350)


# ---------------------------------------------------------------------------
# Header label + movement markers (pure helpers)
# ---------------------------------------------------------------------------


def test_matchday_label(standings):
    assert matchday_label(standings) == "Matchday 2026-06-12"
    assert matchday_label({"timeline": []}) == "Pre-tournament"


def test_movement_markers_ascii_only(standings):
    markers = movement_markers(standings)
    # Day 1 order: Bob, Ian, Carol -> day 2 order: Ian, Bob, Carol.
    assert markers == {"Ian": "+1", "Bob": "-1", "Carol": "="}
    assert all(m.isascii() for m in markers.values())


def test_movement_markers_without_history(standings):
    standings["timeline"] = standings["timeline"][:1]
    assert set(movement_markers(standings).values()) == {"="}


# ---------------------------------------------------------------------------
# pick_hook
# ---------------------------------------------------------------------------


def _player(name, points, best, rank):
    return {"name": name, "points": points, "best_possible": best, "rank": rank, "teams": []}


def test_hook_lead_change_takes_top_priority(standings):
    # Fixture also contains an eliminated player (Carol), but the lead change
    # on the latest day must win.
    assert pick_hook(standings) == "Ian takes the lead!"


def test_hook_lead_change_tie_break_prefers_best_rank():
    standings = {
        "players": [
            _player("Ann", 10, 30, 1),
            _player("Ben", 10, 30, 2),
            _player("Cal", 9, 30, 3),
        ],
        "timeline": [
            {"date": "2026-06-11", "totals": {"Ann": 1, "Ben": 1, "Cal": 5}},
            {"date": "2026-06-12", "totals": {"Ann": 10, "Ben": 10, "Cal": 9}},
        ],
    }
    assert pick_hook(standings) == "Ann takes the lead!"


def test_hook_no_lead_change_when_leader_holds():
    standings = {
        "players": [_player("Ann", 10, 30, 1), _player("Ben", 4, 30, 2)],
        "timeline": [
            {"date": "2026-06-11", "totals": {"Ann": 5, "Ben": 1}},
            {"date": "2026-06-12", "totals": {"Ann": 10, "Ben": 4}},
        ],
    }
    assert pick_hook(standings) == "Ben trails by 6"


def test_hook_elimination_beats_gap():
    standings = {
        "players": [
            _player("Ann", 20, 40, 1),
            _player("Ben", 19, 40, 2),
            _player("Cal", 5, 15, 3),  # best_possible < leader's points
        ],
        "timeline": [{"date": "2026-06-12", "totals": {"Ann": 20, "Ben": 19, "Cal": 5}}],
    }
    assert pick_hook(standings) == "Cal is mathematically eliminated"


def test_hook_elimination_tie_break_prefers_best_rank():
    standings = {
        "players": [
            _player("Ann", 20, 40, 1),
            _player("Ben", 10, 15, 2),
            _player("Cal", 5, 12, 3),
        ],
        "timeline": [],
    }
    assert pick_hook(standings) == "Ben is mathematically eliminated"


def test_hook_closest_gap():
    standings = {
        "players": [
            _player("Ann", 20, 40, 1),
            _player("Ben", 12, 40, 2),
            _player("Cal", 10, 40, 3),
        ],
        "timeline": [],
    }
    assert pick_hook(standings) == "Cal trails by 2"


def test_hook_closest_gap_tie_break_prefers_topmost_pair():
    standings = {
        "players": [
            _player("Ann", 20, 40, 1),
            _player("Ben", 17, 40, 2),
            _player("Cal", 14, 40, 3),
        ],
        "timeline": [],
    }
    assert pick_hook(standings) == "Ben trails by 3"


def test_hook_empty_timeline_skips_lead_change(standings):
    standings["timeline"] = []
    # Falls through to elimination: Carol's best (60) < leader's points (73).
    assert pick_hook(standings) == "Carol is mathematically eliminated"


def test_hook_degenerate_pools():
    assert pick_hook({"players": [], "timeline": []}) == "The tournament awaits"
    assert pick_hook({"players": [_player("Solo", 3, 9, 1)], "timeline": []}) == (
        "Solo leads the pool"
    )
