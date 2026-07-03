"""Tests for the scoring engine (issue #1).

Includes the binding 2022 regression (replay tests/fixtures/wc2022.json through
score_team with the fixture's own scoring config) plus synthetic 2026 cases
covering the adapter, best_possible, timeline, and ranking.
"""

import json
from pathlib import Path

import pytest

from wcpool.scoring import (
    _bracket_leaves,
    achievements_from_matches,
    compute_standings,
    main,
    score_team,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wc2022.json"

SCORING_2026 = {
    "group_win": 3,
    "group_draw": 1,
    "advance": 3,
    "stage_win_points": {"R32": 4, "R16": 6, "QF": 8, "SF": 10, "FINAL": 14},
    "third_place_win": 4,
}

ALL_STAGE_POINTS = sum(SCORING_2026["stage_win_points"].values())  # 42


def pool(players, third_place_final=False):
    return {
        "pool_name": "Test Pool",
        "third_place_final": third_place_final,
        "scoring": SCORING_2026,
        "players": players,
    }


_ids = iter(range(1, 10_000))


def match(stage, utc_date, home, away, winner="HOME", *, status="FINISHED",
          group=None, decided_by="REGULAR"):
    """Synthetic normalized match. winner: 'HOME', a code, or None (draw/unplayed)."""
    if status != "FINISHED":
        winner = None
    elif winner == "HOME":
        winner = home
    return {
        "id": next(_ids),
        "stage": stage,
        "group": group if stage == "GROUP" else None,
        "utc_date": f"{utc_date}T18:00:00Z",
        "status": status,
        "home": home,
        "away": away,
        "score": None,
        "winner": winner,
        "decided_by": decided_by,
    }


# ---------------------------------------------------------------- 2022 regression


def _fixture():
    return json.loads(FIXTURE.read_text())


@pytest.mark.parametrize("name", ["Ian", "Harry", "Ryan", "Greg", "Bob"])
def test_regression_wc2022_player_totals(name):
    """Replaying the 2022 outcome grid through score_team reproduces the sheet.

    The fixture's config has no R32 — the engine must take stages purely from
    config (Ian 73, Harry 54, Ryan 54, Greg 31, Bob 20).
    """
    fx = _fixture()
    player = next(p for p in fx["players"] if p["name"] == name)
    total = sum(
        score_team(team, fx["scoring"], fx["third_place_final"]) for team in player["teams"]
    )
    assert total == fx["expected_totals"][name]


def test_first_knockout_stage_comes_from_config():
    """With a 2022-style config (no R32), appearing in R16 means advanced."""
    fx = _fixture()
    matches = [
        match("GROUP", "2022-11-22", "ARG", "KSA", "KSA", group="C"),
        match("R16", "2022-12-03", "ARG", "AUS", status="SCHEDULED"),
    ]
    ach = achievements_from_matches(matches, fx["scoring"])
    assert ach["ARG"]["advanced"] is True
    assert ach["KSA"]["advanced"] is False


# ---------------------------------------------------------------- adapter (2026)


def test_third_place_group_qualifier_gets_flat_advance_bonus():
    """A third-place qualifier advances by appearing in an R32 fixture: flat +3."""
    matches = [
        match("GROUP", "2026-06-11", "TPQ", "AAA", "AAA", group="A"),
        match("GROUP", "2026-06-15", "TPQ", "BBB", None, group="A"),
        match("GROUP", "2026-06-19", "TPQ", "CCC", "TPQ", group="A"),
        match("R32", "2026-06-28", "XXX", "TPQ", status="SCHEDULED"),
    ]
    ach = achievements_from_matches(matches, SCORING_2026)["TPQ"]
    assert ach["group_results"] == ["L", "D", "W"]
    assert ach["advanced"] is True
    assert score_team(ach, SCORING_2026, False) == 1 + 3 + 3  # D + W + advance


def test_penalty_shootout_knockout_win_counts_as_win():
    matches = [
        match("R32", "2026-06-28", "PEN", "OPP", "PEN", decided_by="PENALTIES"),
    ]
    ach = achievements_from_matches(matches, SCORING_2026)
    assert ach["PEN"]["ko_wins"] == ["R32"]
    assert score_team(ach["PEN"], SCORING_2026, False) == (
        SCORING_2026["advance"] + SCORING_2026["stage_win_points"]["R32"]
    )
    assert ach["OPP"]["eliminated_at"] == "R32"
    assert ach["OPP"]["alive"] is False


def test_group_draw_scores_group_draw_points():
    matches = [match("GROUP", "2026-06-11", "AAA", "BBB", None, group="B")]
    ach = achievements_from_matches(matches, SCORING_2026)
    assert ach["AAA"]["group_results"] == ["D"]
    assert score_team(ach["AAA"], SCORING_2026, False) == SCORING_2026["group_draw"]
    assert score_team(ach["BBB"], SCORING_2026, False) == SCORING_2026["group_draw"]


def test_eliminated_at_group_only_once_bracket_is_populated():
    group_done = [
        match("GROUP", "2026-06-11", "OUT", "AAA", "AAA", group="A"),
        match("GROUP", "2026-06-15", "OUT", "BBB", "BBB", group="A"),
        match("GROUP", "2026-06-19", "OUT", "CCC", "CCC", group="A"),
    ]
    ach = achievements_from_matches(group_done, SCORING_2026)["OUT"]
    assert ach["eliminated_at"] is None  # undetermined: no R32 fixtures yet
    assert ach["alive"] is True

    # The API publishes the bracket upfront as TBD placeholders (null teams).
    # That must NOT eliminate a team that finished its group games — the bracket
    # is not yet decided. (Regression: this used to cross out every group-
    # completed team the moment the placeholder R32 fixtures appeared.)
    placeholder_r32 = group_done + [
        match("R32", "2026-06-28", None, None, status="SCHEDULED") for _ in range(16)
    ]
    ach = achievements_from_matches(placeholder_r32, SCORING_2026)["OUT"]
    assert ach["eliminated_at"] is None
    assert ach["alive"] is True

    # Once the bracket has real teams and OUT is not among them, it is out.
    with_r32 = group_done + [match("R32", "2026-06-28", "AAA", "BBB", status="SCHEDULED")]
    ach = achievements_from_matches(with_r32, SCORING_2026)["OUT"]
    assert ach["eliminated_at"] == "GROUP"
    assert ach["alive"] is False


def test_partially_populated_bracket_does_not_eliminate_yet():
    """A team is undetermined until every first-round slot is filled (no false
    positives while the API assigns the bracket incrementally)."""
    matches = [
        match("GROUP", "2026-06-11", "OUT", "AAA", "AAA", group="A"),
        match("GROUP", "2026-06-15", "OUT", "BBB", "BBB", group="A"),
        match("GROUP", "2026-06-19", "OUT", "CCC", "CCC", group="A"),
        match("R32", "2026-06-28", "AAA", "BBB", status="SCHEDULED"),  # one slot set
        match("R32", "2026-06-29", None, None, status="SCHEDULED"),  # still TBD
    ]
    ach = achievements_from_matches(matches, SCORING_2026)["OUT"]
    assert ach["eliminated_at"] is None
    assert ach["alive"] is True


def test_advancement_waits_for_full_bracket():
    """A clinched team the API has already slotted into the R32 is NOT shown
    advanced (no chip, no +3 bonus) until every R32 slot has both teams — so all
    qualifiers reveal together instead of trickling in as the API fills the
    bracket. Symmetric with group elimination."""
    won_group = [
        match("GROUP", "2026-06-11", "WIN", "AAA", "WIN", group="A"),
        match("GROUP", "2026-06-15", "WIN", "BBB", "WIN", group="A"),
        match("GROUP", "2026-06-19", "WIN", "CCC", "WIN", group="A"),
    ]
    partial = won_group + [
        match("R32", "2026-06-28", "WIN", None, status="SCHEDULED"),  # placed, opp TBD
        match("R32", "2026-06-29", None, None, status="SCHEDULED"),  # other slot empty
    ]
    ach = achievements_from_matches(partial, SCORING_2026)["WIN"]
    assert ach["advanced"] is False  # withheld until the bracket is fully set
    assert ach["eliminated_at"] is None and ach["alive"] is True
    assert score_team(ach, SCORING_2026, False) == 9  # 3 group wins, no advance yet

    # Fill the rest of the bracket: now the qualifier is credited.
    full = won_group + [match("R32", "2026-06-28", "WIN", "OPP", status="SCHEDULED")]
    ach = achievements_from_matches(full, SCORING_2026)["WIN"]
    assert ach["advanced"] is True
    assert score_team(ach, SCORING_2026, False) == 9 + 3  # group wins + advance


def test_sf_loser_stays_alive_while_bronze_match_pending():
    matches = [
        match("SF", "2026-07-14", "WIN", "SFL", "WIN"),
        match("THIRD_PLACE", "2026-07-18", "SFL", "OTH", status="SCHEDULED"),
    ]
    ach = achievements_from_matches(matches, SCORING_2026)["SFL"]
    assert ach["alive"] is True
    assert ach["eliminated_at"] is None

    played = [
        match("SF", "2026-07-14", "WIN", "SFL", "WIN"),
        match("THIRD_PLACE", "2026-07-18", "SFL", "OTH", "SFL"),
    ]
    ach = achievements_from_matches(played, SCORING_2026)
    assert ach["SFL"]["eliminated_at"] == "SF"  # bronze winner's bracket exit
    assert ach["SFL"]["ko_wins"] == ["THIRD_PLACE"]
    assert ach["OTH"]["eliminated_at"] == "THIRD_PLACE"


# ------------------------------------------------------------ third-place toggle


def _bronze_matches():
    return [
        match("SF", "2026-07-14", "AAA", "BRZ", "AAA"),
        match("SF", "2026-07-15", "BBB", "OTH", "BBB"),
        match("THIRD_PLACE", "2026-07-18", "BRZ", "OTH", "BRZ"),
        match("FINAL", "2026-07-19", "AAA", "BBB", "AAA"),
    ]


def test_third_place_toggle_changes_exactly_the_bronze_points():
    players = [{"name": "Ian", "teams": ["BRZ"]}, {"name": "Bob", "teams": ["OTH"]}]
    off = compute_standings(_bronze_matches(), pool(players, third_place_final=False))
    on = compute_standings(_bronze_matches(), pool(players, third_place_final=True))
    pts_off = {p["name"]: p["points"] for p in off["players"]}
    pts_on = {p["name"]: p["points"] for p in on["players"]}
    assert pts_on["Ian"] - pts_off["Ian"] == SCORING_2026["third_place_win"]
    assert pts_on["Bob"] == pts_off["Bob"]


def test_score_team_third_place_entry_only_scores_when_toggle_on():
    ach = {"group_results": [], "advanced": False, "ko_wins": ["THIRD_PLACE"]}
    assert score_team(ach, SCORING_2026, False) == 0
    assert score_team(ach, SCORING_2026, True) == SCORING_2026["third_place_win"]


# ---------------------------------------------------------------- best_possible


def test_best_possible_mid_group():
    matches = [
        match("GROUP", "2026-06-11", "FRA", "AAA", "FRA", group="D"),
        match("GROUP", "2026-06-15", "FRA", "BBB", status="SCHEDULED", group="D"),
        match("GROUP", "2026-06-19", "FRA", "CCC", status="SCHEDULED", group="D"),
    ]
    out = compute_standings(matches, pool([{"name": "Ian", "teams": ["FRA"]}]))
    ian = out["players"][0]
    assert ian["points"] == 3
    # 1 win banked + 2 remaining group wins + advance + every knockout stage.
    assert ian["best_possible"] == 3 + 2 * 3 + 3 + ALL_STAGE_POINTS


def test_best_possible_mid_knockout():
    matches = [
        match("GROUP", "2026-06-11", "FRA", "AAA", "FRA", group="D"),
        match("GROUP", "2026-06-15", "FRA", "BBB", "FRA", group="D"),
        match("GROUP", "2026-06-19", "FRA", "CCC", "FRA", group="D"),
        match("R32", "2026-06-28", "FRA", "POL", "FRA"),
        match("R16", "2026-07-02", "FRA", "ENG", status="SCHEDULED"),
    ]
    players = [{"name": "Ian", "teams": ["FRA"]}, {"name": "Bob", "teams": ["POL"]}]
    out = compute_standings(matches, pool(players))
    by_name = {p["name"]: p for p in out["players"]}
    ian, bob = by_name["Ian"], by_name["Bob"]
    assert ian["points"] == 3 * 3 + 3 + 4  # group sweep + advance + R32 win
    remaining = sum(SCORING_2026["stage_win_points"][s] for s in ("R16", "QF", "SF", "FINAL"))
    assert ian["best_possible"] == ian["points"] + remaining
    # POL advanced (R32 fixture) but lost it: dead team adds nothing.
    assert bob["points"] == SCORING_2026["advance"]
    assert bob["best_possible"] == bob["points"]
    assert bob["teams"][0]["alive"] is False


def test_best_possible_caps_stage_wins_across_a_players_teams():
    """Only one of a player's teams can win the FINAL and two the SF.

    Three of Ian's teams have swept R32+R16 and sit in the QF. The old naive
    sum credited all three with every remaining stage (3*(8+10+14)); the capped
    bound credits QF three times (cap 4), SF only twice, FINAL only once.
    """
    matches = []
    for team, others in (("FRA", "AAA"), ("BRA", "BBB"), ("ESP", "CCC")):
        matches += [
            match("R32", "2026-06-28", team, others + "1", team),
            match("R16", "2026-07-02", team, others + "2", team),
            match("QF", "2026-07-06", team, others + "3", status="SCHEDULED"),
        ]
    out = compute_standings(
        matches, pool([{"name": "Ian", "teams": ["FRA", "BRA", "ESP"]}])
    )
    ian = out["players"][0]
    sp = SCORING_2026["stage_win_points"]
    capped = 3 * sp["QF"] + 2 * sp["SF"] + 1 * sp["FINAL"]
    assert ian["best_possible"] == ian["points"] + capped


def test_best_possible_cap_counts_already_banked_sf_slot():
    """A banked SF win (a finalist) consumes one of the two SF slots, so only
    one more of the player's teams can be credited an SF win."""
    matches = [
        # WIN is already in the final (swept through SF).
        match("R32", "2026-06-28", "WIN", "A1", "WIN"),
        match("R16", "2026-07-02", "WIN", "A2", "WIN"),
        match("QF", "2026-07-06", "WIN", "A3", "WIN"),
        match("SF", "2026-07-14", "WIN", "SFL", "WIN"),
        # PSG and LYN swept to the QF and are still alive there.
        match("R32", "2026-06-29", "PSG", "B1", "PSG"),
        match("R16", "2026-07-03", "PSG", "B2", "PSG"),
        match("QF", "2026-07-07", "PSG", "B3", status="SCHEDULED"),
        match("R32", "2026-06-30", "LYN", "C1", "LYN"),
        match("R16", "2026-07-04", "LYN", "C2", "LYN"),
        match("QF", "2026-07-08", "LYN", "C3", status="SCHEDULED"),
    ]
    out = compute_standings(
        matches, pool([{"name": "Ian", "teams": ["WIN", "PSG", "LYN"]}])
    )
    ian = out["players"][0]
    sp = SCORING_2026["stage_win_points"]
    # PSG/LYN: QF each (cap 4 -> 2). SF: cap 2 minus WIN's banked slot -> 1 more.
    # FINAL: cap 1, shared across all three -> 1.
    capped = 2 * sp["QF"] + 1 * sp["SF"] + 1 * sp["FINAL"]
    assert ian["best_possible"] == ian["points"] + capped


def test_best_possible_excludes_pending_third_place_match():
    matches = [
        match("SF", "2026-07-14", "WIN", "SFL", "WIN"),
        match("THIRD_PLACE", "2026-07-18", "SFL", "OTH", status="SCHEDULED"),
    ]
    out = compute_standings(matches, pool([{"name": "Ian", "teams": ["SFL"]}]))
    ian = out["players"][0]
    # Alive for the bronze match, but third-place wins never count toward
    # best_possible and the team is out of the title bracket.
    assert ian["teams"][0]["alive"] is True
    assert ian["best_possible"] == ian["points"]


# --------------------------------------------- bracket-aware best_possible


def _full_bracket():
    """Eight tree-ordered R16 pods of four teams, plus each pod's two R32 pairs.

    Team ``P{pod}{a|b|c|d}``: (a,b) is one R32 match, (c,d) the other, and the
    two winners would meet in that pod's R16 — so a,b,c,d all collide at R16.
    """
    pods, pairings = [], []
    for p in range(8):
        a, b, c, d = (f"P{p}{x}" for x in "abcd")
        pods.append([a, b, c, d])
        pairings += [(a, b), (c, d)]
    return pods, pairings


def _bracket_pool(players):
    cfg = pool(players)
    pods, _ = _full_bracket()
    cfg["bracket"] = {"r16_pods": pods}
    return cfg


def _r32_fixtures(winners):
    """All 16 R32 fixtures; a home code in ``winners`` is a FINISHED win."""
    _, pairings = _full_bracket()
    return [
        match("R32", "2026-06-28", home, away, home)
        if home in winners
        else match("R32", "2026-06-28", home, away, status="SCHEDULED")
        for home, away in pairings
    ]


def test_best_possible_bracket_aware_collapses_teams_that_would_meet():
    # P0a and P0c win their R32 games and reach the R16 alive — but they sit in
    # the same R16 pod, so they would MEET there. Only one can bank R16/QF/SF/
    # FINAL; the other's overlapping path must not be double-counted.
    matches = _r32_fixtures({"P0a", "P0c"})
    out = compute_standings(matches, _bracket_pool([{"name": "Ian", "teams": ["P0a", "P0c"]}]))
    ian = out["players"][0]
    sp = SCORING_2026["stage_win_points"]
    assert ian["points"] == 2 * (sp["R32"] + SCORING_2026["advance"])
    collapsed = sp["R16"] + sp["QF"] + sp["SF"] + sp["FINAL"]  # one shared path
    assert ian["best_possible"] == ian["points"] + collapsed


def test_best_possible_bracket_aware_is_tighter_than_global_cap():
    matches = _r32_fixtures({"P0a", "P0c"})
    players = [{"name": "Ian", "teams": ["P0a", "P0c"]}]
    aware = compute_standings(matches, _bracket_pool(players))["players"][0]
    naive = compute_standings(matches, pool(players))["players"][0]
    assert aware["points"] == naive["points"]
    assert aware["best_possible"] < naive["best_possible"]


def test_best_possible_bracket_aware_independent_across_halves():
    # P0a (top half) and P4a (bottom half) can only meet in the FINAL, so every
    # earlier round is genuinely winnable by both — the bracket bound matches
    # the global one, which already caps the FINAL at one champion.
    matches = _r32_fixtures({"P0a", "P4a"})
    players = [{"name": "Ian", "teams": ["P0a", "P4a"]}]
    aware = compute_standings(matches, _bracket_pool(players))["players"][0]
    naive = compute_standings(matches, pool(players))["players"][0]
    assert aware["best_possible"] == naive["best_possible"]
    sp = SCORING_2026["stage_win_points"]
    indep = 2 * (sp["R16"] + sp["QF"] + sp["SF"]) + sp["FINAL"]
    assert aware["best_possible"] == aware["points"] + indep


def test_best_possible_bracket_aware_same_r32_match():
    # P0a and P0b are drawn AGAINST each other in the R32: at most one win
    # between them at every stage, starting with R32 itself.
    matches = _r32_fixtures(set())  # nothing decided yet; both alive
    players = [{"name": "Ian", "teams": ["P0a", "P0b"]}]
    aware = compute_standings(matches, _bracket_pool(players))["players"][0]
    sp = SCORING_2026["stage_win_points"]
    one_path = sp["R32"] + sp["R16"] + sp["QF"] + sp["SF"] + sp["FINAL"]
    # advance bonus is still per team (both are already in the bracket).
    assert aware["best_possible"] == aware["points"] + one_path


def test_bracket_ignored_when_it_does_not_match_fixtures():
    matches = _r32_fixtures({"P0a"})
    players = [{"name": "Ian", "teams": ["P0a", "P0c"]}]
    bad = pool(players)
    bad["bracket"] = {"r16_pods": [["X", "Y", "Z", "W"]]}  # wrong teams and size
    assert _bracket_leaves(matches, SCORING_2026, bad["bracket"]) is None
    # A bracket that fails validation is discarded, not trusted: identical to
    # having supplied no bracket at all.
    assert (
        compute_standings(matches, bad)["players"]
        == compute_standings(matches, pool(players))["players"]
    )


def test_bracket_ignored_before_first_round_is_fully_drawn():
    # Half the R32 slots still TBD -> the bracket is not yet decisive.
    _, pairings = _full_bracket()
    partial = []
    for i, (home, away) in enumerate(pairings):
        if i < 8:
            partial.append(match("R32", "2026-06-28", home, away, status="SCHEDULED"))
        else:
            partial.append(match("R32", "2026-06-28", None, None, status="SCHEDULED"))
    pods, _ = _full_bracket()
    assert _bracket_leaves(partial, SCORING_2026, {"r16_pods": pods}) is None


# -------------------------------------------------------------------- timeline


def test_timeline_cumulative_with_advance_on_final_group_match_date():
    matches = [
        match("GROUP", "2026-06-11", "FRA", "DEN", "FRA", group="D"),
        match("GROUP", "2026-06-12", "FRA", "TUN", None, group="D"),
        match("GROUP", "2026-06-14", "FRA", "AUS", "FRA", group="D"),
        match("R32", "2026-06-20", "FRA", "POL", "FRA"),
    ]
    players = [{"name": "Ian", "teams": ["FRA"]}, {"name": "Bob", "teams": ["DEN"]}]
    out = compute_standings(matches, pool(players))
    assert out["timeline"] == [
        {"date": "2026-06-11", "totals": {"Ian": 3, "Bob": 0}},
        {"date": "2026-06-12", "totals": {"Ian": 4, "Bob": 0}},
        # final group match day: W (+3) and the advance bonus (+3) accrue here
        {"date": "2026-06-14", "totals": {"Ian": 10, "Bob": 0}},
        {"date": "2026-06-20", "totals": {"Ian": 14, "Bob": 0}},
    ]
    ian = next(p for p in out["players"] if p["name"] == "Ian")
    assert out["timeline"][-1]["totals"]["Ian"] == ian["points"]


# ------------------------------------------------------------------------ ranks


def test_tied_players_share_rank():
    matches = [
        match("GROUP", "2026-06-11", "AAA", "CCC", "AAA", group="A"),
        match("GROUP", "2026-06-11", "BBB", "DDD", "BBB", group="B"),
    ]
    players = [
        {"name": "P1", "teams": ["AAA"]},
        {"name": "P2", "teams": ["BBB"]},
        {"name": "P3", "teams": ["CCC"]},
    ]
    out = compute_standings(matches, pool(players))
    ranks = {p["name"]: p["rank"] for p in out["players"]}
    assert ranks == {"P1": 1, "P2": 1, "P3": 3}
    assert [p["name"] for p in out["players"]] == ["P1", "P2", "P3"]


# --------------------------------------------------------------------------- CLI


def test_cli_writes_standings_json(tmp_path):
    matches_doc = {
        "fetched_at": "2026-06-12T04:30:00Z",
        "competition": "WC",
        "matches": [match("GROUP", "2026-06-11", "MEX", "RSA", "MEX", group="A")],
    }
    pool_doc = pool([{"name": "Ian", "teams": ["MEX"]}, {"name": "Bob", "teams": ["RSA"]}])
    matches_path = tmp_path / "matches.json"
    pool_path = tmp_path / "pool.json"
    out_path = tmp_path / "standings.json"
    matches_path.write_text(json.dumps(matches_doc))
    pool_path.write_text(json.dumps(pool_doc))

    main([str(matches_path), str(pool_path), str(out_path)])

    standings = json.loads(out_path.read_text())
    assert set(standings) == {"generated_at", "players", "timeline"}
    by_name = {p["name"]: p for p in standings["players"]}
    assert by_name["Ian"]["points"] == SCORING_2026["group_win"]
    assert by_name["Ian"]["rank"] == 1
    assert by_name["Bob"]["rank"] == 2
    assert standings["timeline"] == [{"date": "2026-06-11", "totals": {"Ian": 3, "Bob": 0}}]
