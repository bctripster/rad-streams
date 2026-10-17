"""Burn-in prevention: where the text panel sits.

On every photo change (once a minute by default) the panel jumps to a new
preset spot, so no pixel shows the same text for long. It holds still in
between: a slow drift made photos look glitchy once ffmpeg blended the 4 fps
frames up to 25 fps, and it cost bitrate for no real burn-in benefit.
"""
from __future__ import annotations

import random
from typing import Optional

# (x, y) of the panel's free space, 0 = left/top edge, 1 = right/bottom edge.
PRESETS = {
    "bottom-left": (0.0, 1.0),
    "top-right": (1.0, 0.0),
    "bottom-right": (1.0, 1.0),
    "top-left": (0.0, 0.0),
    "bottom-center": (0.5, 1.0),
    "middle-left": (0.0, 0.5),
    "top-center": (0.5, 0.0),
    "middle-right": (1.0, 0.5),
}
CORNERS = ("bottom-left", "top-right", "bottom-right", "top-left")

MARGIN = 0.05        # title-safe margin, fraction of each dimension


class PanelMotion:
    def __init__(self, pattern: str = "cycle", seed: Optional[int] = None):
        self.pattern = pattern if pattern in ("cycle", "random", "corners") else "cycle"
        self.rng = random.Random(seed)
        self._order = list(CORNERS if self.pattern == "corners" else PRESETS)
        self._i = -1
        self.preset = self._order[0]
        self.jump(0.0, 60.0)

    def jump(self, now: float, span: float, only: Optional[tuple] = None) -> str:
        """Move to the next preset. (``now`` and ``span`` are kept for callers; the panel holds still.)

        ``only`` limits the choice to some presets (e.g. to keep clear of the
        splash logo); the normal order resumes afterwards.
        """
        allowed = [p for p in self._order if not only or p in only] or [p for p in PRESETS if p in only]
        if self.pattern == "random":
            choices = [p for p in allowed if p != self.preset] or allowed
            self.preset = self.rng.choice(choices)
        else:
            for _ in range(len(self._order)):
                self._i = (self._i + 1) % len(self._order)
                if self._order[self._i] in allowed:
                    break
            self.preset = self._order[self._i] if self._order[self._i] in allowed else allowed[0]
        return self.preset

    def position(self, now: float, frame: tuple[int, int], panel: tuple[int, int]) -> tuple[int, int]:
        """Top-left pixel of the panel (the same for every frame until the next jump)."""
        fw, fh = frame
        pw, ph = panel
        mx, my = int(fw * MARGIN), int(fh * MARGIN)
        free_w, free_h = max(fw - 2 * mx - pw, 0), max(fh - 2 * my - ph, 0)
        ax, ay = PRESETS[self.preset]
        return int(round(mx + ax * free_w)), int(round(my + ay * free_h))
