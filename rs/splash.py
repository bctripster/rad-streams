"""The Rad Streams splash: a vintage cathedral radio glowing in a dark room,
with the Rad Streams logo beside it. Shown before a city's photos arrive, or
when a city has none. Drawn with Pillow and numpy, no image files needed."""
from __future__ import annotations

import functools
import os
import tempfile
import threading
from typing import Optional

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")

# The plugin icon's gradient, purple to coral.
LOGO_FROM = (150, 96, 255)
LOGO_TO = (255, 112, 112)


def _font(name: str, px: int):
    try:
        return ImageFont.truetype(os.path.join(FONT_DIR, name), max(8, px))
    except OSError:
        return ImageFont.load_default()


def _blur(img: Image.Image, r: float) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(max(0.5, r)))


def _arch_mask(size, box, arch_h: int) -> Image.Image:
    """A cathedral outline: a rectangle topped by a half-ellipse."""
    x0, y0, x1, y1 = box
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    d.ellipse((x0, y0, x1, y0 + 2 * arch_h), fill=255)
    d.rectangle((x0, y0 + arch_h, x1, y1), fill=255)
    d.rectangle((0, y1 + 1, size[0], size[1]), fill=0)    # the ellipse's lower half can overshoot
    return m


def _room(w: int, h: int, cx: float, cy: float, rng) -> np.ndarray:
    """Dark backdrop lit warmly from behind the radio, plus a tabletop."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.hypot((xx - cx) / w, (yy - cy) / h * 0.8)
    glow = np.clip(1.0 - r / 0.62, 0, 1) ** 2.2
    dark = np.array([12, 9, 12], np.float32)
    warm = np.array([92, 48, 26], np.float32)
    img = dark + (warm - dark) * glow[..., None]
    # cool tint far from the light, so the warm area reads as lamplight
    img += np.array([0, 2, 8], np.float32) * (1 - glow[..., None])
    # tabletop
    top = int(h * 0.835)
    t = np.clip((yy - top) / (h - top), 0, 1)
    table = (yy >= top)[..., None]
    wood = np.array([46, 26, 16], np.float32) * (1 - 0.55 * t[..., None])
    wood = wood * (0.7 + 0.6 * glow[..., None])
    img = np.where(table, wood, img)
    edge = (np.abs(yy - top) < max(1.0, h / 540))[..., None]
    img = np.where(edge, img + 26, img)
    img += rng.normal(0, 2.2, (h, w, 1)).astype(np.float32)   # fine grain against banding
    return img


def _wood(w: int, h: int, rng, base=(118, 60, 28)) -> np.ndarray:
    """Walnut-ish vertical grain."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    warp = sum(rng.uniform(4, 14) * np.sin(yy / rng.uniform(60, 220) * (w / 600) + rng.uniform(0, 6))
               for _ in range(3))
    g = xx + warp
    grain = (0.55 * np.sin(g / (w / 140)) + 0.3 * np.sin(g / (w / 37) + 1.3)
             + 0.15 * np.sin(g / (w / 11)))
    shade = 1.0 + 0.11 * grain
    return np.array(base, np.float32) * shade[..., None]


def _paste_np(dst: np.ndarray, src: np.ndarray, mask: Image.Image, xy=(0, 0)) -> None:
    """Alpha-blend an RGB float array into ``dst`` through an L mask."""
    x, y = xy
    h, w = src.shape[:2]
    a = np.asarray(mask, np.float32)[..., None] / 255.0
    region = dst[y:y + h, x:x + w]
    region[:] = region * (1 - a) + src * a


def _radio(canvas: np.ndarray, cx: int, base_y: int, height: int, rng) -> tuple[int, int, int, int]:
    """Draw a cathedral radio standing on the table; returns its bounding box."""
    H, W = canvas.shape[:2]
    rw = int(height * 0.78)
    x0, x1 = cx - rw // 2, cx + rw // 2
    y1 = base_y
    y0 = y1 - height
    arch = int(rw * 0.5)
    s = height / 760.0

    # contact shadow on the table
    sh = Image.new("L", (W, H), 0)
    ImageDraw.Draw(sh).ellipse((x0 - 40 * s, y1 - 18 * s, x1 + 40 * s, y1 + 34 * s), fill=200)
    sh = np.asarray(_blur(sh, 22 * s), np.float32)[..., None] / 255.0
    canvas[:] = canvas * (1 - 0.75 * sh)

    # cabinet with a bevelled edge: lighter rim, darker towards the middle edges
    body = _arch_mask((W, H), (x0, y0, x1, y1), arch)
    wood = _wood(W, H, rng)
    inner = _blur(body.filter(ImageFilter.MinFilter(3)), 26 * s)
    bevel = np.asarray(inner, np.float32)[..., None] / 255.0
    # light comes from the upper left
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    light = np.clip(1.05 - 0.45 * ((xx - x0) / rw) - 0.25 * ((yy - y0) / height), 0.55, 1.1)[..., None]
    cab = wood * (0.55 + 0.45 * bevel) * light
    rim = np.asarray(_blur(ImageChops.subtract(body, body.filter(ImageFilter.MinFilter(5))), 2 * s),
                     np.float32)[..., None] / 255.0
    cab = cab + rim * 70 * light
    _paste_np(canvas, cab, body)

    # raised inner frame (a lighter wood band) around the grille
    m = int(46 * s)
    fx0, fx1, fy0, fy1 = x0 + m, x1 - m, y0 + m, int(y0 + height * 0.58)
    frame = _arch_mask((W, H), (fx0, fy0, fx1, fy1), (fx1 - fx0) // 2)
    band = _wood(W, H, rng, base=(150, 82, 40)) * light
    _paste_np(canvas, band, frame)

    # speaker grille: dark cloth lit from inside by the tubes
    g = int(22 * s)
    gx0, gx1, gy0, gy1 = fx0 + g, fx1 - g, fy0 + g, fy1 - g
    grille = _arch_mask((W, H), (gx0, gy0, gx1, gy1), (gx1 - gx0) // 2)
    gcx, gcy = (gx0 + gx1) / 2, gy0 + (gy1 - gy0) * 0.62
    r = np.hypot((xx - gcx) / (gx1 - gx0), (yy - gcy) / (gy1 - gy0))
    tube = np.clip(1 - r / 0.75, 0, 1) ** 1.6
    cloth = np.array([40, 24, 16], np.float32) + np.array([150, 80, 30], np.float32) * tube[..., None]
    weave = (np.sin(xx * 1.9 / max(s, 0.3)) * np.sin(yy * 1.9 / max(s, 0.3)))[..., None]
    cloth = cloth * (0.93 + 0.07 * weave)
    _paste_np(canvas, cloth, grille)

    # gothic fretwork over the cloth: vertical bars and nested arches
    fret = Image.new("L", (W, H), 0)
    fd = ImageDraw.Draw(fret)
    bw = max(2, int(13 * s))
    gw = gx1 - gx0
    for k in (1, 2, 3, 4, 5):
        bx = gx0 + gw * k / 6
        fd.line((bx, gy0 + (gy1 - gy0) * 0.30, bx, gy1), fill=255, width=bw)
    for frac in (0.52, 0.80):
        aw = gw * frac
        ax0, ax1 = gcx - aw / 2, gcx + aw / 2
        top = gy0 + (gy1 - gy0) * (0.30 - frac * 0.32) + gw * 0.12
        fd.arc((ax0, top, ax1, top + aw), 180, 360, fill=255, width=bw)
        fd.line((ax0, top + aw / 2, ax0, gy1), fill=255, width=bw)
        fd.line((ax1, top + aw / 2, ax1, gy1), fill=255, width=bw)
    fret = ImageChops.multiply(fret, grille)
    fshadow = np.asarray(_blur(fret, 5 * s), np.float32)[..., None] / 255.0
    canvas[:] = canvas * (1 - 0.55 * fshadow)
    _paste_np(canvas, _wood(W, H, rng, base=(140, 76, 36)) * light, fret)

    # dial window with a glowing amber face, ticks and a needle
    dcx, dcy = cx, int(y0 + height * 0.705)
    dr = int(rw * 0.12)
    face = Image.new("L", (W, H), 0)
    ImageDraw.Draw(face).ellipse((dcx - dr, dcy - dr, dcx + dr, dcy + dr), fill=255)
    halo = np.asarray(_blur(face, 30 * s), np.float32)[..., None] / 255.0
    canvas[:] = canvas + halo * np.array([120, 62, 18], np.float32)
    bezel = Image.new("L", (W, H), 0)
    ImageDraw.Draw(bezel).ellipse((dcx - dr - 9 * s, dcy - dr - 9 * s, dcx + dr + 9 * s, dcy + dr + 9 * s),
                                  fill=255)
    _paste_np(canvas, np.full((H, W, 3), (196, 160, 92), np.float32) * light, bezel)
    fr = np.hypot(xx - dcx, yy - dcy) / dr
    amber = (np.array([255, 196, 104], np.float32) * (1 - 0.35 * fr[..., None] ** 2))
    _paste_np(canvas, amber, face)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for i in range(23):
        ang = np.radians(200 + i * (140 / 22))
        r0 = dr * (0.70 if i % 2 else 0.60)
        od.line((dcx + r0 * np.cos(ang), dcy + r0 * np.sin(ang),
                 dcx + dr * 0.86 * np.cos(ang), dcy + dr * 0.86 * np.sin(ang)),
                fill=(70, 36, 14, 230), width=max(1, int(3 * s)))
    ang = np.radians(292)
    od.line((dcx, dcy + dr * 0.2, dcx + dr * 0.92 * np.cos(ang), dcy + dr * 0.92 * np.sin(ang)),
            fill=(150, 28, 18, 255), width=max(2, int(5 * s)))
    od.ellipse((dcx - 7 * s, dcy + dr * 0.2 - 7 * s, dcx + 7 * s, dcy + dr * 0.2 + 7 * s),
               fill=(60, 30, 14, 255))
    # a soft highlight across the glass
    od.chord((dcx - dr * 0.8, dcy - dr * 0.85, dcx + dr * 0.8, dcy + dr * 0.1), 200, 340,
             fill=(255, 255, 255, 34))
    rgb = np.asarray(ov.convert("RGB"), np.float32)
    _paste_np(canvas, rgb, ov.getchannel("A"))

    # three knobs
    ky = int(y0 + height * 0.875)
    kr = int(rw * 0.062)
    for kx in (cx - rw * 0.28, cx, cx + rw * 0.28):
        kx = int(kx)
        k = Image.new("L", (W, H), 0)
        ImageDraw.Draw(k).ellipse((kx - kr, ky - kr, kx + kr, ky + kr), fill=255)
        ks = np.asarray(_blur(k, 6 * s), np.float32)[..., None] / 255.0
        canvas[:] = canvas * (1 - 0.5 * np.roll(ks, (int(6 * s), int(4 * s)), axis=(0, 1)))
        kd = np.hypot(xx - (kx - kr * 0.35), yy - (ky - kr * 0.4)) / (kr * 1.6)
        knob = np.array([96, 48, 22], np.float32) * (1.35 - kd[..., None]).clip(0.35, 1.4)
        _paste_np(canvas, knob, k)

    # plinth
    p = Image.new("L", (W, H), 0)
    ImageDraw.Draw(p).rounded_rectangle((x0 - 14 * s, y1 - 30 * s, x1 + 14 * s, y1), radius=int(8 * s),
                                        fill=255)
    _paste_np(canvas, _wood(W, H, rng, base=(84, 42, 20)) * light, p)
    return x0, y0, x1, y1


def _gradient_fill(mask: Image.Image, c0, c1, bbox=None) -> Image.Image:
    """Diagonal purple-to-coral fill through ``mask`` (RGBA, mask as alpha)."""
    w, h = mask.size
    x0, y0, x1, y1 = bbox or (0, 0, w, h)
    ty = np.clip((np.arange(h) - y0) / max(1, y1 - y0), 0, 1)
    tx = np.clip((np.arange(w) - x0) / max(1, x1 - x0), 0, 1)
    t = (np.add.outer(0.6 * ty, 0.4 * tx))[..., None]
    rgb = np.array(c0, np.float32) * (1 - t) + np.array(c1, np.float32) * t
    img = Image.fromarray(rgb.clip(0, 255).astype("uint8")).convert("RGBA")
    img.putalpha(mask)
    return img


def _logo(w: int, h: int, s: float) -> Image.Image:
    """The wave mark and the RAD STREAMS wordmark, centred in a w x h RGBA tile."""
    big = _font("Inter-Bold.ttf", int(170 * s))
    small = _font("Inter-SemiBold.ttf", int(58 * s))
    tag = _font("Inter-Regular.ttf", int(30 * s))
    track = int(20 * s)

    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    # wave mark, as on the plugin icon
    mx, my = w // 2, int(h * 0.30)
    dot = int(26 * s)
    d.ellipse((mx - dot, my - dot, mx + dot, my + dot), fill=255)
    lw = max(3, int(20 * s))
    for i, r in enumerate((70, 122, 174)):
        rr = int(r * s)
        d.arc((mx - rr, my - rr, mx + rr, my + rr), 222, 318, fill=255, width=lw)
    # wordmark
    word = "RAD"
    tw = d.textlength(word, font=big)
    asc, _ = big.getmetrics()
    ty = my + int(44 * s)
    d.text((mx - tw / 2, ty), word, font=big, fill=255)
    letters = "STREAMS"
    widths = [d.textlength(c, font=small) for c in letters]
    total = sum(widths) + track * (len(letters) - 1)
    x = mx - total / 2
    sy = ty + int(asc * 1.08)
    for c, cw in zip(letters, widths):
        d.text((x, sy), c, font=small, fill=255)
        x += cw + track
    # thin rules either side of STREAMS
    ry = sy + int(small.getmetrics()[0] * 0.58)
    gap = int(26 * s)
    d.line((mx - total / 2 - gap - 90 * s, ry, mx - total / 2 - gap, ry), fill=255, width=max(2, int(4 * s)))
    d.line((mx + total / 2 + gap, ry, mx + total / 2 + gap + 90 * s, ry), fill=255, width=max(2, int(4 * s)))

    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = _blur(mask, 28 * s).point(lambda v: int(v * 0.55))
    box = mask.getbbox()
    out.alpha_composite(_gradient_fill(glow, LOGO_FROM, LOGO_TO, box))
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(_blur(mask, 6 * s).point(lambda v: int(v * 0.6)))
    out.alpha_composite(shadow, (int(4 * s), int(6 * s)))
    out.alpha_composite(_gradient_fill(mask, LOGO_FROM, LOGO_TO, box))

    td = ImageDraw.Draw(out)
    line = "internet radio, live on your TV"
    lw_ = td.textlength(line, font=tag)
    td.text((mx - lw_ / 2, sy + int(small.getmetrics()[0] * 1.9)), line, font=tag, fill=(222, 208, 196, 200))
    return out


SPLASH_VERSION = 1      # bump when the drawing changes, so cached copies are redrawn


_LOCK = threading.Lock()     # one drawing at a time: at 4K it briefly needs ~850 MB


def splash_background(size: tuple[int, int], cache_dir: Optional[str] = None) -> Image.Image:
    """The splash at ``size``, shared by every station (see _splash_background)."""
    with _LOCK:
        return _splash_background(tuple(size), cache_dir)


@functools.lru_cache(maxsize=2)
def _splash_background(size: tuple[int, int], cache_dir: Optional[str] = None) -> Image.Image:
    """The splash frame at ``size``, shared by every station.

    Drawing takes a few seconds, so with ``cache_dir`` the result is kept as a
    PNG and later processes just load it.
    """
    path = None
    if cache_dir:
        path = os.path.join(cache_dir, f"splash-v{SPLASH_VERSION}-{size[0]}x{size[1]}.png")
        try:
            with Image.open(path) as im:
                if im.size == tuple(size):
                    return im.convert("RGB")
        except (OSError, ValueError):
            pass
    img = draw_splash(size)
    if path:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                img.save(f, format="PNG")
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except (OSError, UnboundLocalError):
                pass
    return img


def draw_splash(size: tuple[int, int]) -> Image.Image:
    w, h = size
    s = h / 1080.0
    rng = np.random.default_rng(1928)        # fixed seed: the same splash every time
    radio_cx = int(w * 0.70)
    canvas = _room(w, h, radio_cx, h * 0.48, rng)
    rh = int(h * 0.74)
    rx0 = max(0, radio_cx - int(rh * 0.39) - int(110 * s))
    rx1 = min(w, radio_cx + int(rh * 0.39) + int(110 * s))
    ry0 = max(0, int(h * 0.855) - rh - int(30 * s))
    # draw in a crop around the radio: much faster than full-frame arrays
    _radio(canvas[ry0:, rx0:rx1], radio_cx - rx0, int(h * 0.855) - ry0, rh, rng)
    img = Image.fromarray(canvas.clip(0, 255).astype("uint8"))
    logo = _logo(int(w * 0.44), int(h * 0.80), s)
    img.paste(logo, (int(w * 0.25 - logo.width / 2), int(h * 0.12)), logo)
    return img
