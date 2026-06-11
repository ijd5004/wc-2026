"""Load and validate the pool definition (data/pool.json).

Schema per docs/CONTRACTS.md. Validation fails loudly and lists every problem at
once, so a typo'd draft submission is a one-pass fix.
"""

import json
from pathlib import Path

REQUIRED_SCORING_KEYS = {"group_win", "group_draw", "advance", "stage_win_points", "third_place_win"}


class PoolValidationError(ValueError):
    """Raised when pool.json is malformed; message lists all problems found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid pool.json:\n" + "\n".join(f"  - {p}" for p in problems))


def load_pool(pool_path: str | Path, teams: dict | None = None) -> dict:
    """Load pool.json, validate, and return it.

    `teams` is the canonical team table (data/teams.json contents). When given,
    every drafted code must exist in it; when None (e.g. before the first fetch),
    code-existence checks are skipped but structural checks still run.
    """
    pool = json.loads(Path(pool_path).read_text())
    problems = []

    for key in ("pool_name", "third_place_final", "scoring", "players"):
        if key not in pool:
            problems.append(f"missing top-level key: {key}")
    if problems:
        raise PoolValidationError(problems)

    if not isinstance(pool["third_place_final"], bool):
        problems.append("third_place_final must be true or false")

    scoring = pool["scoring"]
    missing = REQUIRED_SCORING_KEYS - scoring.keys()
    if missing:
        problems.append(f"scoring missing keys: {', '.join(sorted(missing))}")
    elif not isinstance(scoring["stage_win_points"], dict) or not scoring["stage_win_points"]:
        problems.append("scoring.stage_win_points must be a non-empty stage→points map")

    players = pool["players"]
    if not players:
        problems.append("players list is empty")

    names = [p.get("name") for p in players]
    for name in {n for n in names if names.count(n) > 1}:
        problems.append(f"duplicate player name: {name}")

    seen: dict[str, str] = {}
    team_counts = {len(p.get("teams", [])) for p in players}
    if len(team_counts) > 1:
        problems.append(f"players have unequal team counts: {sorted(team_counts)}")
    for p in players:
        name = p.get("name", "<unnamed>")
        if not p.get("teams"):
            problems.append(f"player {name} has no teams")
        for code in p.get("teams", []):
            if code in seen:
                problems.append(f"team {code} drafted twice: {seen[code]} and {name}")
            seen[code] = name
            if teams is not None and code not in teams:
                problems.append(f"unknown team code {code} (player {name}) — not in teams.json")

    if problems:
        raise PoolValidationError(problems)
    return pool


def load_pool_with_teams(pool_path: str | Path, teams_path: str | Path) -> dict:
    teams = json.loads(Path(teams_path).read_text())
    return load_pool(pool_path, teams)
