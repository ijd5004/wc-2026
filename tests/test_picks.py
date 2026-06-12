import json
from pathlib import Path

import pytest

from wcpool.picks import PoolValidationError, load_pool

REPO_POOL = Path(__file__).parent.parent / "data" / "pool.json"

TEAMS = {c: {"name": c, "flag": "🏳️"} for c in ["FRA", "ARG", "BRA", "ENG", "ESP", "GER"]}


def valid_pool():
    return {
        "pool_name": "Test",
        "third_place_final": False,
        "scoring": {
            "group_win": 3,
            "group_draw": 1,
            "advance": 3,
            "stage_win_points": {"R32": 4, "R16": 6, "QF": 8, "SF": 10, "FINAL": 14},
            "third_place_win": 4,
        },
        "players": [
            {"name": "A", "teams": ["FRA", "ARG"]},
            {"name": "B", "teams": ["BRA", "ENG"]},
        ],
    }


def write(tmp_path, pool):
    p = tmp_path / "pool.json"
    p.write_text(json.dumps(pool))
    return p


def test_valid_pool_loads(tmp_path):
    pool = load_pool(write(tmp_path, valid_pool()), TEAMS)
    assert pool["pool_name"] == "Test"


def test_duplicate_team_across_players(tmp_path):
    bad = valid_pool()
    bad["players"][1]["teams"] = ["FRA", "ENG"]
    with pytest.raises(PoolValidationError, match="drafted twice"):
        load_pool(write(tmp_path, bad), TEAMS)


def test_unknown_code_rejected_only_with_teams(tmp_path):
    bad = valid_pool()
    bad["players"][0]["teams"] = ["FRA", "XYZ"]
    with pytest.raises(PoolValidationError, match="unknown team code XYZ"):
        load_pool(write(tmp_path, bad), TEAMS)
    load_pool(write(tmp_path, bad), teams=None)  # structural-only mode passes


def test_duplicate_player_name(tmp_path):
    bad = valid_pool()
    bad["players"][1]["name"] = "A"
    bad["players"][1]["teams"] = ["BRA", "ENG"]
    with pytest.raises(PoolValidationError, match="duplicate player name"):
        load_pool(write(tmp_path, bad), TEAMS)


def test_unequal_team_counts(tmp_path):
    bad = valid_pool()
    bad["players"][1]["teams"] = ["BRA"]
    with pytest.raises(PoolValidationError, match="unequal team counts"):
        load_pool(write(tmp_path, bad), TEAMS)


def test_missing_scoring_key(tmp_path):
    bad = valid_pool()
    del bad["scoring"]["advance"]
    with pytest.raises(PoolValidationError, match="scoring missing keys: advance"):
        load_pool(write(tmp_path, bad), TEAMS)


def test_all_problems_reported_at_once(tmp_path):
    bad = valid_pool()
    bad["players"][1]["name"] = "A"
    bad["players"][1]["teams"] = ["FRA"]
    with pytest.raises(PoolValidationError) as e:
        load_pool(write(tmp_path, bad), TEAMS)
    assert len(e.value.problems) >= 3  # dup name, dup team, unequal counts


def test_repo_pool_is_structurally_valid():
    pool = load_pool(REPO_POOL, teams=None)
    assert pool["placeholder"] is False
    assert pool["third_place_final"] is False
    assert pool["scoring"]["stage_win_points"]["FINAL"] == 14
    assert len(pool["players"]) == 5
    assert all(len(p["teams"]) == 6 for p in pool["players"])

def test_repo_pool_codes_resolve_in_repo_teams_json():
    """Every drafted code must exist in data/teams.json (catches draft typos in CI)."""
    teams = json.loads((REPO_POOL.parent / "teams.json").read_text())
    pool = load_pool(REPO_POOL, teams)
    for player in pool["players"]:
        for code in player["teams"]:
            assert teams[code]["flag"] != "🏳️", f"{code} has fallback flag"
