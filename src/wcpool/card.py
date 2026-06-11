"""Share-card PNG renderer for the group text (issue #5).

Renders a portrait 1080x1350 PNG from ``standings.json`` + ``pool.json`` +
``teams.json`` (schemas in docs/CONTRACTS.md). Pillow is the only non-stdlib
dependency, explicitly allowed for this module by the dependency policy.

ASCII/Latin only: default-available fonts do not reliably render emoji or
glyphs like a solid triangle, so movement markers are "+2" / "-1" / "=" and
teams are shown by FIFA code.

CLI: ``python -m wcpool.card --data-dir data --out site/card.png``
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH = 1080
HEIGHT = 1350

BG = (14, 17, 22)
FG = (235, 238, 242)
DIM = (140, 148, 158)
HOOK_COLOR = (255, 200, 60)
UP_COLOR = (80, 200, 120)
DOWN_COLOR = (235, 100, 100)
WATERMARK_COLOR = (255, 255, 255, 28)

# Accent color per player, indexed by rank (consistent run to run).
ACCENTS = [
    (255, 196, 0),  # 1: gold
    (170, 180, 195),  # 2: silver
    (205, 127, 50),  # 3: bronze
    (90, 170, 255),
    (255, 120, 180),
    (120, 220, 200),
    (200, 160, 255),
    (255, 160, 90),
]

_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_BOLD_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Load a system DejaVu font; fall back to PIL's default font."""
    for path in _BOLD_FONT_PATHS if bold else _FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow: no size argument
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested independently of rendering)
# ---------------------------------------------------------------------------


def matchday_label(standings: dict) -> str:
    """Header subtitle: last timeline date, or pre-tournament when empty."""
    timeline = standings.get("timeline") or []
    if not timeline:
        return "Pre-tournament"
    return f"Matchday {timeline[-1]['date']}"


def _leaders(totals: dict) -> set:
    top = max(totals.values())
    return {name for name, pts in totals.items() if pts == top}


def movement_markers(standings: dict) -> dict:
    """Per-player movement vs the previous timeline day: "+2", "-1" or "="."""
    players = standings.get("players") or []
    timeline = standings.get("timeline") or []
    if len(timeline) < 2:
        return {p["name"]: "=" for p in players}
    prev = timeline[-2]["totals"]
    markers = {}
    for p in players:
        prev_pts = prev.get(p["name"], 0)
        prev_rank = 1 + sum(1 for v in prev.values() if v > prev_pts)
        delta = prev_rank - p["rank"]
        markers[p["name"]] = f"{delta:+d}" if delta else "="
    return markers


def pick_hook(standings: dict) -> str:
    """Pick the one-line drama hook for the card.

    Priority:
      a) lead change on the latest timeline day -> "X takes the lead!"
      b) a player mathematically eliminated (best_possible < leader's
         points) -> "X is mathematically eliminated"
      c) closest gap between adjacent ranks -> "X trails by N"

    Tie-breaks: for (a) the best-ranked new leader; for (b) the best-ranked
    eliminated player; for (c) the topmost of the equally-close pairs.
    """
    players = sorted(standings.get("players") or [], key=lambda p: p["rank"])
    if not players:
        return "The tournament awaits"
    if len(players) == 1:
        return f"{players[0]['name']} leads the pool"

    # a) lead change on the latest day
    timeline = standings.get("timeline") or []
    if len(timeline) >= 2:
        curr = _leaders(timeline[-1]["totals"])
        prev = _leaders(timeline[-2]["totals"])
        if curr.isdisjoint(prev):
            for p in players:  # best-ranked new leader
                if p["name"] in curr:
                    return f"{p['name']} takes the lead!"

    # b) mathematical elimination
    leader_points = players[0]["points"]
    for p in players:  # rank order = best-ranked eliminated player first
        if p["best_possible"] < leader_points:
            return f"{p['name']} is mathematically eliminated"

    # c) closest gap between adjacent ranks (topmost pair wins ties)
    best_pair = min(
        zip(players, players[1:]),
        key=lambda pair: pair[0]["points"] - pair[1]["points"],
    )
    gap = best_pair[0]["points"] - best_pair[1]["points"]
    return f"{best_pair[1]['name']} trails by {gap}"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _team_codes(player: dict) -> str:
    parts = []
    for team in player.get("teams") or []:
        code = team["code"]
        parts.append(code if team.get("alive", True) else f"({code})")
    return " ".join(parts)


def _draw_watermark(img: Image.Image) -> None:
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font(110, bold=True)
    text = "PLACEHOLDER PICKS"
    w = draw.textlength(text, font=font)
    draw.text(((WIDTH - w) / 2, HEIGHT / 2 - 60), text, font=font, fill=WATERMARK_COLOR)
    overlay = overlay.rotate(20, center=(WIDTH / 2, HEIGHT / 2))
    img.alpha_composite(overlay)


def render_card(standings: dict, pool: dict, teams: dict, out_path: Path) -> Path:
    """Render the share card and write it to ``out_path``. Returns the path."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), BG + (255,))
    draw = ImageDraw.Draw(img)
    margin = 60

    # Header
    title_font = _load_font(68, bold=True)
    sub_font = _load_font(38)
    draw.text((margin, 64), str(pool.get("pool_name", "World Cup Pool")), font=title_font, fill=FG)
    draw.text((margin, 158), matchday_label(standings), font=sub_font, fill=DIM)
    draw.line([(margin, 226), (WIDTH - margin, 226)], fill=DIM, width=2)

    # Standings rows
    players = sorted(standings.get("players") or [], key=lambda p: p["rank"])
    markers = movement_markers(standings)
    top, bottom = 260, 1180
    row_h = min(120, (bottom - top) // max(1, len(players)))
    name_font = _load_font(max(22, int(row_h * 0.38)), bold=True)
    small_font = _load_font(max(16, int(row_h * 0.22)))
    move_font = _load_font(max(18, int(row_h * 0.30)), bold=True)

    for i, p in enumerate(players):
        y = top + i * row_h
        accent = ACCENTS[(p["rank"] - 1) % len(ACCENTS)]
        draw.rectangle([(margin, y + 6), (margin + 10, y + row_h - 6)], fill=accent)
        draw.text((margin + 32, y + int(row_h * 0.12)), f"{p['rank']}.", font=name_font, fill=accent)
        draw.text((margin + 110, y + int(row_h * 0.12)), str(p["name"]), font=name_font, fill=FG)
        codes = _team_codes(p)
        if codes:
            draw.text((margin + 110, y + int(row_h * 0.58)), codes, font=small_font, fill=DIM)

        pts = f"{p['points']} pts"
        pts_w = draw.textlength(pts, font=name_font)
        draw.text((WIDTH - margin - 140 - pts_w, y + int(row_h * 0.12)), pts, font=name_font, fill=FG)
        best = f"best {p['best_possible']}"
        best_w = draw.textlength(best, font=small_font)
        draw.text(
            (WIDTH - margin - 140 - best_w, y + int(row_h * 0.58)),
            best,
            font=small_font,
            fill=DIM,
        )

        marker = markers.get(p["name"], "=")
        color = UP_COLOR if marker.startswith("+") else DOWN_COLOR if marker.startswith("-") else DIM
        mark_w = draw.textlength(marker, font=move_font)
        draw.text((WIDTH - margin - mark_w, y + int(row_h * 0.22)), marker, font=move_font, fill=color)

    # Drama hook
    hook_font = _load_font(44, bold=True)
    hook = pick_hook(standings)
    hook_w = draw.textlength(hook, font=hook_font)
    draw.text(((WIDTH - hook_w) / 2, 1240), hook, font=hook_font, fill=HOOK_COLOR)

    if pool.get("placeholder"):
        _draw_watermark(img)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, format="PNG")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wcpool.card", description="Render the share-card PNG.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("site/card.png"))
    args = parser.parse_args(argv)

    standings = _load_json(args.data_dir / "standings.json")
    pool = _load_json(args.data_dir / "pool.json")
    teams = _load_json(args.data_dir / "teams.json")
    out = render_card(standings, pool, teams, args.out)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
