# Data contracts

Shared schemas between pipeline stages. **These are the law for all issue branches** — if a
change is needed, change it here first in its own commit and flag it in the PR description.

All JSON files live under `data/` (committed) except build output, which goes to `site/`
(gitignored; published by CI).

## Stage enum

`GROUP`, `R32`, `R16`, `QF`, `SF`, `THIRD_PLACE`, `FINAL`

2026 knockout order: `R32 → R16 → QF → SF → FINAL` (with `THIRD_PLACE` played between SF and
FINAL). The 2022 regression fixture has no `R32`; the scoring engine must take the stage list
from config, not hardcode it.

## `data/teams.json` — canonical team table (owner: issue #2)

Keys are FIFA three-letter codes. All other files refer to teams **by code only**.

```json
{
  "FRA": {"name": "France", "flag": "🇫🇷"},
  "ARG": {"name": "Argentina", "flag": "🇦🇷"}
}
```

## `data/matches.json` — normalized results (owner: issue #2)

```json
{
  "fetched_at": "2026-06-12T04:30:00Z",
  "competition": "WC",
  "matches": [
    {
      "id": 12345,
      "stage": "GROUP",
      "group": "A",
      "utc_date": "2026-06-11T20:00:00Z",
      "status": "FINISHED",
      "home": "MEX",
      "away": "RSA",
      "score": {"home": 2, "away": 1},
      "winner": "MEX",
      "decided_by": "REGULAR"
    }
  ]
}
```

- `status`: `SCHEDULED` | `IN_PLAY` | `FINISHED` (collapse the API's finer states to these).
- `group` is `null` for knockout matches.
- `winner` is a team code, or `null` for a draw / unfinished match.
- `decided_by`: `REGULAR` | `ET` | `PENALTIES`. A knockout match decided on penalties has a
  `winner` (shootout wins count as wins). A group match level after 90' is a draw
  (`winner: null`) regardless of anything else.

## `data/pool.json` — pool config + picks (owner: issue #3)

```json
{
  "pool_name": "D Dogs",
  "third_place_final": false,
  "scoring": {
    "group_win": 3,
    "group_draw": 1,
    "advance": 3,
    "stage_win_points": {"R32": 4, "R16": 6, "QF": 8, "SF": 10, "FINAL": 14},
    "third_place_win": 4
  },
  "players": [
    {"name": "Ian", "teams": ["FRA", "MAR"]}
  ]
}
```

- `third_place_final` toggles whether `THIRD_PLACE` wins score `third_place_win`. Default off.
- `stage_win_points` keys define which knockout stages exist — this is how the engine replays
  2022 (`{"R16": 4, "QF": 6, "SF": 10, "FINAL": 12}`).
- Validation (issue #3): every code exists in `teams.json`; no team drafted twice; player
  names unique.

## Achievements — scoring engine intermediate (owner: issue #1)

The scoring core operates on per-team achievements, derived from `matches.json` by an adapter
in the same module. The 2022 regression fixture (`tests/fixtures/wc2022.json`) feeds this
layer directly, since we only have the 2022 outcome grid, not per-match data.

```json
{
  "team": "FRA",
  "group_results": ["W", "W", "L"],
  "advanced": true,
  "ko_wins": ["R16", "QF", "SF"],
  "alive": false,
  "eliminated_at": "FINAL"
}
```

- `advanced` derivation from matches: the team appears in a first-knockout-round fixture,
  **once the bracket is fully populated** (every first-round slot has both teams assigned).
  The API fills knockout slots unevenly as groups clinch, so advancement is withheld until
  the whole bracket is set — otherwise an arbitrary subset of qualifiers (the ones the API
  happened to place first) would light up while identically-placed teams wait. All qualifiers
  then reveal together, on the same gate as `"GROUP"` elimination. (Covers group
  winners/runners-up and third-place qualifiers alike — flat bonus either way.)
- `alive`: not yet eliminated and tournament unfinished. While fixtures are unknown
  (group stage in progress), every undefeated-in-the-bracket team is alive.
- `eliminated_at`: stage of the knockout loss, `"GROUP"` for group-stage exits, `null` if alive.
- **`"GROUP"` elimination only triggers once the first-round bracket is populated** —
  i.e. every first-knockout-round fixture has both teams assigned. The API ships the full
  schedule upfront with TBD (`null`) knockout slots, so a placeholder R32 fixture is **not**
  evidence a group-completed team failed to advance. Until the bracket is set, such teams are
  `alive` / undetermined.

## Standings output — engine → dashboard/card (owner: issue #1)

Written to `data/standings.json` by the scoring step:

```json
{
  "generated_at": "2026-06-12T04:31:00Z",
  "players": [
    {
      "name": "Ian",
      "points": 73,
      "best_possible": 85,
      "rank": 1,
      "teams": [
        {
          "code": "FRA",
          "points": 29,
          "alive": true,
          "eliminated_at": null,
          "group_record": {"w": 2, "d": 1, "l": 0},
          "advanced": true,
          "ko_wins": ["R32", "R16"]
        }
      ]
    }
  ],
  "timeline": [
    {"date": "2026-06-11", "totals": {"Ian": 3, "Bob": 0}}
  ]
}
```

- `timeline`: cumulative totals per player after each calendar day with a finished match
  (drives the race chart). Achievement bonuses (advance, stage wins) accrue on the date of
  the match that earned them.
- `best_possible` is a **per-player upper bound**: current points + all remaining group wins
  and advance bonuses per team, plus knockout stage wins capped by how many of the player's
  own teams can win each stage (one champion, two finalists, four semifinalists, …, halving
  each round back from the final; already-banked wins consume those slots). When `pool.json`
  supplies a `bracket` (see below) the knockout cap is **fully bracket-aware**: each stage
  counts the distinct bracket sub-trees the player's live teams occupy, so two teams that
  would meet before the final contribute only one win from their meeting round on. Without a
  valid bracket (e.g. the 2022 replay, or before the first knockout round is fully drawn) it
  falls back to the global per-stage caps, which ignore intra-squad collisions.
- `bracket` (optional, in `pool.json`) makes the above exact. `r16_pods` lists the eight
  second-round pods in bracket (tree) order — each pod is the four first-round participants
  that funnel into one next-round match; pods 0&1 meet in a QF, pods 0–3 share a semifinal
  half. It is validated against the actual first-round fixtures at scoring time (every slot
  filled, each pod holding exactly two fixtures) and silently ignored if it no longer matches,
  so a stale or malformed bracket can only relax the bound to the global caps, never corrupt it.
- Per-team `group_record` is the frozen group-stage W/D/L tally; `advanced` and `ko_wins`
  (mirrored from the achievements layer) let the dashboard show the compact round a team
  reached. The points column is independent and keeps counting through the knockouts.

## Environment

- `FOOTBALL_DATA_API_KEY` — football-data.org token. Env var locally, Actions secret in CI.
  Never committed.

## Dependency policy

Runtime code is **stdlib only** (urllib for HTTP). Dev tools: pytest, ruff. Exception:
the share-card renderer (issue #5) may add Pillow. Any other dependency needs a written
reason in the PR.
