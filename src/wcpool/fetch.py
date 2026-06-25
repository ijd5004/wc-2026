"""Fetch World Cup match data from football-data.org and normalize it.

Produces (under ``--data-dir``, default ``data/``):

- ``matches.json``   — normalized results per ``docs/CONTRACTS.md``
- ``teams.json``     — canonical team table (merged, never drops existing entries)
- ``raw/<YYYY-MM-DD>.json`` — raw API snapshot for the UTC day (idempotent)

Usage::

    FOOTBALL_DATA_API_KEY=... python -m wcpool.fetch [--data-dir data]

Exit codes: 0 success, 1 fetch/normalization failure (existing ``matches.json``
left untouched), 2 missing API key.

Runtime dependencies: stdlib only (urllib).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

API_URL = "https://api.football-data.org/v4/competitions/WC/matches"
ENV_KEY = "FOOTBALL_DATA_API_KEY"

# football-data.org v4 stage -> CONTRACTS.md stage enum. Unknown stages are a
# hard error (don't guess).
STAGE_MAP = {
    "GROUP_STAGE": "GROUP",
    "LAST_32": "R32",
    "LAST_16": "R16",
    "QUARTER_FINALS": "QF",
    "SEMI_FINALS": "SF",
    "THIRD_PLACE": "THIRD_PLACE",
    "FINAL": "FINAL",
}

# Statuses collapse to SCHEDULED | IN_PLAY | FINISHED.
_FINISHED_STATUSES = {"FINISHED", "AWARDED"}
_IN_PLAY_STATUSES = {"IN_PLAY", "PAUSED"}

DURATION_MAP = {
    "REGULAR": "REGULAR",
    "EXTRA_TIME": "ET",
    "PENALTY_SHOOTOUT": "PENALTIES",
}

# FIFA three-letter codes (not ISO!) -> flag emoji, for plausible 2026
# qualifiers. Unknown codes fall back to a white flag with a stderr warning.
FLAGS = {
    # Hosts + CONCACAF
    "USA": "🇺🇸", "MEX": "🇲🇽", "CAN": "🇨🇦", "CRC": "🇨🇷", "JAM": "🇯🇲",
    "PAN": "🇵🇦", "HON": "🇭🇳", "SLV": "🇸🇻", "GUA": "🇬🇹", "HAI": "🇭🇹",
    "CUW": "🇨🇼", "TRI": "🇹🇹", "SUR": "🇸🇷",
    # CONMEBOL
    "ARG": "🇦🇷", "BRA": "🇧🇷", "URU": "🇺🇾", "COL": "🇨🇴", "ECU": "🇪🇨",
    "PER": "🇵🇪", "CHI": "🇨🇱", "PAR": "🇵🇾", "VEN": "🇻🇪", "BOL": "🇧🇴",
    # UEFA (note FIFA codes: GER, NED, SUI, CRO, DEN, ...)
    "GER": "🇩🇪", "NED": "🇳🇱", "SUI": "🇨🇭", "FRA": "🇫🇷", "ESP": "🇪🇸",
    "ENG": "🏴󠁧󠁢󠁥󠁮󠁧󠁿", "SCO": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "WAL": "🏴󠁧󠁢󠁷󠁬󠁳󠁿", "POR": "🇵🇹", "ITA": "🇮🇹",
    "BEL": "🇧🇪", "CRO": "🇭🇷", "DEN": "🇩🇰", "POL": "🇵🇱", "AUT": "🇦🇹",
    "SRB": "🇷🇸", "UKR": "🇺🇦", "TUR": "🇹🇷", "SWE": "🇸🇪", "NOR": "🇳🇴",
    "CZE": "🇨🇿", "GRE": "🇬🇷", "HUN": "🇭🇺", "ROU": "🇷🇴", "SVK": "🇸🇰",
    "SVN": "🇸🇮", "ALB": "🇦🇱", "IRL": "🇮🇪", "ISL": "🇮🇸", "GEO": "🇬🇪",
    # AFC
    "JPN": "🇯🇵", "KOR": "🇰🇷", "AUS": "🇦🇺", "IRN": "🇮🇷", "KSA": "🇸🇦",
    "QAT": "🇶🇦", "UAE": "🇦🇪", "IRQ": "🇮🇶", "UZB": "🇺🇿", "JOR": "🇯🇴",
    "CHN": "🇨🇳", "OMA": "🇴🇲", "BHR": "🇧🇭", "IDN": "🇮🇩", "KUW": "🇰🇼",
    # CAF
    "MAR": "🇲🇦", "SEN": "🇸🇳", "TUN": "🇹🇳", "ALG": "🇩🇿", "EGY": "🇪🇬",
    "NGA": "🇳🇬", "GHA": "🇬🇭", "CMR": "🇨🇲", "CIV": "🇨🇮", "RSA": "🇿🇦",
    "MLI": "🇲🇱", "BFA": "🇧🇫", "CPV": "🇨🇻", "COD": "🇨🇩", "ZAM": "🇿🇲",
    "GAB": "🇬🇦",
    # OFC
    "NZL": "🇳🇿",
    # UEFA additions seen in live 2026 data
    "BIH": "🇧🇦",
}
FALLBACK_FLAG = "🏳️"

# The API's tla is not always the FIFA code (observed in live 2026 data:
# Uruguay is URY there, FIFA says URU). Everything downstream — pool.json,
# teams.json, standings — speaks FIFA, so alias API codes here.
TLA_ALIASES = {
    "URY": "URU",
    "CUR": "CUW",
}


def canonical_code(tla: str | None) -> str | None:
    if tla is None:
        return None
    return TLA_ALIASES.get(tla, tla)


class FetchError(Exception):
    """Raised when the API request fails (HTTP error, network, bad JSON)."""


class NormalizationError(ValueError):
    """Raised when the API payload doesn't match what we know how to map."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def http_fetch(api_key: str) -> dict[str, Any]:
    """Default transport: GET the matches endpoint, return parsed JSON."""
    request = urllib.request.Request(API_URL, headers={"X-Auth-Token": api_key})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} from {API_URL}: {exc.reason}") from exc
    except OSError as exc:  # URLError, socket timeouts, DNS failures
        raise FetchError(f"network error fetching {API_URL}: {exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise FetchError(f"invalid JSON from {API_URL}: {exc}") from exc


def map_stage(stage: str) -> str:
    try:
        return STAGE_MAP[stage]
    except KeyError:
        raise NormalizationError(f"unknown stage from API: {stage!r}") from None


def map_status(status: str) -> str:
    if status in _FINISHED_STATUSES:
        return "FINISHED"
    if status in _IN_PLAY_STATUSES:
        return "IN_PLAY"
    return "SCHEDULED"


def map_decided_by(duration: str | None) -> str:
    """Map ``score.duration`` to ``REGULAR | ET | PENALTIES``.

    This field is informational — the ``winner`` field drives all scoring — so an
    unexpected or missing value must **not** abort the whole fetch (which would
    silently freeze the dashboard mid-tournament, exactly when ET/penalty matches
    first appear). A missing/null duration and any unrecognized value degrade to
    ``REGULAR``; unknown non-null values also emit a stderr warning.
    """
    if duration is None:
        return "REGULAR"
    if duration in DURATION_MAP:
        return DURATION_MAP[duration]
    print(f"warning: unknown score.duration {duration!r}; treating as REGULAR", file=sys.stderr)
    return "REGULAR"


def extract_group(group: str | None) -> str | None:
    """API "Group A" or "GROUP_A" -> "A"; null (knockouts) stays null."""
    if not group:
        return None
    return group.replace("_", " ").split()[-1]


def resolve_winner(winner: str | None, home: str | None, away: str | None) -> str | None:
    if winner == "HOME_TEAM":
        return home
    if winner == "AWAY_TEAM":
        return away
    return None  # DRAW or null


def normalize_match(match: dict[str, Any]) -> dict[str, Any]:
    home = canonical_code(match["homeTeam"].get("tla"))
    away = canonical_code(match["awayTeam"].get("tla"))
    score = match.get("score") or {}
    full_time = score.get("fullTime") or {}
    return {
        "id": match["id"],
        "stage": map_stage(match["stage"]),
        "group": extract_group(match.get("group")),
        "utc_date": match["utcDate"],
        "status": map_status(match["status"]),
        "home": home,
        "away": away,
        "score": {"home": full_time.get("home"), "away": full_time.get("away")},
        "winner": resolve_winner(score.get("winner"), home, away),
        "decided_by": map_decided_by(score.get("duration")),
    }


def normalize(raw: dict[str, Any], fetched_at: datetime) -> dict[str, Any]:
    """Convert a raw v4 response to the CONTRACTS.md matches.json document."""
    return {
        "fetched_at": fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "competition": "WC",
        "matches": [normalize_match(m) for m in raw.get("matches", [])],
    }


def build_teams(
    raw: dict[str, Any],
    existing: dict[str, dict[str, str]] | None = None,
    *,
    warn: Callable[[str], None] | None = None,
) -> dict[str, dict[str, str]]:
    """Build the teams.json table from API team entries, merged over `existing`.

    Existing entries are never dropped; a manually-corrected flag survives a
    refresh if the code is missing from the static FLAGS table.
    """
    if warn is None:
        warn = lambda msg: print(msg, file=sys.stderr)  # noqa: E731
    teams = dict(existing or {})
    for match in raw.get("matches", []):
        for side in ("homeTeam", "awayTeam"):
            entry = match.get(side) or {}
            code, name = canonical_code(entry.get("tla")), entry.get("name")
            if not code or not name:
                continue  # TBD knockout slot
            flag = FLAGS.get(code)
            if flag is None:
                flag = teams.get(code, {}).get("flag") or FALLBACK_FLAG
                if flag == FALLBACK_FLAG:
                    warn(f"warning: no flag emoji for team code {code!r}; using fallback")
            teams[code] = {"name": name, "flag": flag}
    return teams


def write_snapshot(raw: dict[str, Any], data_dir: Path, now: datetime) -> Path:
    """Write the raw API response to data/raw/<YYYY-MM-DD>.json (UTC, idempotent)."""
    raw_dir = data_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{now.strftime('%Y-%m-%d')}.json"
    _write_json(path, raw)
    return path


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None, *, fetch_fn: Callable[[str], dict] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m wcpool.fetch",
        description="Fetch and normalize World Cup match data from football-data.org.",
    )
    parser.add_argument("--data-dir", default="data", help="output directory (default: data)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    api_key = os.environ.get(ENV_KEY)
    if not api_key:
        print(f"error: {ENV_KEY} is not set (get a token at football-data.org)", file=sys.stderr)
        return 2

    fetch = fetch_fn or http_fetch
    now = _utc_now()
    try:
        raw = fetch(api_key)
        normalized = normalize(raw, now)
    except (FetchError, NormalizationError) as exc:
        print(f"error: {exc}; leaving existing matches.json untouched", file=sys.stderr)
        return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    write_snapshot(raw, data_dir, now)

    teams_path = data_dir / "teams.json"
    existing_teams = _read_json(teams_path) if teams_path.exists() else {}
    teams = build_teams(raw, existing_teams)
    _write_json(teams_path, dict(sorted(teams.items())))

    _write_json(data_dir / "matches.json", normalized)
    print(
        f"wrote {len(normalized['matches'])} matches, {len(teams)} teams to {data_dir}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
