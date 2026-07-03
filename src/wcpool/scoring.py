"""Scoring engine for the prediction pool (issue #1).

Three layers, all config-driven (no hardcoded stage lists or point values):

1. ``score_team`` — pure function: one team's achievements dict -> points.
2. ``achievements_from_matches`` — adapter: normalized ``matches.json`` list ->
   per-team achievements dicts (schema in docs/CONTRACTS.md).
3. ``compute_standings`` — full ``standings.json`` structure (ranked players,
   per-team detail, naive ``best_possible`` upper bound, daily timeline).

Knockout stages are taken from ``scoring.stage_win_points`` key order (e.g.
``R32 -> R16 -> QF -> SF -> FINAL`` for 2026, no ``R32`` for the 2022 replay).
``THIRD_PLACE`` is special-cased: it never appears in ``stage_win_points``; a
``THIRD_PLACE`` entry in ``ko_wins`` scores ``third_place_win`` only when the
pool's ``third_place_final`` toggle is on.

Documented decisions (see also docs/CONTRACTS.md):

- ``alive`` means "could still play another match". An SF loser stays alive
  (``eliminated_at`` null) while a third-place fixture exists in the data and
  is unplayed; after the bronze match, the loser is eliminated at
  ``THIRD_PLACE`` and the winner at ``SF`` (its bracket exit). The champion
  ends with ``eliminated_at`` null and ``alive`` false once the FINAL is
  finished.
- ``best_possible`` is a per-player upper bound. Group wins and the advance
  bonus are counted independently per team, but knockout stage wins are capped
  by how many of the player's own teams could win each stage: only one champion
  (FINAL), two finalists (SF), four semifinalists (QF), and so on — halving each
  round back from the final. Already-banked stage wins consume those slots too.
  When a ``bracket`` is supplied (see ``_bracket_leaves``) the cap becomes fully
  bracket-aware: two of a player's teams sitting in the same knockout sub-tree
  can win a given stage at most once *between them* (they would have to meet),
  so each stage counts distinct occupied sub-trees rather than raw team counts.
  Without a bracket (e.g. the 2022 replay) it falls back to the global per-stage
  caps, an over-estimate that ignores intra-squad collisions. Remaining group
  matches are counted from fixtures present in the data (a team with no
  scheduled fixtures loaded contributes none). Teams that already lost a
  knockout match add no further stage-win points (a pending bronze match is
  excluded — third-place wins never count toward ``best_possible``).
- Timeline: group W/D points and knockout stage wins accrue on the match's
  UTC calendar date; the advance bonus accrues on the team's last finished
  group match date.

Runtime is stdlib-only. CLI:

    python -m wcpool.scoring <matches.json> <pool.json> <out standings.json>
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

GROUP = "GROUP"
SF = "SF"
THIRD_PLACE = "THIRD_PLACE"
FINAL = "FINAL"
FINISHED = "FINISHED"


def _match_list(matches) -> list[dict]:
    """Accept either the bare match list or the full matches.json document."""
    if isinstance(matches, dict):
        return list(matches.get("matches", []))
    return list(matches)


def _ko_stages(scoring_cfg: dict) -> list[str]:
    """Knockout stage order, purely from config (THIRD_PLACE excluded)."""
    return list(scoring_cfg["stage_win_points"])


def _group_record(group_results: list[str]) -> dict[str, int]:
    """W/D/L counts from a team's finished group results (frozen after groups)."""
    return {
        "w": group_results.count("W"),
        "d": group_results.count("D"),
        "l": group_results.count("L"),
    }


def _is_finished_final(match: dict) -> bool:
    return match["stage"] == FINAL and match["status"] == FINISHED


def score_team(achievement: dict, scoring_cfg: dict, third_place_final: bool) -> int:
    """Points for one team's achievements dict. All values come from config."""
    points = 0
    for result in achievement.get("group_results", []):
        if result == "W":
            points += scoring_cfg["group_win"]
        elif result == "D":
            points += scoring_cfg["group_draw"]
    if achievement.get("advanced"):
        points += scoring_cfg["advance"]
    for stage in achievement.get("ko_wins", []):
        if stage == THIRD_PLACE:
            if third_place_final:
                points += scoring_cfg["third_place_win"]
        else:
            points += scoring_cfg["stage_win_points"][stage]
    return points


def achievements_from_matches(matches, scoring_cfg: dict) -> dict[str, dict]:
    """Derive per-team achievements dicts from the normalized match list."""
    matches = _match_list(matches)
    stages = _ko_stages(scoring_cfg)
    first_ko = stages[0] if stages else None

    teams: set[str] = set()
    for m in matches:
        for side in ("home", "away"):
            if m.get(side):
                teams.add(m[side])

    tournament_finished = any(_is_finished_final(m) for m in matches)
    third_place_exists = any(m["stage"] == THIRD_PLACE for m in matches)
    third_place_finished = any(
        m["stage"] == THIRD_PLACE and m["status"] == FINISHED for m in matches
    )
    # The API publishes the whole fixture list upfront with TBD (null) knockout
    # slots, so "an R32 fixture exists" is NOT evidence a team failed to advance.
    # Only treat the bracket as decisive once every first-round slot has both
    # teams assigned; until then a group-completed team is undetermined (stays
    # alive), never prematurely eliminated. See test_eliminated_at_group_*.
    first_ko_fixtures = [m for m in matches if m["stage"] == first_ko]
    first_ko_bracket_set = bool(first_ko_fixtures) and all(
        m.get("home") and m.get("away") for m in first_ko_fixtures
    )

    out: dict[str, dict] = {}
    for team in sorted(teams):
        team_matches = sorted(
            (m for m in matches if team in (m.get("home"), m.get("away"))),
            key=lambda m: m["utc_date"],
        )
        group_matches = [m for m in team_matches if m["stage"] == GROUP]
        group_results = []
        for m in group_matches:
            if m["status"] != FINISHED:
                continue
            if m.get("winner") == team:
                group_results.append("W")
            elif m.get("winner") is None:
                group_results.append("D")
            else:
                group_results.append("L")

        # Contract rule: appears in a knockout fixture. A team in any round beyond
        # the first (R16/QF/…) is unambiguously through. For the *first* round,
        # though, only credit advancement once the bracket is fully populated: the
        # API fills R32 slots unevenly as groups clinch, so crediting on partial
        # data would light up an arbitrary subset of qualifiers (the ones the API
        # happened to place) while identically-placed teams wait. Gating the R32
        # case on first_ko_bracket_set makes all qualifiers reveal together,
        # symmetric with "GROUP" elimination below. (In real data a later-round
        # slot is never filled before R32 is set, so this only keeps the adapter
        # correct on synthetic/bracket-tail data.)
        in_first_ko = any(m["stage"] == first_ko for m in team_matches)
        in_later_ko = any(m["stage"] not in (GROUP, first_ko) for m in team_matches)
        advanced = first_ko is not None and (
            in_later_ko or (in_first_ko and first_ko_bracket_set)
        )

        ko_finished = [
            m for m in team_matches if m["stage"] != GROUP and m["status"] == FINISHED
        ]
        ko_wins = [m["stage"] for m in ko_finished if m.get("winner") == team]
        ko_losses = [m for m in ko_finished if m.get("winner") not in (None, team)]

        eliminated_at = None
        if ko_losses:
            last_loss = ko_losses[-1]  # chronological; at most SF then THIRD_PLACE
            if last_loss["stage"] == SF and third_place_exists:
                # Losing the SF does not eliminate while the bronze match is
                # pending; afterwards the bronze winner's bracket exit was SF.
                eliminated_at = SF if third_place_finished else None
            else:
                eliminated_at = last_loss["stage"]
        elif (
            group_matches
            and all(m["status"] == FINISHED for m in group_matches)
            and first_ko_bracket_set
            and not advanced
        ):
            eliminated_at = GROUP

        out[team] = {
            "team": team,
            "group_results": group_results,
            "advanced": advanced,
            "ko_wins": ko_wins,
            "alive": eliminated_at is None and not tournament_finished,
            "eliminated_at": eliminated_at,
        }
    return out


def _empty_achievement(code: str, tournament_finished: bool) -> dict:
    return {
        "team": code,
        "group_results": [],
        "advanced": False,
        "ko_wins": [],
        "alive": not tournament_finished,
        "eliminated_at": None,
    }


def _ko_losers(matches: list[dict]) -> set[str]:
    """Teams that lost a finished knockout match (out of the title bracket)."""
    losers = set()
    for m in matches:
        if m["stage"] != GROUP and m["status"] == FINISHED and m.get("winner"):
            losers.add(m["away"] if m["winner"] == m["home"] else m["home"])
    return losers


def _stage_caps(scoring_cfg: dict) -> dict[str, int]:
    """Max number of teams that can win each knockout stage: 1 FINAL winner,
    2 SF winners, 4 QF winners, ... halving each round back from the final.

    Derived from the config stage order (not hardcoded) so it stays correct for
    the 2022 replay, which has no R32 (R16 cap is then 8, not 16).
    """
    stages = _ko_stages(scoring_cfg)
    n = len(stages)
    return {stage: 2 ** (n - 1 - i) for i, stage in enumerate(stages)}


def _bracket_leaves(matches: list[dict], scoring_cfg: dict,
                    bracket_cfg: dict | None) -> dict[str, int] | None:
    """Map each knockout team to a leaf index encoding its bracket position.

    ``bracket_cfg["r16_pods"]`` lists the second-round pods in *tree order* —
    each pod is the set of first-round (R32) participants that funnel into one
    match at the next stage, and adjacent pods meet a round later. Pod ``p`` is
    assigned leaf indices ``2p`` and ``2p+1`` (its two first-round fixtures), so
    for a team on leaf ``L`` the sub-tree it occupies at knockout stage index
    ``i`` (0 = first round) is simply ``L >> i``: two teams collide exactly when
    their leaves agree in the high bits, i.e. share a sub-tree.

    Returns ``None`` (caller falls back to the global caps) unless the bracket
    is present *and* consistent with the data: every first-round fixture has
    both teams set, and each pod contains exactly two of those fixtures. Any
    mismatch (wrong codes, half-populated bracket, 2022 replay with no config)
    yields ``None`` rather than a wrong answer.
    """
    if not bracket_cfg:
        return None
    pods = bracket_cfg.get("r16_pods")
    stages = _ko_stages(scoring_cfg)
    if not pods or not stages:
        return None
    first = stages[0]
    first_fixtures = [m for m in matches if m["stage"] == first]
    pairs = [
        frozenset((m["home"], m["away"]))
        for m in first_fixtures
        if m.get("home") and m.get("away")
    ]
    if len(pairs) != len(first_fixtures) or len(pairs) != 2 * len(pods):
        return None

    leaves: dict[str, int] = {}
    matched: set[frozenset] = set()
    for p, pod in enumerate(pods):
        pod_set = set(pod)
        pod_pairs = sorted(
            (pr for pr in pairs if pr <= pod_set), key=lambda s: sorted(s)
        )
        if len(pod_pairs) != 2:
            return None
        for k, pr in enumerate(pod_pairs):
            matched.add(pr)
            for team in pr:
                if team in leaves:
                    return None
                leaves[team] = 2 * p + k
    if len(matched) != len(pairs) or len(leaves) != 2 * len(pairs):
        return None
    return leaves


def _remaining_group_pts(code: str, matches: list[dict], scoring_cfg: dict) -> int:
    """Points from a team's not-yet-played group fixtures, assuming it wins them."""
    remaining = sum(
        1
        for m in matches
        if m["stage"] == GROUP and m["status"] != FINISHED and code in (m["home"], m["away"])
    )
    return remaining * scoring_cfg["group_win"]


def _best_possible_extra(player_teams: list[str], achievements: dict[str, dict],
                         matches: list[dict], scoring_cfg: dict, ko_losers: set[str],
                         tournament_finished: bool,
                         bracket_leaves: dict[str, int] | None = None) -> int:
    """Per-player upper-bound points still attainable across all their teams.

    Group wins and the advance bonus are independent per team. Knockout stage
    wins are capped: at most ``_stage_caps`` of the player's teams can win each
    stage (one champion, two finalists, ...), counting already-banked wins
    against those slots.

    With ``bracket_leaves`` the knockout cap is exact rather than global: each
    stage credits the number of distinct bracket sub-trees the player's still-
    live teams occupy (minus sub-trees where a win is already banked), so two
    teams destined to meet contribute only one win from their meeting round on.
    Without it, the global caps apply and intra-squad collisions are ignored.
    """
    stages = _ko_stages(scoring_cfg)
    extra = 0

    if bracket_leaves is not None:
        winnable: dict[str, set[int]] = defaultdict(set)  # stage -> occupied sub-trees
        banked: dict[str, set[int]] = defaultdict(set)    # stage -> sub-trees already won
        for code in player_teams:
            ach = achievements.get(code) or _empty_achievement(code, tournament_finished)
            leaf = bracket_leaves.get(code)
            if leaf is not None:
                for i, stage in enumerate(stages):
                    if stage in ach["ko_wins"]:
                        banked[stage].add(leaf >> i)
            if not ach["alive"]:
                continue
            extra += _remaining_group_pts(code, matches, scoring_cfg)
            if not ach["advanced"]:
                extra += scoring_cfg["advance"]
            # A team beaten in the bracket can win no further stage; a pending
            # bronze match (third place) is never part of best_possible.
            if code not in ko_losers and leaf is not None:
                for i, stage in enumerate(stages):
                    winnable[stage].add(leaf >> i)
        for stage, pts in scoring_cfg["stage_win_points"].items():
            extra += len(winnable[stage] - banked[stage]) * pts
        return extra

    caps = _stage_caps(scoring_cfg)
    already: dict[str, int] = defaultdict(int)   # stage -> player's teams that already won it
    eligible: dict[str, int] = defaultdict(int)  # stage -> player's teams that could still win it
    for code in player_teams:
        ach = achievements.get(code) or _empty_achievement(code, tournament_finished)
        for stage in ach["ko_wins"]:
            if stage in caps:
                already[stage] += 1
        if not ach["alive"]:
            continue
        extra += _remaining_group_pts(code, matches, scoring_cfg)
        if not ach["advanced"]:
            extra += scoring_cfg["advance"]
        # Third-place win is never part of best_possible (it is not in
        # stage_win_points); a team already beaten in the bracket adds nothing.
        if code not in ko_losers:
            for stage in caps:
                if stage not in ach["ko_wins"]:
                    eligible[stage] += 1
    for stage, pts in scoring_cfg["stage_win_points"].items():
        credited = min(eligible[stage], max(0, caps[stage] - already[stage]))
        extra += credited * pts
    return extra


def _timeline(matches: list[dict], pool_cfg: dict, achievements: dict[str, dict]) -> list[dict]:
    """Cumulative totals per player per UTC calendar day with >=1 finished match."""
    scoring = pool_cfg["scoring"]
    third_place_final = bool(pool_cfg.get("third_place_final", False))
    owner = {
        code: player["name"] for player in pool_cfg["players"] for code in player["teams"]
    }
    finished = sorted(
        (m for m in matches if m["status"] == FINISHED), key=lambda m: m["utc_date"]
    )

    events: dict[str, list[tuple[str, int]]] = defaultdict(list)  # date -> [(player, pts)]
    for m in finished:
        date = m["utc_date"][:10]
        if m["stage"] == GROUP:
            for code in (m["home"], m["away"]):
                name = owner.get(code)
                if name is None:
                    continue
                if m.get("winner") == code:
                    events[date].append((name, scoring["group_win"]))
                elif m.get("winner") is None:
                    events[date].append((name, scoring["group_draw"]))
        elif m.get("winner") and m["winner"] in owner:
            if m["stage"] == THIRD_PLACE:
                if third_place_final:
                    events[date].append((owner[m["winner"]], scoring["third_place_win"]))
            else:
                events[date].append((owner[m["winner"]], scoring["stage_win_points"][m["stage"]]))

    # Advance bonus accrues on the team's last finished group match date.
    for code, ach in achievements.items():
        name = owner.get(code)
        if name is None or not ach["advanced"]:
            continue
        group_dates = [
            m["utc_date"][:10]
            for m in finished
            if m["stage"] == GROUP and code in (m["home"], m["away"])
        ]
        if group_dates:
            events[max(group_dates)].append((name, scoring["advance"]))

    totals = {player["name"]: 0 for player in pool_cfg["players"]}
    timeline = []
    for date in sorted({m["utc_date"][:10] for m in finished}):
        for name, pts in events.get(date, []):
            totals[name] += pts
        timeline.append({"date": date, "totals": dict(totals)})
    return timeline


def compute_standings(matches, pool_cfg: dict, *, generated_at: str | None = None) -> dict:
    """Build the full standings.json structure (see docs/CONTRACTS.md)."""
    matches = _match_list(matches)
    scoring = pool_cfg["scoring"]
    third_place_final = bool(pool_cfg.get("third_place_final", False))
    achievements = achievements_from_matches(matches, scoring)
    tournament_finished = any(_is_finished_final(m) for m in matches)
    ko_losers = _ko_losers(matches)
    bracket_leaves = _bracket_leaves(matches, scoring, pool_cfg.get("bracket"))

    players = []
    for player in pool_cfg["players"]:
        total = 0
        teams = []
        for code in player["teams"]:
            ach = achievements.get(code) or _empty_achievement(code, tournament_finished)
            points = score_team(ach, scoring, third_place_final)
            total += points
            teams.append(
                {
                    "code": code,
                    "points": points,
                    "alive": ach["alive"],
                    "eliminated_at": ach["eliminated_at"],
                    "group_record": _group_record(ach["group_results"]),
                    "advanced": ach["advanced"],
                    "ko_wins": list(ach["ko_wins"]),
                }
            )
        extra = _best_possible_extra(
            player["teams"], achievements, matches, scoring, ko_losers,
            tournament_finished, bracket_leaves,
        )
        players.append(
            {
                "name": player["name"],
                "points": total,
                "best_possible": total + extra,
                "rank": 0,  # assigned below
                "teams": teams,
            }
        )

    players.sort(key=lambda p: (-p["points"], p["name"]))
    for i, player in enumerate(players):
        if i and player["points"] == players[i - 1]["points"]:
            player["rank"] = players[i - 1]["rank"]
        else:
            player["rank"] = i + 1

    if generated_at is None:
        generated_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
    return {
        "generated_at": generated_at,
        "players": players,
        "timeline": _timeline(matches, pool_cfg, achievements),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m wcpool.scoring",
        description="Compute pool standings from normalized match results.",
    )
    parser.add_argument("matches", help="path to matches.json")
    parser.add_argument("pool", help="path to pool.json")
    parser.add_argument("out", help="path to write standings.json")
    args = parser.parse_args(argv)

    matches = json.loads(Path(args.matches).read_text(encoding="utf-8"))
    pool_cfg = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    standings = compute_standings(matches, pool_cfg)
    Path(args.out).write_text(
        json.dumps(standings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
