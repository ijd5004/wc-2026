"""Guards the 2022 regression fixture itself: totals recomputed with the 2022 weights
must match the final spreadsheet standings. The scoring engine (issue #1) replays this
same fixture through its real code path."""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "wc2022.json"


def test_wc2022_fixture_totals():
    fx = json.loads(FIXTURE.read_text())
    scoring = fx["scoring"]
    for player in fx["players"]:
        total = 0
        for t in player["teams"]:
            total += sum(
                scoring["group_win"] if r == "W" else scoring["group_draw"] if r == "D" else 0
                for r in t["group_results"]
            )
            if t["advanced"]:
                total += scoring["advance"]
            total += sum(scoring["stage_win_points"][s] for s in t["ko_wins"])
        assert total == fx["expected_totals"][player["name"]], player["name"]


def test_wc2022_fixture_shape():
    fx = json.loads(FIXTURE.read_text())
    assert len(fx["players"]) == 5
    for player in fx["players"]:
        assert len(player["teams"]) == 5
        for t in player["teams"]:
            assert len(t["group_results"]) == 3
            assert t["eliminated_at"] in {"GROUP", "R16", "QF", "SF", "FINAL", None}
            assert not t["alive"]
