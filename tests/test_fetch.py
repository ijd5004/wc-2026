"""Tests for wcpool.fetch — no live network; everything runs off recorded fixtures."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wcpool import fetch
from wcpool.fetch import (
    FetchError,
    NormalizationError,
    build_teams,
    extract_group,
    map_decided_by,
    map_stage,
    map_status,
    normalize,
    resolve_winner,
    write_snapshot,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fd_api_sample.json"
NOW = datetime(2026, 6, 14, 4, 30, 0, tzinfo=timezone.utc)

MATCH_KEYS = {
    "id", "stage", "group", "utc_date", "status",
    "home", "away", "score", "winner", "decided_by",
}


@pytest.fixture()
def raw():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture()
def normalized(raw):
    return normalize(raw, NOW)


def by_id(doc, match_id):
    return next(m for m in doc["matches"] if m["id"] == match_id)


# --- normalization shape (CONTRACTS.md) -------------------------------------


def test_normalized_document_shape(normalized):
    assert set(normalized) == {"fetched_at", "competition", "matches"}
    assert normalized["fetched_at"] == "2026-06-14T04:30:00Z"
    assert normalized["competition"] == "WC"
    assert len(normalized["matches"]) == 7
    for match in normalized["matches"]:
        assert set(match) == MATCH_KEYS
        assert set(match["score"]) == {"home", "away"}
        assert match["status"] in {"SCHEDULED", "IN_PLAY", "FINISHED"}
        assert match["decided_by"] in {"REGULAR", "ET", "PENALTIES"}


def test_finished_group_win(normalized):
    match = by_id(normalized, 101)
    assert match == {
        "id": 101,
        "stage": "GROUP",
        "group": "A",
        "utc_date": "2026-06-11T20:00:00Z",
        "status": "FINISHED",
        "home": "MEX",
        "away": "RSA",
        "score": {"home": 2, "away": 1},
        "winner": "MEX",
        "decided_by": "REGULAR",
    }


def test_group_draw_has_null_winner(normalized):
    match = by_id(normalized, 102)
    assert match["status"] == "FINISHED"
    assert match["score"] == {"home": 1, "away": 1}
    assert match["winner"] is None
    assert match["group"] == "B"


def test_scheduled_match(normalized):
    match = by_id(normalized, 103)
    assert match["status"] == "SCHEDULED"
    assert match["score"] == {"home": None, "away": None}
    assert match["winner"] is None


def test_in_play_match(normalized):
    match = by_id(normalized, 104)
    assert match["status"] == "IN_PLAY"
    assert match["score"] == {"home": 1, "away": 0}
    assert match["winner"] is None


def test_knockout_decided_on_penalties(normalized):
    match = by_id(normalized, 105)
    assert match["stage"] == "R32"
    assert match["group"] is None
    assert match["score"] == {"home": 2, "away": 2}  # fullTime aggregate, not shootout
    assert match["winner"] == "ARG"  # shootout wins count as wins
    assert match["decided_by"] == "PENALTIES"


def test_knockout_decided_in_extra_time(normalized):
    match = by_id(normalized, 106)
    assert match["stage"] == "QF"
    assert match["winner"] == "POR"
    assert match["decided_by"] == "ET"


def test_tbd_final_has_null_teams_and_group(normalized):
    match = by_id(normalized, 107)
    assert match["stage"] == "FINAL"
    assert match["group"] is None
    assert match["home"] is None
    assert match["away"] is None
    assert match["winner"] is None


# --- individual mappings -----------------------------------------------------


@pytest.mark.parametrize(
    ("api_stage", "expected"),
    [
        ("GROUP_STAGE", "GROUP"),
        ("LAST_32", "R32"),
        ("LAST_16", "R16"),
        ("QUARTER_FINALS", "QF"),
        ("SEMI_FINALS", "SF"),
        ("THIRD_PLACE", "THIRD_PLACE"),
        ("FINAL", "FINAL"),
    ],
)
def test_stage_mapping(api_stage, expected):
    assert map_stage(api_stage) == expected


def test_unknown_stage_is_a_hard_error():
    with pytest.raises(NormalizationError, match="PLAYOFFS"):
        map_stage("PLAYOFFS")


@pytest.mark.parametrize(
    ("api_status", "expected"),
    [
        ("FINISHED", "FINISHED"),
        ("AWARDED", "FINISHED"),
        ("IN_PLAY", "IN_PLAY"),
        ("PAUSED", "IN_PLAY"),
        ("SCHEDULED", "SCHEDULED"),
        ("TIMED", "SCHEDULED"),
        ("POSTPONED", "SCHEDULED"),
        ("SUSPENDED", "SCHEDULED"),
        ("CANCELLED", "SCHEDULED"),
    ],
)
def test_status_mapping(api_status, expected):
    assert map_status(api_status) == expected


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ("REGULAR", "REGULAR"),
        ("EXTRA_TIME", "ET"),
        ("PENALTY_SHOOTOUT", "PENALTIES"),
        (None, "REGULAR"),  # missing/null duration (e.g. scheduled): must not abort
        ("SOMETHING_NEW", "REGULAR"),  # unknown future enum degrades, never aborts
    ],
)
def test_decided_by_mapping(duration, expected):
    assert map_decided_by(duration) == expected


def test_unknown_duration_does_not_abort_normalization():
    """A knockout match with an unrecognized score.duration still normalizes
    (winner drives scoring; decided_by must never freeze the whole fetch)."""
    from wcpool.fetch import normalize_match

    match = {
        "id": 1,
        "stage": "FINAL",
        "group": None,
        "utcDate": "2026-07-19T19:00:00Z",
        "status": "FINISHED",
        "homeTeam": {"tla": "ARG"},
        "awayTeam": {"tla": "FRA"},
        "score": {"winner": "HOME_TEAM", "duration": "GOLDEN_GOAL",
                  "fullTime": {"home": 1, "away": 0}},
    }
    normalized = normalize_match(match)
    assert normalized["winner"] == "ARG"
    assert normalized["decided_by"] == "REGULAR"


@pytest.mark.parametrize(
    ("api_group", "expected"),
    [("Group A", "A"), ("Group L", "L"), (None, None)],
)
def test_group_letter_extraction(api_group, expected):
    assert extract_group(api_group) == expected


@pytest.mark.parametrize(
    ("winner", "expected"),
    [("HOME_TEAM", "MEX"), ("AWAY_TEAM", "RSA"), ("DRAW", None), (None, None)],
)
def test_winner_resolution(winner, expected):
    assert resolve_winner(winner, "MEX", "RSA") == expected


# --- teams.json --------------------------------------------------------------


def test_build_teams_from_fixture(raw):
    teams = build_teams(raw)
    assert teams["MEX"] == {"name": "Mexico", "flag": "🇲🇽"}
    assert teams["NED"] == {"name": "Netherlands", "flag": "🇳🇱"}  # FIFA code, not ISO
    assert teams["GER"] == {"name": "Germany", "flag": "🇩🇪"}
    assert teams["RSA"] == {"name": "South Africa", "flag": "🇿🇦"}
    assert None not in teams  # TBD final slots are skipped


def test_build_teams_merge_never_drops_existing(raw):
    existing = {
        "JPN": {"name": "Japan", "flag": "🇯🇵"},  # not in fixture, must survive
        "MEX": {"name": "El Tri", "flag": "🇲🇽"},  # refreshed from API
    }
    teams = build_teams(raw, existing)
    assert teams["JPN"] == {"name": "Japan", "flag": "🇯🇵"}
    assert teams["MEX"]["name"] == "Mexico"


def test_unknown_code_falls_back_to_white_flag():
    raw = {
        "matches": [
            {
                "homeTeam": {"name": "Atlantis", "tla": "ATL"},
                "awayTeam": {"name": "Mexico", "tla": "MEX"},
            }
        ]
    }
    warnings = []
    teams = build_teams(raw, warn=warnings.append)
    assert teams["ATL"] == {"name": "Atlantis", "flag": "🏳️"}
    assert any("ATL" in w for w in warnings)


def test_unknown_code_keeps_manually_set_flag():
    raw = {
        "matches": [
            {
                "homeTeam": {"name": "Atlantis", "tla": "ATL"},
                "awayTeam": {"name": "Mexico", "tla": "MEX"},
            }
        ]
    }
    existing = {"ATL": {"name": "Atlantis", "flag": "🔱"}}
    teams = build_teams(raw, existing, warn=lambda _msg: None)
    assert teams["ATL"]["flag"] == "🔱"


# --- raw snapshots -----------------------------------------------------------


def test_snapshot_filename_and_idempotency(tmp_path, raw):
    path = write_snapshot(raw, tmp_path, NOW)
    assert path == tmp_path / "raw" / "2026-06-14.json"
    assert json.loads(path.read_text(encoding="utf-8")) == raw

    rerun = dict(raw, resultSet={"count": 99})
    path2 = write_snapshot(rerun, tmp_path, NOW)
    assert path2 == path
    assert list((tmp_path / "raw").iterdir()) == [path]  # overwritten, not duplicated
    assert json.loads(path.read_text(encoding="utf-8"))["resultSet"] == {"count": 99}


# --- CLI / main --------------------------------------------------------------


def test_main_missing_key_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    rc = fetch.main(["--data-dir", str(tmp_path)], fetch_fn=lambda key: pytest.fail("no fetch"))
    assert rc == 2
    assert "FOOTBALL_DATA_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "matches.json").exists()


def test_main_fetch_error_preserves_matches_and_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-token")
    sentinel = '{"fetched_at": "old", "competition": "WC", "matches": []}'
    (tmp_path / "matches.json").write_text(sentinel, encoding="utf-8")

    def boom(_key):
        raise FetchError("HTTP 503 from api: Service Unavailable")

    rc = fetch.main(["--data-dir", str(tmp_path)], fetch_fn=boom)
    assert rc == 1
    assert "503" in capsys.readouterr().err
    assert (tmp_path / "matches.json").read_text(encoding="utf-8") == sentinel
    assert not (tmp_path / "raw").exists()  # no snapshot of a failed fetch


def test_main_success_writes_everything(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-token")
    monkeypatch.setattr(fetch, "_utc_now", lambda: NOW)
    seen_keys = []

    def fake_fetch(key):
        seen_keys.append(key)
        return raw

    rc = fetch.main(["--data-dir", str(tmp_path)], fetch_fn=fake_fetch)
    assert rc == 0
    assert seen_keys == ["test-token"]

    matches = json.loads((tmp_path / "matches.json").read_text(encoding="utf-8"))
    assert matches == normalize(raw, NOW)

    snapshot = tmp_path / "raw" / "2026-06-14.json"
    assert json.loads(snapshot.read_text(encoding="utf-8")) == raw

    teams = json.loads((tmp_path / "teams.json").read_text(encoding="utf-8"))
    assert teams["ARG"] == {"name": "Argentina", "flag": "🇦🇷"}


def test_main_merges_existing_teams_json(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-token")
    (tmp_path / "teams.json").write_text(
        json.dumps({"JPN": {"name": "Japan", "flag": "🇯🇵"}}), encoding="utf-8"
    )
    rc = fetch.main(["--data-dir", str(tmp_path)], fetch_fn=lambda _key: raw)
    assert rc == 0
    teams = json.loads((tmp_path / "teams.json").read_text(encoding="utf-8"))
    assert teams["JPN"] == {"name": "Japan", "flag": "🇯🇵"}
    assert "MEX" in teams


def test_main_unknown_stage_exits_1_and_preserves_matches(tmp_path, monkeypatch, raw, capsys):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "test-token")
    sentinel = '{"old": true}'
    (tmp_path / "matches.json").write_text(sentinel, encoding="utf-8")
    bad = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bad["matches"][0]["stage"] = "PRELIMINARY_ROUND"

    rc = fetch.main(["--data-dir", str(tmp_path)], fetch_fn=lambda _key: bad)
    assert rc == 1
    assert "PRELIMINARY_ROUND" in capsys.readouterr().err
    assert (tmp_path / "matches.json").read_text(encoding="utf-8") == sentinel


def test_tla_alias_normalizes_uruguay():
    from wcpool.fetch import canonical_code, normalize_match

    match = {
        "id": 1,
        "stage": "GROUP_STAGE",
        "group": "GROUP_H",
        "utcDate": "2026-06-15T18:00:00Z",
        "status": "FINISHED",
        "homeTeam": {"id": 1, "name": "Uruguay", "tla": "URY"},
        "awayTeam": {"id": 2, "name": "Spain", "tla": "ESP"},
        "score": {"winner": "HOME_TEAM", "duration": "REGULAR", "fullTime": {"home": 1, "away": 0}},
    }
    normalized = normalize_match(match)
    assert normalized["home"] == "URU"
    assert normalized["winner"] == "URU"
    assert normalized["group"] == "H"
    assert canonical_code(None) is None


def test_extract_group_handles_both_api_formats():
    from wcpool.fetch import extract_group

    assert extract_group("Group A") == "A"
    assert extract_group("GROUP_A") == "A"
    assert extract_group(None) is None
