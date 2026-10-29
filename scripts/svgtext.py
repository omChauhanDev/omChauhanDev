"""Set text with HarfBuzz and write it out as SVG outlines.

GitHub shows README images through a proxy that won't load web fonts, so the
cards carry their type as paths. Everyone sees the same Geist, kerning included.
Each glyph is drawn once per file and placed with <use>, which keeps files small.
"""

from __future__ import annotations

from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont


def num(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class Font:
    def __init__(self, path: Path, key: str):
        self.key = key
        data = path.read_bytes()
        self._hb = hb.Font(hb.Face(data))
        tt = TTFont(path)
        self._glyphs = tt.getGlyphSet()
        self._order = tt.getGlyphOrder()
        self._hmtx = tt["hmtx"]
        self._glyf = tt["glyf"]
        self.upem = tt["head"].unitsPerEm
        self._outlines: dict[int, str] = {}

    def _shape(self, text: str):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self._hb, buf, {"kern": True, "liga": True})
        return buf.glyph_infos, buf.glyph_positions

    def outline(self, gid: int) -> str:
        """Glyph outline in font units, y pointing down."""
        if gid not in self._outlines:
            pen = SVGPathPen(self._glyphs, ntos=num)
            self._glyphs[self._order[gid]].draw(TransformPen(pen, (1, 0, 0, -1, 0, 0)))
            self._outlines[gid] = pen.getCommands()
        return self._outlines[gid]

    def width(self, text: str, size: float, tracking: float = 0.0) -> float:
        infos, positions = self._shape(text)
        return sum(p.x_advance for p in positions) * size / self.upem + tracking * max(len(infos) - 1, 0)

    def left_bearing(self, text: str, size: float) -> float:
        """Distance from the pen position to the first glyph's ink, for optical alignment."""
        infos, _ = self._shape(text)
        return self._hmtx[self._order[infos[0].codepoint]][1] * size / self.upem if infos else 0.0

    def right_bearing(self, text: str, size: float) -> float:
        """Distance from the last glyph's ink to the end of its advance."""
        infos, _ = self._shape(text)
        if not infos:
            return 0.0
        name = self._order[infos[-1].codepoint]
        advance = self._hmtx[name][0]
        x_max = getattr(self._glyf[name], "xMax", advance)
        return (advance - x_max) * size / self.upem

    def place(self, text: str, size: float, x: float, y: float,
              tracking: float = 0.0, anchor: str = "start") -> tuple[list[tuple[int, float, float]], float]:
        """Return (glyph id, x, y) for each glyph of `text` with its baseline at y, and the run's width."""
        infos, positions = self._shape(text)
        scale = size / self.upem
        width = sum(p.x_advance for p in positions) * scale + tracking * max(len(infos) - 1, 0)
        if anchor == "end":
            x -= width
        elif anchor == "middle":
            x -= width / 2
        placed = []
        for info, pos in zip(infos, positions):
            placed.append((info.codepoint, x + pos.x_offset * scale, y - pos.y_offset * scale))
            x += pos.x_advance * scale + tracking
        return placed, width

    def wrap(self, text: str, size: float, max_width: float) -> list[str]:
        """Break into as few lines as fit, then even them out so no line is left with one word."""
        words = text.split()
        lines, line = [], ""
        for word in words:
            trial = f"{line} {word}".strip()
            if line and self.width(trial, size) > max_width:
                lines.append(line)
                line = word
            else:
                line = trial
        if line:
            lines.append(line)
        if len(lines) != 2:
            return lines
        # Two lines: take the break that keeps both inside max_width with the most even widths.
        best = lines
        best_gap = abs(self.width(lines[0], size) - self.width(lines[1], size))
        for cut in range(1, len(words)):
            first, second = " ".join(words[:cut]), " ".join(words[cut:])
            w1, w2 = self.width(first, size), self.width(second, size)
            if max(w1, w2) <= max_width and abs(w1 - w2) < best_gap:
                best, best_gap = [first, second], abs(w1 - w2)
        return best
