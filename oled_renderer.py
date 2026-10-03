"""OLED bitmap renderer for GameSense screens.

Renders a full-screen 1-bit frame sized exactly to the device resolution, in
the byte format SteelSeries Engine expects:

    * one bit per pixel, 1 = white / 0 = black
    * MSB first, row by row, origin top-left
    * total bytes == ceil(width * height / 8)

Layout: an animated "AI writing code" panel on the left (a bot face above a
terminal that types short snippets character-by-character with a blinking
cursor), plus up to four text lines stacked on the right. The animation is a
pure function of an integer `tick` advanced by the caller, so each frame is
deterministic and stateless.

Zero dependencies (Python 3.8+ stdlib). Text uses a built-in 5x7 font; the
optional ChatGPT mark (`render_logo`) is still rasterized from the official
favicon SVG path when available.

Usage:
    import oled_renderer as oled
    frame = oled.compose_frame(["5H maxed", "WK left 69%"], 128, 48, tick=7)
"""

from __future__ import annotations

import math
import os
import re
from functools import lru_cache

# ---------------------------------------------------------------------------
# 5x7 bitmap font (each glyph: 7 rows of 5 chars, '1' = pixel on).
# Enough for the strings this app displays; unknown glyphs render as space.
# ---------------------------------------------------------------------------

FONT = {
    " ": ("00000",) * 7,
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11100", "10010", "10001", "10001", "10001", "10010", "11100"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000",) * 6 + ("11111",),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110",) * 2 + ("10001",) * 4 + ("01110",),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111",) + ("00100",) * 6,
    "U": ("10001",) * 6 + ("01110",),
    "V": ("10001",) * 4 + ("01010", "01010", "00100"),
    "W": ("10001",) * 3 + ("10101",) * 3 + ("01010",),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100",) + ("00100",) * 3,
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100",) + ("01000",) * 3,
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    ".": ("00000",) * 5 + ("01100", "01100"),
    ",": ("00000",) * 5 + ("01100", "01000"),
    ":": ("00100", "00100", "00000") + ("00100", "00100", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    "%": ("10010", "10001", "00010", "00100", "01000", "10001", "10010"),
    "-": ("00000",) * 3 + ("11111",) + ("00000",) * 3,
    "+": ("00100", "00100", "00100", "11111", "00100", "00100", "00100"),
    "?": ("01110", "10001", "00001", "00110", "00100", "00000", "00100"),
    "!": ("00100",) * 5 + ("00000", "00100"),
    ">": ("10000", "01000", "00100", "00010", "00100", "01000", "10000"),
}

GLYPH_W = 5
GLYPH_H = 7
CHAR_PITCH = GLYPH_W + 1  # one blank column between glyphs


# ---------------------------------------------------------------------------
# SVG path parsing / rasterization (official ChatGPT mark)
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.\d*|\d+)(?:[eE][-+]?\d+)?")
_CMD_RE = re.compile(r"([A-Za-z])|([^A-Za-z]+)")


def _path_to_polylines(d: str) -> list[list[tuple[float, float]]]:
    """Parse an SVG path 'd' string into a list of closed polylines.

    Supports M/L/H/V/C/Z (absolute and relative). Beziers are sampled into
    16 straight segments each, which is plenty at OLED resolutions.
    """
    polys: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    cx = cy = sx = sy = 0.0  # current point / subpath start

    def flush():
        nonlocal cur
        if len(cur) > 1:
            polys.append(list(cur))
        cur = []

    pos = 0
    while pos < len(d):
        m = _CMD_RE.match(d, pos)
        if not m:
            break
        if m.group(2) is not None:
            # stray whitespace / numbers before a command - skip.
            pos = m.end()
            continue
        cmd = m.group(1)
        rel = cmd.islower()
        cmd_u = cmd.upper()

        # consume all numeric arguments belonging to this command (this also
        # covers implicit repetition, e.g. "C x1 y1 ... x6 y6 C ..." without a
        # repeated letter).
        args: list[float] = []
        p2 = m.end()
        while p2 < len(d):
            nxt = _CMD_RE.match(d, p2)
            if not nxt or nxt.group(1) is not None:
                break
            args.extend(float(x) for x in _NUM_RE.findall(nxt.group(2)))
            p2 = nxt.end()
        pos = p2

        i = 0
        if cmd_u == "M":
            while i + 1 < len(args):
                x, y = args[i], args[i + 1]
                i += 2
                if rel:
                    x += cx
                    y += cy
                flush()
                cx, cy = sx, sy = x, y
                cur.append((x, y))
        elif cmd_u == "L":
            while i + 1 < len(args):
                x, y = args[i] + (cx if rel else 0), args[i + 1] + (cy if rel else 0)
                i += 2
                cx, cy = x, y
                cur.append((x, y))
        elif cmd_u == "H":
            while i < len(args):
                x = args[i] + (cx if rel else 0)
                i += 1
                cx = x
                cur.append((x, cy))
        elif cmd_u == "V":
            while i < len(args):
                y = args[i] + (cy if rel else 0)
                i += 1
                cy = y
                cur.append((cx, y))
        elif cmd_u == "C":
            while i + 5 < len(args):
                ox, oy = cx, cy
                x1 = args[i] + (ox if rel else 0)
                y1 = args[i + 1] + (oy if rel else 0)
                x2 = args[i + 2] + (ox if rel else 0)
                y2 = args[i + 3] + (oy if rel else 0)
                xe = args[i + 4] + (ox if rel else 0)
                ye = args[i + 5] + (oy if rel else 0)
                i += 6
                for s in range(1, 17):
                    t = s / 16.0
                    u = 1.0 - t
                    px = u ** 3 * ox + 3 * u ** 2 * t * x1 + 3 * u * t ** 2 * x2 + t ** 3 * xe
                    py = u ** 3 * oy + 3 * u ** 2 * t * y1 + 3 * u * t ** 2 * y2 + t ** 3 * ye
                    cur.append((px, py))
                cx, cy = xe, ye
        elif cmd_u == "Z":
            if (cx, cy) != (sx, sy):
                cur.append((sx, sy))
            flush()
        # unsupported commands (Q/A/S/T...) have no args consumed here; the
        # outer loop advances past them on the next iteration.

    return polys


def _even_odd_fill(polys: list[list[tuple[float, float]]], size: int) -> list[bool]:
    """Even-odd fill of the combined subpaths into a `size`x`size` bitmap.

    Uses 3x supersampling for smooth edges and bounding-box pre-filtering of
    segments per scanline (this is what keeps it fast in pure Python).
    """
    # scale into [0, size] canvas space
    xs = [p[0] for poly in polys for p in poly]
    ys = [p[1] for poly in polys for p in poly]
    if not xs or max(xs) - min(xs) < 1e-6:
        return [False] * (size * size)
    ext = max(max(xs), max(ys))
    scale = (size - 0.5) / ext

    # collect segments with bbox, in canvas space
    segs: list[tuple[float, float, float, float, tuple[float, float], tuple[float, float]]] = []
    for poly in polys:
        scaled = [(x * scale, y * scale) for x, y in poly]
        pts = scaled + [scaled[0]]  # close the subpath
        for a, b in zip(pts[:-1], pts[1:]):
            (x1, y1), (x2, y2) = a, b
            if x1 == x2 and y1 == y2:
                continue
            segs.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2), a, b))

    SS = 3
    N = size * SS
    grid: list[bool] = [False] * (N * N)
    for sy in range(N):
        py = (sy + 0.5) / SS
        row_segs = []
        for xmin, ymin, xmax, ymax, a, b in segs:
            if ymin <= py < ymax or ymax <= py < ymin:
                x1, y1 = a
                x2, y2 = b
                # x crossing at this scanline
                xc = x1 + (py - y1) * ((x2 - x1) / (y2 - y1)) if y2 != y1 else x1
                row_segs.append(xc)
        for sx in range(N):
            px = (sx + 0.5) / SS
            crossings = sum(1 for xc in row_segs if xc > px)
            grid[sy * N + sx] = (crossings & 1) == 1

    # downsample with a soft threshold
    out: list[bool] = [False] * (size * size)
    for y in range(size):
        for x in range(size):
            total = 0
            for dy in range(SS):
                base = ((y * SS + dy) * N) + (x * SS)
                for dx in range(SS):
                    if grid[base + dx]:
                        total += 1
            out[y * size + x] = total >= 4  # >~40% of supersampled pixels
    return out


@lru_cache(maxsize=8)
def render_logo(size: int, svg_path: str | None = None) -> list[bool]:
    """Rasterize the ChatGPT mark from chatgpt-logo.svg to a `size`x`size` grid."""
    if not svg_path or not os.path.exists(svg_path):
        raise FileNotFoundError("chatgpt-logo.svg not found")
    with open(svg_path, "r", encoding="utf-8-sig") as fh:
        content = fh.read()

    m = re.search(r'd="([^"]+)"', content)
    if not m:
        raise ValueError("no SVG path (d=...) in logo file")
    # find the viewBox to normalize aspect correctly (logo is square, but be safe)
    vb = re.search(r'viewBox="([^"]+)"', content)
    d = m.group(1)

    polys = _path_to_polylines(d)
    if not polys:
        raise ValueError("SVG path parsed to no subpaths")

    # trim each polyline's bounding box, then fit into the square canvas
    minx = min(p[0] for poly in polys for p in poly)
    miny = min(p[1] for poly in polys for p in poly)
    maxx = max(p[0] for poly in polys for p in poly)
    maxy = max(p[1] for poly in polys for p in poly)
    w, h = maxx - minx, maxy - miny
    if vb:
        parts = [float(x) for x in re.findall(r"[-+]?(?:\d*\.\d+|\d+\.\d*|\d+)", vb.group(1))]
        if len(parts) == 4 and (parts[2] > w or parts[3] > h):
            # path is a subregion of the viewBox; keep its own bbox instead
            pass

    polys = [[((x - minx), (y - miny)) for x, y in poly] for poly in polys]
    bitmap = _even_odd_fill(polys, size)
    if not any(bitmap):
        raise ValueError("logo rasterized to an empty bitmap")
    return bitmap


# ---------------------------------------------------------------------------
# Text + frame composition
# ---------------------------------------------------------------------------

def draw_text(grid: list[int], W: int, H: int, text: str, x0: int, y_top: int, pitch: int = CHAR_PITCH) -> None:
    """Blit `text` (5x7 font) into a flat 0/1 row-major grid."""
    for col, ch in enumerate(text):
        glyph = FONT.get(ch.upper()) or FONT.get(ch) or FONT[" "]
        gx = x0 + col * pitch
        if gx + GLYPH_W > W:
            break
        for r in range(GLYPH_H):
            rowbits = glyph[r]
            c = y_top + r
            if 0 <= c < H:
                for b in range(GLYPH_W):
                    if rowbits[b] == "1" and gx + b < W:
                        grid[c * W + (gx + b)] = 1


# ---------------------------------------------------------------------------
# "AI writing code" animation (left panel, replaces the old logo)
# ---------------------------------------------------------------------------

# Short pseudo-code snippets typed character by character on loop. Each line is
# at most 7 glyphs wide so it fits the ~40px panel at 5px pitch with no spacing.
SNIPPETS: list[list[str]] = [
    ["think..", "code()"],
    ["> build", "done!"],
    ["run ai", "pass!"],
    ["100% ok", "ship!"],
]

HOLD_TICKS = 16   # frames to keep a finished snippet on screen (cursor blinks)
BLANK_TICKS = 4   # short pause between snippets


def _build_anim_seq() -> list[tuple[int, int, bool]]:
    """Flatten the whole animation into a periodic table of (snippet, chars_typed, holding)."""
    seq: list[tuple[int, int, bool]] = []
    for i, snip in enumerate(SNIPPETS):
        total = sum(len(line) for line in snip)
        for c in range(total + 1):          # typing phase, one char per tick
            seq.append((i, c, False))
        seq += [(i, total, True)] * HOLD_TICKS
        seq += [(-1, 0, False)] * BLANK_TICKS
    return seq


_ANIM_SEQ = _build_anim_seq()


def anim_state(tick: int) -> tuple[int, int, bool]:
    """Animation state for a given tick index.

    `tick < 0` is the "hero" frame (first snippet fully typed) used as the
    bind-time placeholder. Returns (snippet_index, chars_typed, holding).
    """
    if tick is None or tick < 0:
        return 0, sum(len(line) for line in SNIPPETS[0]), False
    entry = _ANIM_SEQ[tick % len(_ANIM_SEQ)]
    return entry


def demo_tick() -> int:
    """A tick where the first snippet is mid-typing (for `--once` previews)."""
    snip = SNIPPETS[0]
    if len(snip) >= 2:
        return len(snip[0]) + min(4, max(1, len(snip[1]) - 1))
    return max(1, sum(len(line) for line in snip) // 2)


def _typing_state(snippet: list[str], typed: int) -> tuple[list[str], int, int]:
    """Split `typed` characters across the snippet's lines.

    Returns (visible_partial_lines, cursor_line_index, cursor_column).
    """
    visible: list[str] = []
    rem = min(typed, sum(len(line) for line in snippet))
    for i, line in enumerate(snippet):
        if rem <= 0:
            break
        take = min(len(line), rem)
        visible.append(line[:take])
        if take < len(line):
            return visible, i, take
        rem -= len(line)
    # nothing typed yet -> cursor at the very start; all typed -> end of last line
    if not visible:
        return [], 0, 0
    last = max(0, len(snippet) - 1)
    return snippet[:last + 1], last, len(snippet[last])


def _draw_ai_panel(grid: list[int], W: int, H: int, ax: int, aw: int, tick: int) -> None:
    """Draw the bot face plus the terminal panel with typed code and cursor."""
    snip_idx, typed, holding = anim_state(tick)

    # fixed layout metrics (all snippets are exactly two lines)
    face_w, face_h = 8, 7
    line_step = GLYPH_H + 2                      # vertical pitch for code lines
    block_h = face_h + 5 + 2 * line_step         # face + gap + two code lines
    top = max(1, (H - block_h) // 2)
    x0 = ax + 3                                  # left padding inside the panel

    # -- bot face: filled square head, two eye holes, one antenna pixel ----
    fy = top
    for y in range(face_h):
        row = (fy + y) * W
        for x in range(face_w):
            if x0 + x < W:
                grid[row + x0 + x] = 1
    eyes_y = fy + 3
    grid[eyes_y * W + x0 + 2] = 0
    grid[eyes_y * W + x0 + 5] = 0
    if top >= 2:
        grid[(top - 1) * W + x0 + 4] = 1         # antenna

    # -- typed code lines ----------------------------------------------------
    cy0 = fy + face_h + 5
    if snip_idx >= 0 and 0 <= snip_idx < len(SNIPPETS):
        snippet = SNIPPETS[snip_idx]
        visible, cur_line, cur_col = _typing_state(snippet, typed)
        for i, text in enumerate(visible):
            draw_text(grid, W, H, text, x0, cy0 + i * line_step, pitch=GLYPH_W)

        # block cursor at the typing position (blinks while holding)
        cursor_on = (not holding) or ((tick % 6) < 4)
        if cursor_on:
            cx = x0 + cur_col * GLYPH_W
            cy = cy0 + cur_line * line_step
            for y in range(GLYPH_H):
                if 0 <= cy + y < H:
                    row = (cy + y) * W
                    for x in range(GLYPH_W):
                        if cx + x < W and cx + x < ax + aw:
                            grid[row + cx + x] = 1


def compose_frame(lines: list[str], W: int, H: int, tick: int = 0) -> list[int]:
    """Compose the full-screen frame bytes for a WxH OLED.

    Layout: animated "AI writing code" panel on the left (driven by `tick`),
    up to four text lines stacked on the right. Returns ceil(W*H/8) bytes,
    MSB-first per row. On very small screens (<100px wide or <32px tall) the
    panel is dropped and only the text is shown.
    """
    grid = [0] * (W * H)

    have_anim = W >= 100 and H >= 32
    ax, aw = 2, min(40, max(28, W // 3))
    text_x = ax + aw + 8 if have_anim else 4

    if have_anim:
        _draw_ai_panel(grid, W, H, ax, aw, tick)

    # -- text block (up to 4 lines, auto-fit spacing) ------------------------
    nonempty = [str(l).strip() for l in lines if str(l).strip()]
    n_lines = min(4, max(1, len(nonempty)))
    gap, total_h = 3, H - 4
    for g in (3, 2, 1):
        candidate = n_lines * GLYPH_H + (n_lines - 1) * g
        if candidate <= H - 4:
            gap, total_h = g, candidate
            break
    y0 = max(1, (H - total_h) // 2)

    avail = W - text_x - 2
    pitch = CHAR_PITCH
    shown_lines = [str(l)[:max(1, avail // pitch)] for l in (lines + [""] * 4)[:4]]
    if any(len(str(l)) > len(s) for l, s in zip(lines[:4], shown_lines)):
        pitch = GLYPH_W  # tight pitch fallback so longer lines can still fit
        shown_lines = [str(l)[:max(1, avail // pitch)] for l in (lines + [""] * 4)[:4]]

    idx = 0
    for line in shown_lines:
        if not str(line).strip():
            continue
        draw_text(grid, W, H, str(line), text_x, y0 + idx * (GLYPH_H + gap), pitch=pitch)
        idx += 1

    # pack to bytes (MSB first per row, origin top-left)
    out: list[int] = []
    for y in range(H):
        bits = grid[y * W:(y + 1) * W]
        for x in range(0, len(bits), 8):
            byte = 0
            for b in range(min(8, len(bits) - x)):
                if bits[x + b]:
                    byte |= 1 << (7 - b)
            out.append(byte & 0xFF)
    return out[: math.ceil(W * H / 8)]


def ascii_preview(frame: list[int], W: int, H: int) -> str:
    """Render packed frame bytes as ASCII art for console inspection."""
    lines = []
    for y in range(H):
        row = ""
        for x in range(W):
            byte_i = (y * W + x) // 8
            if byte_i >= len(frame):
                break
            bit = (frame[byte_i] >> (7 - ((y * W + x) % 8))) & 1
            row += "#" if bit else "."
        lines.append(row)
    return "\n".join(lines)
