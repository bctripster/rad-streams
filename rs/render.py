"""Frame composition with Pillow: photo crossfades, the text panel, the credit."""
from __future__ import annotations

import colorsys
import hashlib
import os
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .motion import PRESETS, PanelMotion
from .splash import splash_background

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
FONT_DIR = os.path.join(ASSETS, "fonts")

TEXT = (236, 236, 232, 255)        # off-white: easier on panels than pure white
TEXT_DIM = (196, 198, 200, 255)
PANEL_FILL = (8, 10, 14, 150)      # dim, semi-transparent
CREDIT = (228, 228, 228, 220)
CREDIT_FILL = (8, 10, 14, 165)

CREDIT_FADE_SECONDS = 1.5
CREDIT_SEP = "  ·  "      # between the photo credit and the weather data credit

SPLASH_SHIFT = 0.015      # how far the splash shifts between changes, fraction of each side
SPLASH_OFFSETS = ((0, 0), (-1, -1), (1, 1), (1, -1), (-1, 1))
SPLASH_PRESETS = ("top-right", "bottom-right", "middle-right")   # the logo is on the left


@dataclass
class PanelInfo:
    station: str
    city: str
    title: Optional[str] = None      # cleaned now-playing, None -> station fallback
    weather: Optional[str] = None    # one-line summary
    status: Optional[str] = None     # e.g. "Reconnecting to stream…"
    weather_place: Optional[str] = None  # where the weather is, e.g. "Vancouver"
    themed: bool = False             # a theme channel: the panel shows no city/theme or "Live from"

    def key(self) -> tuple:
        return (self.station, self.city, self.title, self.weather, self.status, self.weather_place,
                self.themed)


class Fonts:
    def __init__(self, height: int):
        s = height / 1080.0
        self.scale = s

        def f(name: str, size: float, minimum: int = 8):
            path = os.path.join(FONT_DIR, name)
            try:
                return ImageFont.truetype(path, max(minimum, int(round(size * s))))
            except OSError:
                return ImageFont.load_default()

        self.label = f("Inter-SemiBold.ttf", 26)
        self.title = f("Inter-Bold.ttf", 54)
        self.artist = f("Inter-Regular.ttf", 36)
        self.info = f("Inter-Regular.ttf", 30)
        self.credit = f("Inter-Regular.ttf", 17, minimum=11)   # stays legible at 480p
        self.status = f("Inter-SemiBold.ttf", 24)
        self.caption = f("Inter-SemiBold.ttf", 19)


def accent_for(name: str) -> tuple[int, int, int, int]:
    """A stable, fairly bright accent colour per station."""
    hue = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:4], 16) / 65535.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.68, 0.65)
    return int(r * 255), int(g * 255), int(b * 255), 255


def fit_photo(path: str, size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("I;16", "I;16B", "I;16L", "I"):
            # 16-bit greyscale: scale to 8 bits first, or it converts to solid white.
            im = im.point(lambda v: v / 256).convert("L")
        im = im.convert("RGB")
        return ImageOps.fit(im, size, Image.LANCZOS, centering=(0.5, 0.45))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        cur = word
        while draw.textlength(cur, font=font) > width and len(cur) > 1:  # very long word
            cut = len(cur)
            while cut > 1 and draw.textlength(cur[:cut], font=font) > width:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _ellipsize(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def build_panel(info: PanelInfo, fonts: Fonts, frame_w: int, accent) -> Image.Image:
    from .audio import split_artist_title

    s = fonts.scale
    pad = int(30 * s)
    inner = int(frame_w * 0.36)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    rows: list[tuple] = []   # (kind, text, font, colour, gap_after)
    # Without a track title the station name is the headline, so the label says "On air".
    label = info.station.upper() if info.title else "ON AIR"
    # A theme channel's panel names no place: just the station and what's playing.
    rows.append(("label", (label, None if info.themed else info.city), fonts.label, accent, int(14 * s)))
    if info.title:
        artist, title = split_artist_title(info.title)
        for line in _wrap(probe, title, fonts.title, inner, 2):
            rows.append(("text", line, fonts.title, TEXT, int(4 * s)))
        if artist:
            rows.append(("text", _ellipsize(probe, artist, fonts.artist, inner),
                         fonts.artist, TEXT_DIM, int(4 * s)))
    else:
        for line in _wrap(probe, info.station, fonts.title, inner, 2):
            rows.append(("text", line, fonts.title, TEXT, int(4 * s)))
        if not info.themed:
            rows.append(("text", _ellipsize(probe, f"Live from {info.city.split(',')[0]}", fonts.artist,
                                            inner), fonts.artist, TEXT_DIM, int(4 * s)))
    if info.status:
        rows.append(("text", _ellipsize(probe, info.status, fonts.status, inner), fonts.status, accent,
                     int(4 * s)))
    if info.weather:
        rows.append(("rule", None, None, None, int(14 * s)))
        # Say whose weather it is: the station's city.
        if info.weather_place:
            rows.append(("caption", f"WEATHER IN {info.weather_place.upper()}", fonts.caption, TEXT_DIM,
                         int(2 * s)))
        rows.append(("info", info.weather, fonts.info, TEXT_DIM, 0))

    def row_h(font) -> int:
        a, d = font.getmetrics()
        return a + d

    height = pad * 2
    for kind, _, font, _, gap in rows:
        height += (int(18 * s) if kind == "rule" else row_h(font)) + gap
    width = inner + pad * 2

    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    d.rounded_rectangle((0, 0, width - 1, height - 1), radius=int(18 * s), fill=PANEL_FILL)
    y = pad
    for kind, text, font, colour, gap in rows:
        if kind == "label":
            station, city = text
            d.text((pad, y), _ellipsize(d, station, font, inner // 2), font=font, fill=colour)
            used = d.textlength(_ellipsize(d, station, font, inner // 2), font=font)
            if city:
                rest = _ellipsize(d, f"  ·  {city}", font, int(inner - used))
                d.text((pad + used, y), rest, font=font, fill=TEXT_DIM)
            y += row_h(font) + gap
        elif kind == "rule":
            mid = y + int(9 * s)
            d.line((pad, mid, pad + inner, mid), fill=(255, 255, 255, 60), width=max(1, int(2 * s)))
            y += int(18 * s) + gap
        elif kind == "caption":
            d.text((pad, y), _ellipsize(d, text, font, inner), font=font, fill=(*colour[:3], 170))
            y += row_h(font) + gap
        elif kind == "info":
            d.text((pad, y), _ellipsize(d, text, font, inner), font=font, fill=colour)
            y += row_h(font) + gap
        else:
            d.text((pad, y), text, font=font, fill=colour)
            y += row_h(font) + gap
    return panel


def build_credit(text: str, fonts: Fonts, max_w: int) -> Image.Image:
    """Small credit on a dim pill, so it stays readable on bright photos."""
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    head, sep, tail = text.rpartition(CREDIT_SEP)
    if sep and probe.textlength(text, font=fonts.credit) > max_w:
        # Shorten the photo credit, never the required data credit after it.
        tail = sep + tail
        head = _ellipsize(probe, head, fonts.credit, max(0, int(max_w - probe.textlength(tail, font=fonts.credit))))
        text = head + tail
    text = _ellipsize(probe, text, fonts.credit, max_w)
    a, d_ = fonts.credit.getmetrics()
    padx, pady = max(4, int(10 * fonts.scale)), max(2, int(4 * fonts.scale))
    w = int(probe.textlength(text, font=fonts.credit)) + padx * 2
    h = a + d_ + pady * 2
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=CREDIT_FILL)
    d.text((padx, pady), text, font=fonts.credit, fill=CREDIT)
    return img


def splash_size(size: tuple[int, int]) -> tuple[int, int]:
    """The splash is drawn this much larger than the frame, to leave room to shift it."""
    w, h = size
    return w + 2 * int(w * SPLASH_SHIFT), h + 2 * int(h * SPLASH_SHIFT)


class Compositor:
    """Holds the current/next photo and draws frames on demand."""

    def __init__(self, size: tuple[int, int], station_name: str, *, pattern: str = "cycle",
                 crossfade: float = 2.0, seed: Optional[int] = None,
                 splash_dir: Optional[str] = None, credit_seconds: float = 0.0):
        self.size = size
        self.credit_seconds = credit_seconds
        self.fonts = Fonts(size[1])
        self.accent = accent_for(station_name)
        self.motion = PanelMotion(pattern, seed=seed)
        self.crossfade = crossfade
        # The Rad Streams splash shows until the city's photos arrive (or if there are none).
        # It is drawn a little oversize and shifted on each change, against burn-in.
        self._splash = splash_background(splash_size(size), splash_dir)
        self._splash_i = 0
        self.fallback = self._splash_view()
        self.on_splash = True
        self.current = self.fallback
        self.previous: Optional[Image.Image] = None
        self.fade_start = 0.0
        self.credit_text: Optional[str] = None
        self._credit_img: Optional[Image.Image] = None
        self._credit_prev: Optional[Image.Image] = None
        self._panel_key = None
        self._panel: Optional[Image.Image] = None
        self.motion.jump(0.0, 60.0, only=SPLASH_PRESETS)

    def _splash_view(self) -> Image.Image:
        w, h = self.size
        dx, dy = int(w * SPLASH_SHIFT), int(h * SPLASH_SHIFT)
        fx, fy = SPLASH_OFFSETS[self._splash_i % len(SPLASH_OFFSETS)]
        x, y = int(dx * (1 + fx)), int(dy * (1 + fy))
        return self._splash.crop((x, y, x + w, y + h))

    def show(self, photo: Optional[Image.Image], credit: Optional[str], now: float, span: float) -> None:
        """Crossfade to ``photo`` (or the fallback) and move the text."""
        self.previous = self.current
        self.on_splash = photo is None
        if photo is None:
            self._splash_i += 1
            self.fallback = self._splash_view()
        self.current = photo if photo is not None else self.fallback
        self.fade_start = now
        self._credit_prev = self._credit_img
        self.credit_text = credit
        self._credit_img = build_credit(credit, self.fonts, int(self.size[0] * 0.6)) if credit else None
        # On the splash the panel stays on the radio's side, clear of the logo.
        self.motion.jump(now, span, only=SPLASH_PRESETS if self.on_splash else None)

    def _panel_for(self, info: PanelInfo) -> Image.Image:
        key = info.key()
        if key != self._panel_key:
            self._panel = build_panel(info, self.fonts, self.size[0], self.accent)
            self._panel_key = key
        return self._panel

    def frame(self, info: PanelInfo, now: float) -> Image.Image:
        t = (now - self.fade_start) / self.crossfade if self.crossfade > 0 else 1.0
        if self.previous is not None and t < 1.0:
            alpha = max(0.0, t)
            alpha = alpha * alpha * (3 - 2 * alpha)  # smoothstep
            base = Image.blend(self.previous, self.current, alpha)
        else:
            self.previous = None
            base = self.current.copy()

        panel = self._panel_for(info)
        x, y = self.motion.position(now, self.size, panel.size)
        base.paste(panel, (x, y), panel)

        credit = self._credit_img
        if credit is not None:
            # The credit sits on the edge opposite the panel, small and dim.
            ax, ay = PRESETS[self.motion.preset]
            w, h = self.size
            m = int(h * 0.025)
            cx = m if ax >= 0.5 else w - credit.width - m
            cy = m if ay >= 0.5 else h - credit.height - m
            alpha = self._credit_alpha(now, t)
            if alpha >= 1.0:
                base.paste(credit, (cx, cy), credit)
            elif alpha > 0.0:
                fade = credit.copy()
                fade.putalpha(fade.getchannel("A").point(lambda v: int(v * alpha)))
                base.paste(fade, (cx, cy), fade)
        return base

    def _credit_alpha(self, now: float, t: float) -> float:
        """Fade in with the photo; with credit_seconds set, fade out after that long."""
        alpha = max(t, 0.0) if self.previous is not None and t < 1.0 else 1.0
        if self.credit_seconds > 0:
            shown = now - self.fade_start
            alpha = min(alpha, max(0.0, 1.0 - (shown - self.credit_seconds) / CREDIT_FADE_SECONDS))
        return alpha
