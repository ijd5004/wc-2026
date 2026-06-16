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
- ``best_possible`` is the naive independent upper bound: bracket collisions
  between one player's teams are ignored. Remaining group matches are counted
  from fixtures present in the data (a team with no scheduled fixtures loaded
  contributes none). Teams that already lost a knockout match add no further
  stage-win points (a pending bronze match is excluded — third-place wins
  never count toward ``best_possible``).
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
    first_ko_fixtures_exist = any(m["stage"] == first_ko for m in matches)

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

        # Contract rule: appears in a first-knockout-round fixture. Appearing in
        # any later knockout fixture implies the same, which keeps the adapter
        # correct on partial data (e.g. only the bracket tail loaded).
        advanced = first_ko is not None and any(m["stage"] != GROUP for m in team_matches)

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
            and first_ko_fixtures_exist
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


def _best_possible_extra(code: str, ach: dict, matches: list[dict], scoring_cfg: dict,
                         ko_losers: set[str]) -> int:
    """Naive upper-bound points still attainable by one team (0 if not alive)."""
    if not ach["alive"]:
        return 0
    extra = 0
    remaining_group = sum(
        1
        for m in matches
        if m["stage"] == GROUP and m["status"] != FINISHED and code in (m["home"], m["away"])
    )
    extra += remaining_group * scoring_cfg["group_win"]
    if not ach["advanced"]:
        extra += scoring_cfg["advance"]
    if code not in ko_losers:
        # Third-place win is never part of best_possible (it is not in
        # stage_win_points); a team already beaten in the bracket adds nothing.
        extra += sum(
            pts for stage, pts in scoring_cfg["stage_win_points"].items()
            if stage not in ach["ko_wins"]
        )
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

    players = []
    for player in pool_cfg["players"]:
        total = 0
        extra = 0
        teams = []
        for code in player["teams"]:
            ach = achievements.get(code) or _empty_achievement(code, tournament_finished)
            points = score_team(ach, scoring, third_place_final)
            total += points
            extra += _best_possible_extra(code, ach, matches, scoring, ko_losers)
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
