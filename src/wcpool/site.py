"""Static, mobile-first standings dashboard.

Reads ``standings.json``, ``pool.json``, ``teams.json`` and ``matches.json`` (see
``docs/CONTRACTS.md``) from a data directory and writes a single self-contained
``site/index.html``: inline CSS, build-time inline SVG race chart, no JavaScript.

Usage::

    python -m wcpool.site --data-dir data --out site
"""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path

# Distinguishable on a dark background; cycles if there are more players than colors.
PALETTE = [
    "#4fc3f7",
    "#ffb74d",
    "#81c784",
    "#e57373",
    "#ba68c8",
    "#fff176",
    "#4db6ac",
    "#f06292",
    "#a1887f",
    "#90a4ae",
]

STAGE_LABELS = {
    "GROUP": "Groups",
    "R32": "R32",
    "R16": "R16",
    "QF": "QF",
    "SF": "SF",
    "THIRD_PLACE": "3rd-place final",
    "FINAL": "Final",
}

# Compact chip labels for the round a team reached, shown on each team row.
STAGE_SHORT = {
    "GROUP": "Groups",
    "R32": "R32",
    "R16": "R16",
    "QF": "QF",
    "SF": "SF",
    "THIRD_PLACE": "3rd",
    "FINAL": "Final",
}

# Knockout progression order (THIRD_PLACE is off to the side, not a step here).
KO_ORDER = ["R32", "R16", "QF", "SF", "FINAL"]

CSS = """
:root {
  --bg: #14161a;
  --card: #1e2128;
  --text: #e8eaed;
  --muted: #8a919e;
  --accent: #4fc3f7;
  --line: #2c303a;
  --bad: #e57373;
  --warn: #ffb74d;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f5f6f8;
    --card: #ffffff;
    --text: #1b1e24;
    --muted: #5c6470;
    --accent: #0277bd;
    --line: #d9dde4;
    --bad: #c62828;
    --warn: #b26a00;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto;
  padding: 0.75rem;
  max-width: 42rem;
  background: var(--bg);
  color: var(--text);
  font: 16px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
}
h1 { font-size: 1.35rem; margin: 0.25rem 0 0.75rem; }
h2 { font-size: 1.05rem; margin: 1.25rem 0 0.5rem; color: var(--muted);
     text-transform: uppercase; letter-spacing: 0.06em; }
section { background: var(--card); border-radius: 12px; padding: 0.75rem; margin: 0.75rem 0; }
ol, ul { list-style: none; margin: 0; padding: 0; }
.banner {
  background: var(--warn);
  color: #14161a;
  border-radius: 12px;
  padding: 0.6rem 0.75rem;
  font-weight: 600;
  margin: 0.75rem 0;
}
.standing {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.45rem 0.25rem;
  border-bottom: 1px solid var(--line);
}
.standing:last-child { border-bottom: none; }
.standing .rank { color: var(--muted); min-width: 1.5rem; }
.standing .player { font-weight: 600; flex: 1; }
.standing .pts { font-variant-numeric: tabular-nums; font-weight: 700; }
.movement { min-width: 2.5rem; text-align: right; font-variant-numeric: tabular-nums; }
.movement.up { color: #81c784; }
.movement.down { color: var(--bad); }
.movement.same { color: var(--muted); }
.race-chart { width: 100%; height: auto; display: block; }
.no-matches { color: var(--muted); margin: 0.25rem; }
.squad { margin-bottom: 0.9rem; }
.squad:last-child { margin-bottom: 0; }
.squad-head {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  border-bottom: 1px solid var(--line);
  padding-bottom: 0.3rem;
  margin-bottom: 0.35rem;
}
.squad-head .player { font-weight: 700; flex: 1; }
.squad-head .pts { font-weight: 700; font-variant-numeric: tabular-nums; }
.team { display: flex; gap: 0.5rem; align-items: baseline; padding: 0.22rem 0 0.22rem 0.5rem; }
.team .tname { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.team .trec { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 0.85em; }
.team .tpts { font-variant-numeric: tabular-nums; color: var(--muted); min-width: 3.4rem; text-align: right; }
.team.out .tname { text-decoration: line-through; color: var(--muted); }
.team.out .flag { filter: grayscale(1); opacity: 0.6; }
.chip {
  font-size: 0.7em;
  font-weight: 600;
  padding: 0.05rem 0.45rem;
  border-radius: 999px;
  white-space: nowrap;
  background: var(--line);
  color: var(--muted);
}
.chip.live { background: color-mix(in srgb, var(--accent) 22%, transparent); color: var(--accent); }
.chip.champ { background: var(--warn); color: #14161a; }
.bp { display: flex; gap: 0.5rem; align-items: baseline; padding: 0.3rem 0.25rem; }
.bp .player { font-weight: 600; }
.bp .bp-pts { color: var(--muted); }
.badge {
  background: var(--bad);
  color: #fff;
  border-radius: 999px;
  padding: 0.05rem 0.55rem;
  font-size: 0.75em;
  font-weight: 600;
  white-space: nowrap;
}
footer { color: var(--muted); font-size: 0.85rem; padding: 0.5rem 0.25rem 1.5rem; }
footer ul li { padding: 0.1rem 0; }
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _competition_ranks(totals: dict[str, float]) -> dict[str, int]:
    """1224-style competition ranking: rank = 1 + number of strictly better players."""
    return {
        name: 1 + sum(1 for other in totals.values() if other > pts)
        for name, pts in totals.items()
    }


def _movements(standings: dict) -> dict[str, str]:
    """Per-player movement vs. the previous timeline day, rendered as HTML spans."""
    timeline = standings.get("timeline") or []
    prev_ranks: dict[str, int] = {}
    if len(timeline) >= 2:
        prev_ranks = _competition_ranks(timeline[-2].get("totals") or {})
    out: dict[str, str] = {}
    for player in standings.get("players", []):
        name = player["name"]
        prev = prev_ranks.get(name)
        delta = (prev - player["rank"]) if prev is not None else 0
        if delta > 0:
            out[name] = f'<span class="movement up">▲{delta}</span>'
        elif delta < 0:
            out[name] = f'<span class="movement down">▼{-delta}</span>'
        else:
            out[name] = '<span class="movement same">–</span>'
    return out


def _render_standings(standings: dict) -> str:
    movements = _movements(standings)
    rows = []
    for player in standings.get("players", []):
        name = player["name"]
        rows.append(
            f'<li class="standing" data-player="{_esc(name)}">'
            f'<span class="rank">{player["rank"]}</span>'
            f'<span class="player">{_esc(name)}</span>'
            f"{movements[name]}"
            f'<span class="pts">{player["points"]} pts</span>'
            "</li>"
        )
    body = "\n".join(rows) or '<li class="standing">No players yet.</li>'
    return f"<section>\n<h2>Standings</h2>\n<ol>\n{body}\n</ol>\n</section>"


def _player_colors(standings: dict) -> dict[str, str]:
    return {
        p["name"]: PALETTE[i % len(PALETTE)]
        for i, p in enumerate(standings.get("players", []))
    }


def _render_chart(standings: dict) -> str:
    timeline = standings.get("timeline") or []
    players = [p["name"] for p in standings.get("players", [])]
    if not timeline or not players:
        return (
            "<section>\n<h2>Race</h2>\n"
            '<p class="no-matches">No matches yet &mdash; the race chart appears after '
            "the first final whistle.</p>\n</section>"
        )

    width, height = 360, 210
    pad_l, pad_r, pad_t, pad_b = 30, 64, 12, 26
    plot_w, plot_h = width - pad_l - pad_r, height - pad_t - pad_b

    # Per-player cumulative series, carrying the last value through missing days.
    series: dict[str, list[float]] = {name: [] for name in players}
    for entry in timeline:
        totals = entry.get("totals") or {}
        for name in players:
            prev = series[name][-1] if series[name] else 0
            series[name].append(totals.get(name, prev))

    max_pts = max((v for vals in series.values() for v in vals), default=0) or 1
    n = len(timeline)

    def x(i: int) -> float:
        return pad_l + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y(v: float) -> float:
        return pad_t + plot_h * (1 - v / max_pts)

    colors = _player_colors(standings)
    parts = []
    for name in players:
        color = colors[name]
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series[name]))
        last_x, last_y = x(n - 1), y(series[name][-1])
        parts.append(
            f'<polyline class="race-line" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round" points="{pts}"/>'
        )
        parts.append(
            f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2.5" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{last_x + 5:.1f}" y="{last_y + 3:.1f}" fill="{color}" '
            f'font-size="10">{_esc(name)}</text>'
        )

    axis_color = "var(--muted)"
    first_date, last_date = timeline[0]["date"], timeline[-1]["date"]
    axis = [
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + plot_h}" '
        f'stroke="{axis_color}" stroke-width="1"/>',
        f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{pad_l + plot_w}" '
        f'y2="{pad_t + plot_h}" stroke="{axis_color}" stroke-width="1"/>',
        f'<text x="{pad_l - 4}" y="{pad_t + 4}" fill="{axis_color}" font-size="9" '
        f'text-anchor="end">{max_pts:g}</text>',
        f'<text x="{pad_l - 4}" y="{pad_t + plot_h + 4}" fill="{axis_color}" '
        f'font-size="9" text-anchor="end">0</text>',
        f'<text x="{pad_l}" y="{height - 6}" fill="{axis_color}" '
        f'font-size="9">{_esc(first_date)}</text>',
    ]
    if n > 1:
        axis.append(
            f'<text x="{pad_l + plot_w}" y="{height - 6}" fill="{axis_color}" '
            f'font-size="9" text-anchor="end">{_esc(last_date)}</text>'
        )

    svg_body = "\n".join(axis + parts)
    return (
        "<section>\n<h2>Race</h2>\n"
        f'<svg class="race-chart" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="Cumulative points per player per day">\n'
        f"{svg_body}\n</svg>\n</section>"
    )


def _progress(team: dict) -> tuple[str, str]:
    """(css state, compact chip label) for the round a team reached.

    The group record freezes after three matches, so this chip carries the
    knockout story: which round they're alive in, where they were knocked out,
    or the trophy. Points are always shown separately and keep counting.
    """
    ko_wins = team.get("ko_wins") or []
    eliminated_at = team.get("eliminated_at")
    if "FINAL" in ko_wins:
        return "champ", "🏆 Champion"
    if eliminated_at is not None:
        return "out", STAGE_SHORT.get(eliminated_at, eliminated_at)
    if team.get("advanced"):
        # Furthest round reached = the round after the last one they won.
        last = max((KO_ORDER.index(s) for s in ko_wins if s in KO_ORDER), default=-1)
        nxt = KO_ORDER[min(last + 1, len(KO_ORDER) - 1)]
        return "live", STAGE_SHORT.get(nxt, nxt)
    return "group", "Groups"


def _render_team(team: dict, teams: dict) -> str:
    code = team["code"]
    info = teams.get(code) or {}
    flag = info.get("flag", "\U0001f3f3️")  # white flag for unknown codes
    name = info.get("name", code)
    rec = team.get("group_record") or {}
    record = f'{rec.get("w", 0)}-{rec.get("d", 0)}-{rec.get("l", 0)}'
    state, label = _progress(team)
    classes = "team out" if team.get("eliminated_at") is not None else "team"
    chip_class = "chip" if state in ("out", "group") else f"chip {state}"
    return (
        f'<li class="{classes}" data-team="{_esc(code)}">'
        f'<span class="flag">{flag}</span>'
        f'<span class="tname">{_esc(name)}</span>'
        f'<span class="trec" title="group W-D-L">{record}</span>'
        f'<span class="{chip_class}">{_esc(label)}</span>'
        f'<span class="tpts">{team["points"]} pts</span>'
        "</li>"
    )


def _render_squads(standings: dict, teams: dict) -> str:
    cards = []
    for player in standings.get("players", []):
        team_rows = "\n".join(_render_team(t, teams) for t in player.get("teams", []))
        cards.append(
            f'<li class="squad" data-player="{_esc(player["name"])}">'
            f'<div class="squad-head"><span class="player">{_esc(player["name"])}</span>'
            f'<span class="pts">{player["points"]} pts</span></div>'
            f"<ul>\n{team_rows}\n</ul></li>"
        )
    body = "\n".join(cards) or "<li>No players yet.</li>"
    return f"<section>\n<h2>Teams</h2>\n<ul>\n{body}\n</ul>\n</section>"


def _render_best_possible(standings: dict) -> str:
    players = standings.get("players", [])
    leader_points = max((p["points"] for p in players), default=0)
    rows = []
    for player in players:
        badge = ""
        if player["best_possible"] < leader_points:
            badge = ' <span class="badge">mathematically eliminated</span>'
        rows.append(
            f'<li class="bp" data-player="{_esc(player["name"])}">'
            f'<span class="player">{_esc(player["name"])}</span>'
            f'<span class="bp-pts">best possible {player["best_possible"]} pts</span>'
            f"{badge}</li>"
        )
    body = "\n".join(rows) or "<li>No players yet.</li>"
    return f"<section>\n<h2>Best possible</h2>\n<ul>\n{body}\n</ul>\n</section>"


def _render_footer(standings: dict, pool: dict, matches: dict | None) -> str:
    scoring = pool.get("scoring", {})
    lines = []
    if "group_win" in scoring or "group_draw" in scoring:
        lines.append(
            f"Group stage: win {scoring.get('group_win', 0)} pts, "
            f"draw {scoring.get('group_draw', 0)} pt"
        )
    if "advance" in scoring:
        lines.append(f"Advance to knockouts: {scoring['advance']} pts")
    stage_points = scoring.get("stage_win_points") or {}
    if stage_points:
        per_stage = " &middot; ".join(
            f"{_esc(STAGE_LABELS.get(stage, stage))} {pts}"
            for stage, pts in stage_points.items()
        )
        lines.append(f"Knockout win: {per_stage}")
    if pool.get("third_place_final"):
        lines.append(f"Third-place final win: {scoring.get('third_place_win', 0)} pts")
    rules = "\n".join(f"<li>{line}</li>" for line in lines)

    matches_line = ""
    if matches is not None:
        all_matches = matches.get("matches", [])
        finished = sum(1 for m in all_matches if m.get("status") == "FINISHED")
        matches_line = f"<p>{finished} of {len(all_matches)} matches played.</p>\n"

    generated_at = standings.get("generated_at", "unknown")
    return (
        "<footer>\n"
        f"<p>Scoring &mdash; <ul>\n{rules}\n</ul></p>\n"
        f"{matches_line}"
        f"<p>Generated at {_esc(generated_at)}.</p>\n"
        "</footer>"
    )


def render_page(standings: dict, pool: dict, teams: dict, matches: dict | None) -> str:
    """Render the full dashboard HTML from already-parsed data objects."""
    banner = ""
    if pool.get("placeholder"):
        banner = (
            '<div class="banner" role="alert">Placeholder picks — draft pending. '
            "Standings below are not final.</div>\n"
        )
    pool_name = pool.get("pool_name", "Prediction pool")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        f"<title>{_esc(pool_name)} &mdash; World Cup 2026</title>\n"
        f"<style>{CSS}</style>\n"
        "</head>\n<body>\n"
        f"{banner}"
        f"<h1>{_esc(pool_name)}</h1>\n"
        f"{_render_standings(standings)}\n"
        f"{_render_chart(standings)}\n"
        f"{_render_squads(standings, teams)}\n"
        f"{_render_best_possible(standings)}\n"
        f"{_render_footer(standings, pool, matches)}\n"
        "</body>\n</html>\n"
    )


def build_site(data_dir: Path | str, out_dir: Path | str) -> Path:
    """Build ``index.html`` under *out_dir* from the JSON files in *data_dir*.

    ``standings.json``, ``pool.json`` and ``teams.json`` are required;
    ``matches.json`` is optional (its absence only drops the matches-played line).
    Returns the path of the written file.
    """
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    standings = _load_json(data_dir / "standings.json")
    pool = _load_json(data_dir / "pool.json")
    teams = _load_json(data_dir / "teams.json")
    matches_path = data_dir / "matches.json"
    matches = _load_json(matches_path) if matches_path.exists() else None

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "index.html"
    out_path.write_text(render_page(standings, pool, teams, matches), encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m wcpool.site",
        description="Build the static standings dashboard.",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("site"))
    args = parser.parse_args(argv)
    out_path = build_site(args.data_dir, args.out)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
