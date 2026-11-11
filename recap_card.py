"""Draws the drop recap's share card: the Beta Stats card from the proxy checkout
monitor, fed the ACO's checkouts instead of that tool's database.

Product photos beside the Top Hit and each leaderboard row come from the image
the checkout bot put on its webhook (checkouts.image_url); a product without one
gets a tile with its first letter. Fonts are Segoe UI and Bahnschrift from the
Windows fonts folder, which the VPS has.

render_png() and render_gif() take the dict db.recap_card_stats() builds.
"""
import hashlib
import math
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent

def load_share_font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()

SHARE_SCALE = 3
SHARE_W, SHARE_H = 1080, 1350
SHARE_MINT = (74, 222, 160)
SHARE_CORAL = (255, 94, 124)
SHARE_GOLD = (255, 204, 92)
SHARE_GOLD_DEEP = (240, 150, 52)
SHARE_BLUE = (112, 168, 255)
SHARE_TEXT = (244, 247, 252)
SHARE_MUTED = (142, 158, 180)
SHARE_DIM = (100, 114, 136)
SHARE_MOTIONS = ("intro", "once", "ambient")

SHARE_STRIKE_PERIOD = 26
SHARE_STRIKE_FLICKER = (1.0, 0.25, 0.85, 0.35)

SHARE_THEMES_V2: dict[str, dict[str, Any]] = {
    "titanium": {"name": "Titanium Core", "bg": ((7, 9, 14), (14, 16, 25)), "accent": (86, 209, 255), "accent2": (129, 140, 255)},
    "chrome": {"name": "Chrome Alloy", "bg": ((8, 9, 12), (19, 21, 28)), "accent": (196, 218, 240), "accent2": (124, 200, 255)},
    "cyber": {"name": "Cyber Grid", "bg": ((5, 6, 20), (18, 6, 38)), "accent": (0, 232, 255), "accent2": (255, 64, 196)},
    "neon": {"name": "Neon Matrix", "bg": ((3, 10, 11), (5, 24, 21)), "accent": (71, 255, 180), "accent2": (255, 214, 92)},
    "void": {"name": "Void Pulse", "bg": ((8, 5, 20), (22, 9, 46)), "accent": (176, 120, 255), "accent2": (91, 220, 255)},
    "holo": {"name": "Hologram", "bg": ((3, 8, 19), (9, 26, 46)), "accent": (113, 245, 255), "accent2": (255, 125, 216)},
    # Anime fighting themes
    "saiyan": {"name": "Super Saiyan", "bg": ((14, 9, 4), (36, 20, 6)), "accent": (255, 214, 64), "accent2": (92, 196, 255)},
    "ultra": {"name": "Ultra Instinct", "bg": ((6, 8, 16), (16, 20, 38)), "accent": (214, 226, 255), "accent2": (138, 120, 255)},
    "sharingan": {"name": "Sharingan", "bg": ((10, 3, 5), (30, 6, 10)), "accent": (255, 46, 64), "accent2": (255, 140, 120)},
    "kyuubi": {"name": "Nine-Tails Chakra", "bg": ((16, 7, 3), (40, 13, 6)), "accent": (255, 140, 36), "accent2": (255, 64, 48)},
    "bankai": {"name": "Bankai", "bg": ((5, 5, 7), (16, 14, 18)), "accent": (236, 238, 245), "accent2": (220, 38, 60)},
    "cursed": {"name": "Hollow Purple", "bg": ((8, 5, 18), (20, 8, 40)), "accent": (186, 104, 255), "accent2": (72, 132, 255)},
    "hinokami": {"name": "Sun Breathing", "bg": ((14, 6, 4), (36, 12, 6)), "accent": (255, 120, 40), "accent2": (255, 208, 96)},
    "haki": {"name": "Conqueror's Haki", "bg": ((6, 4, 10), (22, 6, 20)), "accent": (232, 40, 92), "accent2": (164, 72, 255)},
}

# Per-theme aura + lightning. aura = (core, edge) flame colors, strength 0-1; bolt = glow color, core = bolt line color;
# bolts = static bolts in the background; strikes = animated lightning flashes in the GIF margins.
SHARE_THEME_FX: dict[str, dict[str, Any]] = {
    "titanium": {"aura": ((170, 230, 255), (86, 209, 255)), "strength": 0.3, "bolt": (96, 196, 255), "core": (236, 248, 255), "bolts": 2, "strikes": True},
    "chrome": {"aura": ((236, 242, 250), (150, 190, 230)), "strength": 0.3, "bolt": (170, 210, 255), "core": (250, 252, 255), "bolts": 2, "strikes": True},
    "cyber": {"aura": ((120, 245, 255), (255, 64, 196)), "strength": 0.45, "bolt": (0, 232, 255), "core": (230, 255, 255), "bolts": 3, "strikes": True},
    "neon": {"aura": ((170, 255, 215), (71, 255, 180)), "strength": 0.4, "bolt": (71, 255, 180), "core": (235, 255, 245), "bolts": 3, "strikes": True},
    "void": {"aura": ((210, 170, 255), (140, 80, 255)), "strength": 0.45, "bolt": (176, 120, 255), "core": (240, 230, 255), "bolts": 3, "strikes": True},
    "holo": {"aura": ((190, 250, 255), (255, 125, 216)), "strength": 0.4, "bolt": (113, 245, 255), "core": (240, 255, 255), "bolts": 3, "strikes": True},
    "saiyan": {"aura": ((255, 240, 150), (255, 176, 30)), "strength": 1.0, "bolt": (110, 200, 255), "core": (235, 248, 255), "bolts": 4, "strikes": True},
    "ultra": {"aura": ((236, 240, 255), (130, 120, 255)), "strength": 0.85, "bolt": (180, 190, 255), "core": (250, 252, 255), "bolts": 3, "strikes": True},
    "sharingan": {"aura": ((255, 110, 110), (200, 10, 36)), "strength": 0.85, "bolt": (150, 190, 255), "core": (240, 246, 255), "bolts": 4, "strikes": True},
    "kyuubi": {"aura": ((255, 196, 90), (255, 70, 20)), "strength": 1.0, "bolt": (255, 150, 60), "core": (255, 240, 210), "bolts": 3, "strikes": True},
    "bankai": {"aura": ((255, 255, 255), (200, 20, 50)), "strength": 0.8, "bolt": (230, 30, 60), "core": (255, 236, 240), "bolts": 3, "strikes": True},
    "cursed": {"aura": ((215, 150, 255), (70, 110, 255)), "strength": 0.9, "bolt": (150, 90, 255), "core": (236, 224, 255), "bolts": 4, "strikes": True},
    "hinokami": {"aura": ((255, 222, 110), (255, 90, 20)), "strength": 1.0, "bolt": (255, 170, 60), "core": (255, 244, 210), "bolts": 1, "strikes": False},
    "haki": {"aura": ((255, 80, 120), (150, 0, 40)), "strength": 0.85, "bolt": (232, 30, 80), "core": (18, 4, 10), "bolts": 4, "strikes": True},
}


def share_bolt_points(rng: Any, start: tuple[float, float], end: tuple[float, float], spread: float, depth: int = 6) -> list[tuple[float, float]]:
    """Jagged lightning polyline by midpoint displacement (logical coords)."""
    points = [start, end]
    for _ in range(depth):
        nxt = [points[0]]
        for a, b in zip(points, points[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy) or 1.0
            off = rng.uniform(-spread, spread) * length
            nxt += [((a[0] + b[0]) / 2 - dy / length * off, (a[1] + b[1]) / 2 + dx / length * off), b]
        points = nxt
    return points


def share_bolt_paths(rng: Any, start: tuple[float, float], end: tuple[float, float], spread: float = 0.2, branches: int = 3) -> list[tuple[list[tuple[float, float]], float]]:
    """Main bolt plus forked branches, as (points, line weight) pairs."""
    main = share_bolt_points(rng, start, end, spread)
    paths = [(main, 1.0)]
    base_angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    for _ in range(branches):
        origin = main[rng.randrange(len(main) // 5, len(main) * 3 // 4)]
        angle = base_angle + rng.choice((-1, 1)) * rng.uniform(0.35, 0.9)
        reach = length * rng.uniform(0.15, 0.38)
        tip = (origin[0] + math.cos(angle) * reach, origin[1] + math.sin(angle) * reach)
        paths.append((share_bolt_points(rng, origin, tip, spread, depth=4), 0.5))
    return paths


def share_draw_bolts(img: Any, paths: list[tuple[list[tuple[float, float]], float]], glow: tuple[int, int, int], core: tuple[int, int, int], alpha: float = 1.0, origin: tuple[float, float] = (0, 0)) -> None:
    """Composite glowing lightning onto a hi-res image whose top-left sits at logical `origin`."""
    from PIL import Image, ImageDraw, ImageFilter

    s = SHARE_SCALE
    pad = 40
    xs = [x for points, _ in paths for x, _ in points]
    ys = [y for points, _ in paths for _, y in points]
    left = max(0, round((min(xs) - pad - origin[0]) * s))
    top = max(0, round((min(ys) - pad - origin[1]) * s))
    right = min(img.width, round((max(xs) + pad - origin[0]) * s))
    bottom = min(img.height, round((max(ys) + pad - origin[1]) * s))
    if right <= left or bottom <= top:
        return
    size = (right - left, bottom - top)

    def stroke(width: float, blur: float, gain: float) -> Any:
        mask = Image.new("L", size, 0)
        draw = ImageDraw.Draw(mask)
        for points, weight in paths:
            xy = [((x - origin[0]) * s - left, (y - origin[1]) * s - top) for x, y in points]
            draw.line(xy, fill=255, width=max(1, round(width * weight * s)), joint="curve")
        if blur:
            mask = mask.filter(ImageFilter.GaussianBlur(blur * s))
        return mask.point(lambda v: min(255, round(v * gain * alpha)))

    share_paint(img, stroke(16, 14, 0.9), share_rgba(glow), left, top)
    share_paint(img, stroke(5, 3.5, 1.0), share_rgba(glow), left, top)
    share_paint(img, stroke(1.8, 0, 1.0), share_rgba(core), left, top)


SHARE_FONT_FILES = {"regular": "segoeui.ttf", "semibold": "seguisb.ttf", "bold": "segoeuib.ttf", "black": "seguibl.ttf"}
SHARE_NUMBER_VARIATIONS = {"num": "SemiBold", "num-bold": "Bold", "num-regular": "Regular"}


@lru_cache(maxsize=256)
def share_font_v2(size: float, weight: str = "semibold") -> Any:
    """Font sized in logical px; returned font renders at SHARE_SCALE."""
    from PIL import ImageFont

    px = max(1, round(size * SHARE_SCALE))
    fonts_dir = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    if weight in SHARE_NUMBER_VARIATIONS:
        path = fonts_dir / "bahnschrift.ttf"
        if path.exists():
            font = ImageFont.truetype(str(path), px)
            try:
                font.set_variation_by_name(SHARE_NUMBER_VARIATIONS[weight])
            except Exception:
                pass
            return font
        weight = "bold"
    path = fonts_dir / SHARE_FONT_FILES.get(weight, "seguisb.ttf")
    if path.exists():
        return ImageFont.truetype(str(path), px)
    return load_share_font(px, weight != "regular")


def share_fit_font_v2(text_value: str, max_width: float, size: float, weight: str, min_size: float = 12, tracking: float = 0.0) -> Any:
    while size > min_size:
        font = share_font_v2(size, weight)
        if share_text_width(text_value, font, tracking) <= max_width:
            return font
        size -= 1
    return share_font_v2(min_size, weight)


def share_text_width(text_value: Any, font: Any, tracking: float = 0.0) -> float:
    text = str(text_value)
    if not tracking:
        return font.getlength(text) / SHARE_SCALE
    return (sum(font.getlength(char) for char in text) + tracking * SHARE_SCALE * max(len(text) - 1, 0)) / SHARE_SCALE


def share_ellipsize(text_value: Any, font: Any, max_width: float) -> str:
    text = str(text_value)
    if share_text_width(text, font) <= max_width:
        return text
    while len(text) > 4 and share_text_width(f"{text}…", font) > max_width:
        text = text[:-1]
    return f"{text.rstrip(' -—–:|/')}…"


def share_text(draw: Any, x: float, y: float, text_value: Any, font: Any, fill: tuple[int, ...], align: str = "l", tracking: float = 0.0) -> None:
    """Draw text at logical (x, baseline y); align is l/m/r; tracking adds logical px between glyphs."""
    s = SHARE_SCALE
    text = str(text_value)
    if not tracking:
        draw.text((x * s, y * s), text, font=font, fill=fill, anchor=f"{align}s")
        return
    total = share_text_width(text, font, tracking) * s
    left = x * s - (total if align == "r" else total / 2 if align == "m" else 0)
    for char in text:
        draw.text((left, y * s), char, font=font, fill=fill, anchor="ls")
        left += font.getlength(char) + tracking * s


def share_box(box: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    return tuple(round(value * SHARE_SCALE) for value in box)  # type: ignore[return-value]


def share_rgba(color: tuple[int, ...], alpha: int | None = None) -> tuple[int, int, int, int]:
    if alpha is not None:
        return (color[0], color[1], color[2], alpha)
    return (color[0], color[1], color[2], color[3] if len(color) > 3 else 255)


def share_mix(c1: tuple[int, ...], c2: tuple[int, ...], amount: float) -> tuple[int, int, int]:
    return tuple(round(c1[i] + (c2[i] - c1[i]) * amount) for i in range(3))  # type: ignore[return-value]


def share_ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1 - (1 - t) ** 4


def share_gradient(size: tuple[int, int], c1: tuple[int, ...], c2: tuple[int, ...], horizontal: bool = False) -> Any:
    from PIL import Image

    width, height = max(1, size[0]), max(1, size[1])
    ramp = Image.linear_gradient("L")
    if horizontal:
        ramp = ramp.rotate(90, expand=True)
    ramp = ramp.resize((width, height), Image.Resampling.BILINEAR)
    return Image.composite(Image.new("RGBA", (width, height), share_rgba(c2)), Image.new("RGBA", (width, height), share_rgba(c1)), ramp)


def share_composite(img: Any, layer: Any, x: int, y: int) -> None:
    if x < 0 or y < 0:
        layer = layer.crop((max(0, -x), max(0, -y), layer.width, layer.height))
        x, y = max(x, 0), max(y, 0)
    if img.mode == "RGB":
        # The card canvas is opaque RGB so ImageDraw "RGBA" calls blend; layers composite via their alpha.
        img.paste(layer, (x, y), layer)
    else:
        img.alpha_composite(layer, (x, y))


def share_paint(img: Any, mask: Any, fill: Any, x: int, y: int) -> None:
    """Composite `fill` (RGBA image or color) through an L mask at hi-res (x, y)."""
    from PIL import Image, ImageChops

    if isinstance(fill, tuple):
        fill = Image.new("RGBA", mask.size, share_rgba(fill))
    layer = fill.copy()
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), mask))
    share_composite(img, layer, x, y)


def share_round_mask(size: tuple[int, int], radius: float) -> Any:
    from PIL import Image, ImageDraw

    mask = Image.new("L", size, 0)
    radius = min(radius, size[0] / 2, size[1] / 2)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def share_gradient_bar(img: Any, box: tuple[float, float, float, float], c1: tuple[int, ...], c2: tuple[int, ...], glow: bool = False) -> None:
    from PIL import ImageFilter

    x1, y1, x2, y2 = share_box(box)
    if x2 - x1 < 2 or y2 <= y1:
        return
    size = (x2 - x1, y2 - y1)
    if glow:
        pad = round(10 * SHARE_SCALE)
        from PIL import Image

        halo = Image.new("L", (size[0] + pad * 2, size[1] + pad * 2), 0)
        halo.paste(share_round_mask(size, size[1] / 2), (pad, pad))
        halo = halo.filter(ImageFilter.GaussianBlur(5 * SHARE_SCALE)).point(lambda v: v * 95 // 255)
        share_paint(img, halo, share_rgba(c2), x1 - pad, y1 - pad)
    share_paint(img, share_round_mask(size, size[1] / 2), share_gradient(size, c1, c2, horizontal=True), x1, y1)


def share_gradient_text(
    img: Any,
    x: float,
    y: float,
    text_value: Any,
    font: Any,
    c1: tuple[int, ...],
    c2: tuple[int, ...],
    align: str = "l",
    glow: tuple[int, int, int] | None = None,
    glow_alpha: int = 80,
    glow_blur: float = 10,
) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    s = SHARE_SCALE
    text = str(text_value)
    anchor = f"{align}s"
    probe = ImageDraw.Draw(img, "RGBA")
    left, top, right, bottom = (int(v) for v in probe.textbbox((x * s, y * s), text, font=font, anchor=anchor))
    pad = round(glow_blur * s * 2.2) if glow else 2
    width, height = right - left + pad * 2, bottom - top + pad * 2
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text((x * s - left + pad, y * s - top + pad), text, font=font, fill=255, anchor=anchor)
    if glow:
        halo = mask.filter(ImageFilter.GaussianBlur(glow_blur * s)).point(lambda v: v * glow_alpha // 255)
        share_paint(img, halo, share_rgba(glow), left - pad, top - pad)
    fill = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    fill.paste(share_gradient((right - left, bottom - top), c1, c2), (pad, pad))
    fill.paste(share_rgba(c1), (0, 0, width, pad))
    fill.paste(share_rgba(c2), (0, pad + bottom - top, width, height))
    share_paint(img, mask, fill, left - pad, top - pad)


def share_glass_panel(
    img: Any,
    box: tuple[float, float, float, float],
    radius: float = 26,
    tint: tuple[int, int, int] | None = None,
    tint_alpha: int = 34,
    tint_at: str = "tl",
    border: tuple[int, int, int] = (255, 255, 255),
    border_alpha: tuple[int, int] = (58, 12),
    fill_top: tuple[int, int, int, int] = (20, 25, 36, 226),
    fill_bottom: tuple[int, int, int, int] = (11, 14, 22, 236),
) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    s = SHARE_SCALE
    x1, y1, x2, y2 = share_box(box)
    width, height = x2 - x1, y2 - y1
    r = round(radius * s)

    pad = round(44 * s)
    shadow = Image.new("L", (width + pad * 2, height + pad * 2), 0)
    ImageDraw.Draw(shadow).rounded_rectangle((pad, pad + 14 * s, pad + width, pad + height + 8 * s), radius=r, fill=150)
    shadow = shadow.filter(ImageFilter.GaussianBlur(20 * s))
    share_paint(img, shadow, (0, 0, 0, 255), x1 - pad, y1 - pad)

    body = share_gradient((width, height), fill_top, fill_bottom)
    if tint:
        small = Image.new("L", (max(1, width // 8), max(1, height // 8)), 0)
        sw, sh = small.size
        cx = 0 if "l" in tint_at else sw
        cy = 0 if "t" in tint_at else sh
        ImageDraw.Draw(small).ellipse((cx - sw * 0.55, cy - sh * 0.9, cx + sw * 0.55, cy + sh * 0.9), fill=tint_alpha)
        small = small.filter(ImageFilter.GaussianBlur(max(2, min(sw, sh) // 3)))
        share_paint(body, small.resize((width, height), Image.Resampling.BICUBIC), share_rgba(tint), 0, 0)
    sheen = share_gradient((width, max(1, height // 2)), (255, 255, 255, 12), (255, 255, 255, 0))
    body.alpha_composite(sheen, (0, 0))
    share_paint(img, share_round_mask((width, height), r), body, x1, y1)

    edge = Image.new("L", (width, height), 0)
    ImageDraw.Draw(edge).rounded_rectangle((0, 0, width - 1, height - 1), radius=r, outline=255, width=max(1, round(1.2 * s)))
    share_paint(img, edge, share_gradient((width, height), share_rgba(border, border_alpha[0]), share_rgba(border, border_alpha[1])), x1, y1)


def share_conic_gradient(size: int, c1: tuple[int, int, int], c2: tuple[int, int, int], start: float, sweep: float) -> Any:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), share_rgba(c2))
    draw = ImageDraw.Draw(img, "RGBA")
    steps = max(2, int(abs(sweep)))
    for step in range(steps):
        a0 = start + sweep * step / steps
        a1 = start + sweep * (step + 1.5) / steps
        draw.pieslice((0, 0, size - 1, size - 1), a0, a1, fill=share_rgba(share_mix(c1, c2, step / (steps - 1))))
    return img


def share_compact_money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


def share_period_label(days: int) -> str:
    if days == 1:
        return "LAST 24 HOURS"
    if days == 365:
        return "LAST 12 MONTHS"
    return f"LAST {days} DAYS"


def share_date_range_label(stats: dict[str, Any]) -> str:
    end_dt = datetime.fromisoformat(stats.get("end_local") or stats["end"])
    if stats.get("start_local") == stats.get("end_local") or stats["days"] == 1:
        return f"{end_dt.strftime('%b').upper()} {end_dt.day}, {end_dt.year}"
    start_dt = datetime.fromisoformat(stats.get("start_local") or stats["start"])
    start_year = f", {start_dt.year}" if start_dt.year != end_dt.year else ""
    return f"{start_dt.strftime('%b').upper()} {start_dt.day}{start_year} – {end_dt.strftime('%b').upper()} {end_dt.day}, {end_dt.year}"

class ShareStatsScene:
    """Lays out the share card, renders the static base once, then animates small regions on top."""

    def __init__(self, stats: dict[str, Any], cards: set[str], theme: str, title: str | None = None) -> None:
        theme = theme if theme in SHARE_THEMES_V2 else "titanium"
        palette = SHARE_THEMES_V2[theme]
        self.theme_key = theme
        self.fx = SHARE_THEME_FX.get(theme) or SHARE_THEME_FX["titanium"]
        self.strikes = bool(self.fx.get("strikes"))
        self._strike_layers: dict[int, Any] = {}
        self.stats = stats
        self.cards = cards
        self.theme_name = palette["name"]
        self.bg = palette["bg"]
        self.accent = palette["accent"]
        self.accent2 = palette["accent2"]
        self.title = re.sub(r"\s+", " ", str(title or "")).strip().upper()[:22] or "BUFFSQUAD"
        self.show_money = "spend" in cards
        self.show_ring = "success" in cards
        self.attempts = stats["checkouts"] + stats["issues"]

        items = list(stats.get("top_wins") or [])
        if not self.show_money:
            items.sort(key=lambda item: (item.get("quantity", 0), item.get("orders", 0)), reverse=True)
        self.items = items
        self.total_value = sum(self.item_value(item) for item in items) or 1.0

        tiles = []
        if "units" in cards:
            tiles.append(("units", "Units", stats["total_units"], self.accent, f"{len(stats.get('products') or [])} items", lambda v: f"+{v:,.0f}"))
        if "placed" in cards:
            tiles.append(("placed", "Placed", stats["checkouts"], SHARE_BLUE, f"of {self.attempts:,}", lambda v: f"{v:,.0f}"))
        if "cancelled" in cards:
            pct = round(stats["issues"] / self.attempts * 100) if self.attempts else 0
            tiles.append(("cancelled", "Cancelled", stats["issues"], SHARE_CORAL, f"{pct}%", lambda v: f"{v:,.0f}"))
        if "avg" in cards and self.show_money:
            tiles.append(("avg", "Avg Order", stats.get("avg_order", 0.0), SHARE_GOLD, "/order", lambda v: f"${v:,.2f}"))
        self.tiles = tiles

        self.timing: dict[str, tuple[int, int]] = {"hero": (2, 34), "ring": (4, 36), "bar": (8, 30), "chart": (14, 34), "sites": (18, 28), "top": (20, 30)}
        for index in range(len(tiles)):
            self.timing[f"tile{index}"] = (10 + index * 3, 28)
        self._layout()
        # Margin strips (logical) where animated lightning strikes; they sit outside every panel and other animated region.
        self.storm_boxes = ((0, 0, 52, self.height), (1028, 0, 1080, self.height), (52, 0, 1028, 50))
        # Long leaderboards stagger faster so the intro doesn't drag.
        row_step = min(3.0, 42 / max(len(self.board_rows), 1))
        for index in range(len(self.board_rows)):
            self.timing[f"row{index}"] = (24 + round(index * row_step), 26)
        self.intro_end = max(start + dur for start, dur in self.timing.values())
        self._ring_gradient = None
        self._chart_layer = None
        self._shimmer_band = None

    def item_value(self, item: dict[str, Any]) -> float:
        return float(item.get("spend") or 0) if self.show_money else float(item.get("quantity") or 0)

    def format_item_value(self, value: float) -> str:
        return f"${value:,.0f}" if self.show_money else f"{value:,.0f} unit{'' if round(value) == 1 else 's'}"

    # ---- layout ---------------------------------------------------------------------------------

    def _layout(self) -> None:
        top, gap = 176, 20
        sections: list[tuple[str, int]] = [("hero", 236)]
        if self.tiles:
            sections.append(("tiles", 120))
        if "activity" in self.cards:
            sections.append(("activity", 196))
        products = "products" in self.cards
        if products:
            sections.append(("top", 136))
        fixed = sum(height for _, height in sections) + gap * (len(sections) - 1)
        self.board_rows: list[dict[str, Any]] = []
        self.row_h, self.row_stride = 58, 66
        if products:
            # Every item gets a row; long lists switch to compact rows and the card grows taller to fit.
            self.board_rows = self.items[1:]
            if len(self.board_rows) > 6:
                self.row_h, self.row_stride = 44, 50
            board_h = 70 + self.row_stride * len(self.board_rows) if self.board_rows else 132
            sections.append(("board", board_h))
            fixed += gap + board_h
        self.height = max(SHARE_H, top + fixed + 70)
        bottom = self.height - 70
        leftover = max(0, bottom - top - fixed)
        if leftover > 40 and "activity" in self.cards:
            # Spare room goes to a taller chart before it turns into empty gaps.
            grow = min(leftover - 40, 280)
            sections = [(name, height + grow if name == "activity" else height) for name, height in sections]
            fixed += grow
            leftover -= grow
        if len(sections) > 1:
            gap += min(leftover // (len(sections) - 1), 28)
            leftover = max(0, bottom - top - (sum(h for _, h in sections) + gap * (len(sections) - 1)))
        y = top + min(leftover // 2, 24)
        self.boxes: dict[str, tuple[int, int, int, int]] = {}
        for name, height in sections:
            self.boxes[name] = (56, y, 1024, y + height)
            y += height + gap

        x1, y1, x2, _ = self.boxes["hero"]
        self.ring_center = (902, y1 + 116)
        self.ring_radius = 88
        self.hero_number_width = (self.ring_center[0] - self.ring_radius - 40 - 88) if self.show_ring else 880
        self.bar_x2 = (self.ring_center[0] - self.ring_radius - 44) if self.show_ring else 992

    # ---- timing ---------------------------------------------------------------------------------

    def progress(self, key: str, frame: int | None) -> float:
        if frame is None or key not in self.timing:
            return 1.0
        start, dur = self.timing[key]
        return share_ease((frame - start) / dur)

    def regions(self) -> list[tuple[str, tuple[int, int, int, int], Any, set[str]]]:
        """(name, logical box, draw fn, timing keys) — everything animated lives in exactly one region."""
        regions = [("pulse", (self.pill_dot[0] - 16, self.pill_dot[1] - 16, self.pill_dot[0] + 16, self.pill_dot[1] + 16), self.draw_pulse, set())]
        regions.append(("hero", self.boxes["hero"], self.draw_hero_dynamic, {"hero", "ring", "bar"}))
        if "tiles" in self.boxes:
            for index, box in enumerate(self.tile_boxes):
                value_box = (box[0] + 2, box[1] + 56, box[2] - 2, box[3] - 4)
                regions.append((f"tile{index}", value_box, lambda img, st, i=index: self.draw_tile_value(img, st, i), {f"tile{index}"}))
        if "activity" in self.boxes:
            regions.append(("chart", self.chart_region, self.draw_chart_dynamic, {"chart"}))
            regions.append(("sites", self.sites_region, self.draw_sites_dynamic, {"sites"}))
        if "top" in self.boxes:
            x1, y1, x2, y2 = self.boxes["top"]
            regions.append(("top", (x1 + 2, y1 + 2, x2 - 2, y2 - 2), self.draw_top_dynamic, {"top"}))
        for index, box in enumerate(self.row_boxes):
            regions.append((f"row{index}", box, lambda img, st, i=index: self.draw_board_row(img, st, i), {f"row{index}"}))
        if self.strikes:
            for slot, box in enumerate(self.storm_boxes):
                regions.append((f"storm{slot}", box, lambda img, st, k=slot: self.draw_strike(img, st, k), set()))
        return regions

    # ---- static base ----------------------------------------------------------------------------

    def build_base(self) -> Any:
        from PIL import Image, ImageDraw

        img = self.build_background()
        draw = ImageDraw.Draw(img, "RGBA")
        self.draw_header(img)
        self.draw_hero_static(img)
        if "tiles" in self.boxes:
            self.draw_tiles_static(img)
        if "activity" in self.boxes:
            self.draw_activity_static(img)
        if "top" in self.boxes:
            self.draw_top_static(img)
        self.row_boxes: list[tuple[int, int, int, int]] = []
        if "board" in self.boxes:
            self.draw_board_static(img)
        footer_font = share_font_v2(11.5, "semibold")
        footer = [
            (58, f"{self.theme_name.upper()}  ·  {self.stats.get('period_label') or share_period_label(self.stats['days'])}", "l"),
            (1022, f"GENERATED {self.stats.get('generated_label', '')}", "r"),
        ]
        strength = float(self.fx.get("strength", 0))
        if strength:
            # Soft dark backing keeps the footer readable over the aura flames.
            from PIL import Image, ImageFilter

            shade = Image.new("L", img.size, 0)
            shade_draw = ImageDraw.Draw(shade)
            for x, text, align in footer:
                share_text(shade_draw, x, self.height - 32, text, footer_font, 255, align=align, tracking=1.6)
            band_top = (self.height - 80) * SHARE_SCALE
            shade = shade.crop((0, band_top, img.width, img.height))
            shade = shade.filter(ImageFilter.GaussianBlur(6 * SHARE_SCALE)).point(lambda v: min(255, round(v * 3.2 * strength)))
            share_paint(img, shade, (0, 0, 0, 255), 0, band_top)
        draw = ImageDraw.Draw(img, "RGBA")
        footer_color = share_mix(SHARE_DIM, SHARE_TEXT, 0.5 * strength)
        for x, text, align in footer:
            share_text(draw, x, self.height - 32, text, footer_font, (*footer_color, 255), align=align, tracking=1.6)
        return img

    def build_background(self) -> Any:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter

        s = SHARE_SCALE
        small_size = (SHARE_W // 4, self.height // 4)
        bg = share_gradient(small_size, self.bg[0], self.bg[1])
        glow = Image.new("RGBA", small_size, (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.ellipse((-90, -110, 190, 120), fill=share_rgba(self.accent, 78))
        glow_draw.ellipse((150, 40, 360, 230), fill=share_rgba(self.accent2, 46))
        glow_draw.ellipse((-80, 200, 150, 400), fill=share_rgba(SHARE_MINT, 20))
        glow_draw.ellipse((160, 250, 360, 420), fill=share_rgba(SHARE_GOLD, 14))
        bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(34)))
        vignette = Image.new("L", small_size, 255)
        ImageDraw.Draw(vignette).ellipse((-60, -50, small_size[0] + 60, small_size[1] + 50), fill=0)
        vignette = vignette.filter(ImageFilter.GaussianBlur(40)).point(lambda v: v * 150 // 255)
        share_paint(bg, vignette, (0, 0, 0, 255), 0, 0)
        self.draw_aura(bg)
        img = bg.resize((SHARE_W * s, self.height * s), Image.Resampling.BICUBIC)
        self.draw_static_bolts(img)

        dots = Image.new("L", (SHARE_W * s, self.height * s), 0)
        dot_draw = ImageDraw.Draw(dots)
        dot_r = 1.1 * s
        for gy in range(14, self.height, 26):
            for gx in range(14, SHARE_W, 26):
                dot_draw.ellipse((gx * s - dot_r, gy * s - dot_r, gx * s + dot_r, gy * s + dot_r), fill=255)
        fade = share_gradient((SHARE_W * s, self.height * s), (0, 0, 0, 30), (0, 0, 0, 6)).getchannel("A")
        share_paint(img, ImageChops.multiply(dots, fade.point(lambda v: min(255, v * 8))).point(lambda v: v * 22 // 255), (255, 255, 255, 255), 0, 0)

        # Fine film grain at output resolution (upscaled NEAREST so it survives the supersample reduce).
        noise = Image.effect_noise((SHARE_W, self.height), 40).resize((SHARE_W * s, self.height * s), Image.Resampling.NEAREST)
        share_paint(img, noise.point(lambda v: max(0, v - 128) * 16 // 128), (255, 255, 255, 255), 0, 0)
        share_paint(img, noise.point(lambda v: max(0, 128 - v) * 22 // 128), (0, 0, 0, 255), 0, 0)
        return img.convert("RGB")

    def draw_aura(self, bg: Any) -> None:
        """Flame-like aura licking up from the bottom edge and climbing the sides, plus a halo behind the title (quarter res)."""
        import random

        from PIL import Image, ImageChops, ImageDraw, ImageFilter

        strength = float(self.fx.get("strength", 0))
        if strength <= 0:
            return
        core_color, edge_color = self.fx["aura"]
        rng = random.Random(f"aura-{self.theme_key}")
        w, h = bg.size
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)
        for _ in range(52):
            x, tall, half, lean = rng.uniform(-10, w + 10), rng.uniform(18, 70), rng.uniform(4, 12), rng.uniform(-12, 12)
            draw.polygon([(x - half, h + 4), (x + half, h + 4), (x + lean, h - tall)], fill=rng.randint(110, 230))
        for side in (0, 1):
            for _ in range(40):
                y, reach, tall = rng.uniform(30, h + 10), rng.uniform(8, 30), rng.uniform(18, 60)
                x0 = -2 if side == 0 else w + 2
                tip = reach if side == 0 else w - reach
                draw.polygon([(x0, y), (x0, y - tall * 0.5), (tip, y - tall)], fill=rng.randint(90, 220))
        # Flames burn hottest low on the card and thin out toward the top.
        fade = Image.linear_gradient("L").resize((w, h)).point(lambda v: 50 + v * 205 // 255)
        mask = ImageChops.multiply(mask, fade).filter(ImageFilter.GaussianBlur(4))
        share_paint(bg, mask.filter(ImageFilter.GaussianBlur(10)).point(lambda v: round(v * 0.9 * strength)), share_rgba(edge_color), 0, 0)
        share_paint(bg, mask.point(lambda v: round(v * 0.75 * strength)), share_rgba(edge_color), 0, 0)
        share_paint(bg, mask.point(lambda v: round(max(0, v - 100) * 255 / 155 * 0.8 * strength)), share_rgba(core_color), 0, 0)
        halo = Image.new("L", (w, h), 0)
        ImageDraw.Draw(halo).ellipse((-30, -24, 200, 50), fill=round(120 * strength))
        share_paint(bg, halo.filter(ImageFilter.GaussianBlur(14)), share_rgba(edge_color), 0, 0)

    def draw_static_bolts(self, img: Any) -> None:
        import random

        count = int(self.fx.get("bolts", 0))
        if count <= 0:
            return
        rng = random.Random(f"bolt-{self.theme_key}")
        spots = [
            (lambda: ((rng.uniform(14, 40), rng.uniform(170, 420)), (rng.uniform(10, 44), rng.uniform(600, 860))), 0.14, 2),
            (lambda: ((rng.uniform(1040, 1066), rng.uniform(320, 600)), (rng.uniform(1036, 1070), rng.uniform(820, 1100))), 0.14, 2),
            (lambda: ((rng.uniform(620, 720), -10), (rng.uniform(820, 960), rng.uniform(34, 50))), 0.2, 2),
            (lambda: ((rng.uniform(1046, 1070), -10), (rng.uniform(1030, 1062), rng.uniform(170, 300))), 0.16, 1),
        ]
        alpha = 0.45 + 0.4 * float(self.fx.get("strength", 0.5))
        for make, spread, branches in spots[:count]:
            start, end = make()
            share_draw_bolts(img, share_bolt_paths(rng, start, end, spread, branches), self.fx["bolt"], self.fx["core"], alpha)

    def draw_header(self, img: Any) -> None:
        from PIL import ImageDraw

        title_font = share_fit_font_v2(self.title, 600, 60, "black", min_size=34)
        strength = float(self.fx.get("strength", 0))
        title_glow = self.fx["aura"][1] if strength else self.accent
        share_gradient_text(img, 54, 104, self.title, title_font, (255, 255, 255), share_mix((255, 255, 255), self.accent, 0.5), glow=title_glow, glow_alpha=round(70 + 70 * strength), glow_blur=14 + 6 * strength)
        share_gradient_bar(img, (58, 120, 150, 125), self.accent, self.accent2, glow=True)
        draw = ImageDraw.Draw(img, "RGBA")
        sub_font = share_font_v2(14, "semibold")
        share_text(draw, 160, 126, "CHECKOUT REPORT", sub_font, (*SHARE_MUTED, 255), tracking=2.6)
        share_text(draw, 58, 154, self.stats.get("period_label") or share_period_label(self.stats["days"]), share_font_v2(12, "semibold"), (*self.accent, 230), tracking=2.2)

        pill_font = share_font_v2(13, "semibold")
        date_text = share_date_range_label(self.stats)
        text_w = share_text_width(date_text, pill_font, 1.6)
        pill = (1024 - text_w - 66, 62, 1024, 106)
        share_glass_panel(img, pill, radius=22, border_alpha=(70, 22), fill_top=(22, 28, 40, 220), fill_bottom=(14, 18, 27, 230))
        self.pill_dot = (round(pill[0] + 24), 84)
        draw = ImageDraw.Draw(img, "RGBA")
        share_text(draw, pill[0] + 42, 89, date_text, pill_font, (*SHARE_TEXT, 255), tracking=1.6)

    def draw_hero_static(self, img: Any) -> None:
        from PIL import ImageDraw

        x1, y1, x2, y2 = self.boxes["hero"]
        share_glass_panel(img, self.boxes["hero"], radius=30, tint=self.accent, tint_alpha=46)
        draw = ImageDraw.Draw(img, "RGBA")
        label = "TOTAL CHECKOUT VALUE" if self.show_money else "ORDERS PLACED"
        share_text(draw, 88, y1 + 44, label, share_font_v2(13, "semibold"), (*SHARE_MUTED, 255), tracking=2.4)
        if self.show_money:
            sub = f"{self.stats['checkouts']:,} orders  ·  {self.stats['total_units']:,} units  ·  {self.attempts:,} attempts"
        else:
            sub = f"{self.stats['total_units']:,} units  ·  {self.attempts:,} attempts"
        share_text(draw, 89, y1 + 166, sub, share_font_v2(16, "regular"), (*SHARE_MUTED, 255))
        draw.rounded_rectangle(share_box((88, y1 + 184, self.bar_x2, y1 + 194)), radius=5 * SHARE_SCALE, fill=(255, 255, 255, 16))

        legend_font = share_font_v2(11.5, "semibold")
        placed_pct = round(self.stats["checkouts"] / self.attempts * 100) if self.attempts else 0
        lx = 88
        for color, text in ((SHARE_MINT, f"PLACED {placed_pct}%"), (SHARE_CORAL, f"CANCELLED {100 - placed_pct if self.attempts else 0}%")):
            draw.ellipse(share_box((lx, y1 + 211, lx + 8, y1 + 219)), fill=share_rgba(color))
            share_text(draw, lx + 15, y1 + 219, text, legend_font, (*SHARE_MUTED, 255), tracking=1.4)
            lx += 15 + share_text_width(text, legend_font, 1.4) + 22
        if "top_issue" in self.cards and self.stats["issues"]:
            issue = str(self.stats.get("top_issue") or "").strip()
            if issue and issue != "None":
                issue_font = share_font_v2(12, "semibold")
                issue_text = share_ellipsize(f"TOP ISSUE  {issue.upper()}", issue_font, self.bar_x2 - lx - 8)
                share_text(draw, self.bar_x2, y1 + 219, issue_text, issue_font, (*share_mix(SHARE_CORAL, (255, 255, 255), 0.25), 235), align="r")

        if self.show_ring:
            cx, cy = self.ring_center
            r = self.ring_radius
            for tick in range(72):
                angle = math.radians(tick * 5 - 90)
                inner, outer = r + 12, r + (18 if tick % 6 == 0 else 15)
                draw.line(
                    [((cx + math.cos(angle) * inner) * SHARE_SCALE, (cy + math.sin(angle) * inner) * SHARE_SCALE),
                     ((cx + math.cos(angle) * outer) * SHARE_SCALE, (cy + math.sin(angle) * outer) * SHARE_SCALE)],
                    fill=(255, 255, 255, 34 if tick % 6 == 0 else 16),
                    width=max(1, round(1.2 * SHARE_SCALE)),
                )
            draw.arc(share_box((cx - r, cy - r, cx + r, cy + r)), 0, 360, fill=(255, 255, 255, 18), width=16 * SHARE_SCALE)
            share_text(draw, cx, cy + 34, "STICK RATE", share_font_v2(10.5, "semibold"), (*SHARE_MUTED, 255), align="m", tracking=1.8)

    def draw_tiles_static(self, img: Any) -> None:
        from PIL import ImageDraw

        x1, y1, x2, y2 = self.boxes["tiles"]
        count = len(self.tiles)
        gap = 16
        width = (x2 - x1 - gap * (count - 1)) / count
        self.tile_boxes = []
        for index, (_, label, _, color, caption, _) in enumerate(self.tiles):
            tx1 = round(x1 + index * (width + gap))
            box = (tx1, y1, round(tx1 + width), y2)
            self.tile_boxes.append(box)
            share_glass_panel(img, box, radius=22, tint=color, tint_alpha=40, tint_at="tr", border=share_mix((255, 255, 255), color, 0.35), border_alpha=(66, 12))
            share_gradient_bar(img, (box[0] + 22, y1 + 20, box[0] + 46, y1 + 24), color, share_mix(color, (255, 255, 255), 0.4), glow=True)
            draw = ImageDraw.Draw(img, "RGBA")
            share_text(draw, box[0] + 22, y1 + 47, label.upper(), share_font_v2(12, "semibold"), (*SHARE_MUTED, 255), tracking=2.0)
            share_text(draw, box[2] - 20, y1 + 47, caption, share_font_v2(12, "regular"), (*SHARE_DIM, 255), align="r")

    def draw_activity_static(self, img: Any) -> None:
        from PIL import Image, ImageDraw

        x1, y1, x2, y2 = self.boxes["activity"]
        share_glass_panel(img, self.boxes["activity"], radius=26, tint=self.accent2, tint_alpha=26, tint_at="tr")
        draw = ImageDraw.Draw(img, "RGBA")
        head = share_font_v2(13, "semibold")
        share_text(draw, 88, y1 + 38, "ACTIVITY", head, (*SHARE_TEXT, 255), tracking=2.4)
        legend = share_font_v2(11, "semibold")
        lx = 676
        for color, text in ((SHARE_CORAL, "CANCELLED"), (self.accent, "PLACED")):
            text_w = share_text_width(text, legend, 1.2)
            share_text(draw, lx, y1 + 37, text, legend, (*SHARE_MUTED, 255), align="r", tracking=1.2)
            draw.rounded_rectangle(share_box((lx - text_w - 22, y1 + 31, lx - text_w - 8, y1 + 34)), radius=SHARE_SCALE, fill=share_rgba(color))
            lx -= text_w + 36

        self.plot = (90, y1 + 62, 676, y2 - 46)
        px1, py1, px2, py2 = self.plot
        for step in range(4):
            gy = py1 + (py2 - py1) * step / 3
            for gx in range(px1, px2, 8):
                draw.line(share_box((gx, gy, gx + 3, gy)), fill=(255, 255, 255, 18 if step == 3 else 10), width=SHARE_SCALE)
        labels = self.stats.get("timeline_labels") or []
        label_font = share_font_v2(11, "semibold")
        if len(labels) > 1:
            picks = range(len(labels)) if len(labels) <= 8 else sorted({round(i * (len(labels) - 1) / 4) for i in range(5)})
            for index in picks:
                lx = px1 + (px2 - px1) * index / (len(labels) - 1)
                align = "l" if index == 0 else "r" if index == len(labels) - 1 else "m"
                share_text(draw, lx, y2 - 20, labels[index], label_font, (*SHARE_DIM, 255), align=align, tracking=0.8)
        self.chart_region = (px1 - 14, py1 - 34, px2 + 14, py2 + 10)

        draw.line(share_box((708, y1 + 26, 708, y2 - 26)), fill=(255, 255, 255, 18), width=SHARE_SCALE)
        share_text(draw, 736, y1 + 38, "TOP SITES", head, (*SHARE_TEXT, 255), tracking=2.4)
        self.site_rows = []
        sites = list(self.stats.get("sites") or [])[:3]
        all_total = sum((s["spend"] if self.show_money else s["orders"]) for s in self.stats.get("sites") or []) or 1
        name_font = share_font_v2(14, "semibold")
        meta_font = share_font_v2(11.5, "semibold")
        if not sites:
            share_text(draw, 736, y1 + 104, "No sites yet", share_font_v2(15, "regular"), (*SHARE_MUTED, 255))
        for index, site in enumerate(sites):
            ry = y1 + 74 + index * 44
            # Meta sits inline, right-aligned before the (animated) percentage column.
            site_meta = share_compact_money(site["spend"]) if self.show_money else f"{site['orders']:,} orders"
            share_text(draw, 948, ry, site_meta, meta_font, (*SHARE_DIM, 255), align="r")
            name_width = 948 - 736 - share_text_width(site_meta, meta_font) - 14
            share_text(draw, 736, ry, share_ellipsize(str(site["site"]).upper(), name_font, name_width), name_font, (*SHARE_TEXT, 255))
            draw.rounded_rectangle(share_box((736, ry + 10, 996, ry + 15)), radius=round(2.5 * SHARE_SCALE), fill=(255, 255, 255, 16))
            value = site["spend"] if self.show_money else site["orders"]
            self.site_rows.append((ry, value / all_total, [self.accent, self.accent2, SHARE_MINT][index]))
        self.sites_region = (730, y1 + 56, 1002, y2 - 8)

    def draw_top_static(self, img: Any) -> None:
        from PIL import ImageDraw

        x1, y1, x2, y2 = self.boxes["top"]
        share_glass_panel(img, self.boxes["top"], radius=26, tint=SHARE_GOLD, tint_alpha=36, tint_at="tr", border=share_mix((255, 255, 255), SHARE_GOLD, 0.5), border_alpha=(80, 14))
        item = self.items[0] if self.items else None
        badge = (88, y1 + 26, 140, y1 + 78)
        share_paint(img, share_round_mask((52 * SHARE_SCALE, 52 * SHARE_SCALE), 16 * SHARE_SCALE), share_gradient((52 * SHARE_SCALE, 52 * SHARE_SCALE), (255, 228, 140), SHARE_GOLD_DEEP), badge[0] * SHARE_SCALE, badge[1] * SHARE_SCALE)
        draw = ImageDraw.Draw(img, "RGBA")
        share_text(draw, 114, y1 + 61, "#1", share_font_v2(21, "num-bold"), (40, 26, 4, 255), align="m")
        share_text(draw, 160, y1 + 40, "TOP HIT", share_font_v2(12.5, "semibold"), (*share_mix(SHARE_GOLD, (255, 255, 255), 0.2), 255), tracking=2.4)
        name_font = share_font_v2(25, "semibold")
        name = item.get("product") if item else "No checkouts in this range"
        share_text(draw, 159, y1 + 74, share_ellipsize(name, name_font, 560), name_font, (*SHARE_TEXT, 255))
        if item:
            meta = f"{str(item.get('site') or 'Unknown').upper()}  ·  {item.get('orders', 0):,} orders  ·  {item.get('quantity', 0):,} units"
            share_text(draw, 160, y1 + 100, meta, share_font_v2(13.5, "semibold"), (*SHARE_MUTED, 255))
        draw.rounded_rectangle(share_box((88, y1 + 114, 992, y1 + 120)), radius=3 * SHARE_SCALE, fill=(255, 255, 255, 16))

    def draw_board_static(self, img: Any) -> None:
        from PIL import ImageDraw

        x1, y1, x2, y2 = self.boxes["board"]
        share_glass_panel(img, self.boxes["board"], radius=28)
        draw = ImageDraw.Draw(img, "RGBA")
        share_text(draw, 88, y1 + 42, "LEADERBOARD", share_font_v2(13, "semibold"), (*SHARE_TEXT, 255), tracking=2.4)
        total_items = len(self.items)
        share_text(draw, 992, y1 + 42, f"ALL {total_items} ITEM{'S' if total_items != 1 else ''}", share_font_v2(11, "semibold"), (*SHARE_DIM, 255), align="r", tracking=1.6)
        if not self.board_rows:
            share_text(draw, 540, y1 + 92, "No additional items in this range", share_font_v2(17, "regular"), (*SHARE_MUTED, 255), align="m")
        for index in range(len(self.board_rows)):
            ry = y1 + 62 + index * self.row_stride
            self.row_boxes.append((80, ry, 1000, ry + self.row_h))

    # ---- dynamic regions ------------------------------------------------------------------------

    def draw_strike(self, img: Any, state: dict[str, Any], slot: int) -> None:
        """Animated lightning flash inside one margin strip; state['strike'] = (strike number, intensity)."""
        import random

        from PIL import Image

        strike = state.get("strike")
        if not strike or strike[0] % len(self.storm_boxes) != slot:
            return
        variant = strike[0] % (len(self.storm_boxes) * 2)
        x1, y1, x2, y2 = self.storm_boxes[slot]
        hi_box = share_box((x1, y1, x2, y2))
        layer = self._strike_layers.get(variant)
        if layer is None:
            rng = random.Random(f"strike-{self.theme_key}-{variant}")
            if x2 - x1 > y2 - y1:
                sx = rng.uniform(x1 + 60, x1 + 420)
                start, end = (sx, rng.uniform(8, 40)), (sx + rng.uniform(320, 540), rng.uniform(8, 40))
                lo, hi, axis = y1 + 5, y2 - 5, 1
            else:
                sy = rng.uniform(140, 620)
                start, end = (rng.uniform(x1 + 12, x2 - 12), sy), (rng.uniform(x1 + 10, x2 - 10), sy + rng.uniform(380, 680))
                lo, hi, axis = x1 + 5, x2 - 5, 0
            # Keep the bolt inside its strip so it never bleeds under the panels.
            paths = [
                ([tuple(min(max(v, lo), hi) if i == axis else v for i, v in enumerate(pt)) for pt in pts], weight)
                for pts, weight in share_bolt_paths(rng, start, end, 0.09, 2)
            ]
            layer = Image.new("RGBA", (hi_box[2] - hi_box[0], hi_box[3] - hi_box[1]), (0, 0, 0, 0))
            share_draw_bolts(layer, paths, self.fx["bolt"], self.fx["core"], 1.0, origin=(x1, y1))
            self._strike_layers[variant] = layer
        intensity = strike[1]
        if intensity < 1:
            layer = layer.copy()
            layer.putalpha(layer.getchannel("A").point(lambda v: round(v * intensity)))
        share_composite(img, layer, hi_box[0], hi_box[1])

    def draw_pulse(self, img: Any, state: dict[str, Any]) -> None:
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img, "RGBA")
        cx, cy = self.pill_dot
        phase = state.get("pulse", 0.0)
        ring = 4 + 9 * phase
        alpha = round(150 * (1 - phase) ** 1.6)
        if alpha > 2:
            draw.ellipse(share_box((cx - ring, cy - ring, cx + ring, cy + ring)), outline=share_rgba(SHARE_MINT, alpha), width=max(1, round(1.6 * SHARE_SCALE)))
        draw.ellipse(share_box((cx - 7, cy - 7, cx + 7, cy + 7)), fill=share_rgba(SHARE_MINT, 46))
        draw.ellipse(share_box((cx - 4, cy - 4, cx + 4, cy + 4)), fill=share_rgba(SHARE_MINT))

    def draw_hero_dynamic(self, img: Any, state: dict[str, Any]) -> None:
        from PIL import Image, ImageChops, ImageDraw, ImageFilter

        frame = state.get("intro")
        x1, y1, x2, y2 = self.boxes["hero"]
        if self.show_money:
            target = float(self.stats["total_spend"])
            final_text = f"${target:,.2f}"
            value_text = f"${target * self.progress('hero', frame):,.2f}"
        else:
            target = float(self.stats["checkouts"])
            final_text = f"{target:,.0f}"
            value_text = f"{target * self.progress('hero', frame):,.0f}"
        number_font = share_fit_font_v2(final_text, self.hero_number_width, 94, "num", min_size=48)
        share_gradient_text(img, 84, y1 + 136, value_text, number_font, (255, 255, 255), (196, 212, 232), glow=self.accent, glow_alpha=46, glow_blur=16)

        bar_p = self.progress("bar", frame)
        span = self.bar_x2 - 88
        if self.attempts and bar_p > 0:
            placed_w = span * self.stats["checkouts"] / self.attempts * bar_p
            cancelled_w = span * self.stats["issues"] / self.attempts * bar_p
            gap = 4 if placed_w > 6 and cancelled_w > 6 else 0
            if placed_w > 3:
                share_gradient_bar(img, (88, y1 + 184, 88 + placed_w - gap / 2, y1 + 194), share_mix(SHARE_MINT, (20, 160, 120), 0.15), SHARE_MINT)
            if cancelled_w > 3:
                start = 88 + placed_w + gap / 2
                share_gradient_bar(img, (start, y1 + 184, start + cancelled_w - gap / 2, y1 + 194), SHARE_CORAL, share_mix(SHARE_CORAL, (180, 40, 90), 0.15))

        if self.show_ring:
            self.draw_ring(img, self.progress("ring", frame))

        shimmer = state.get("shimmer")
        if shimmer is not None:
            s = SHARE_SCALE
            width, height = (x2 - x1) * s, (y2 - y1) * s
            if self._shimmer_band is None:
                band = Image.new("L", (round(260 * s), height), 0)
                ImageDraw.Draw(band).polygon([(110 * s, 0), (170 * s, 0), (150 * s, height), (90 * s, height)], fill=255)
                self._shimmer_band = band.filter(ImageFilter.GaussianBlur(22 * s)).point(lambda v: v * 30 // 255)
            mask = Image.new("L", (width, height), 0)
            mask.paste(self._shimmer_band, (round((-300 + shimmer * (x2 - x1 + 340)) * s), 0))
            share_paint(img, ImageChops.multiply(mask, share_round_mask((width, height), 30 * s)), (255, 255, 255, 255), x1 * s, y1 * s)

    def draw_ring(self, img: Any, progress: float) -> None:
        from PIL import Image, ImageDraw, ImageFilter

        s = SHARE_SCALE
        cx, cy = self.ring_center
        r = self.ring_radius
        rate = max(0, min(int(self.stats["success_rate"]), 100))
        sweep = 360 * rate / 100 * progress
        pad = 26
        size = (r + pad) * 2 * s
        ox, oy = (cx - r - pad) * s, (cy - r - pad) * s
        ring_box = (pad * s, pad * s, (pad + 2 * r) * s, (pad + 2 * r) * s)
        if sweep > 0.5:
            if self._ring_gradient is None:
                full = max(360 * rate / 100, 1)
                self._ring_gradient = share_conic_gradient(size, SHARE_MINT, self.accent, -90, full)
            mask = Image.new("L", (size, size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.arc(ring_box, -90, -90 + sweep, fill=255, width=16 * s)
            mid = r * s - 8 * s
            for angle in (-90, -90 + sweep):
                rad = math.radians(angle)
                ex = (pad + r) * s + math.cos(rad) * mid
                ey = (pad + r) * s + math.sin(rad) * mid
                mask_draw.ellipse((ex - 8 * s, ey - 8 * s, ex + 8 * s, ey + 8 * s), fill=255)
            halo = mask.filter(ImageFilter.GaussianBlur(9 * s)).point(lambda v: v * 120 // 255)
            share_paint(img, halo, share_rgba(self.accent), ox, oy)
            share_paint(img, mask, self._ring_gradient, ox, oy)
        draw = ImageDraw.Draw(img, "RGBA")
        share_text(draw, cx, cy + 14, f"{round(rate * progress)}%", share_font_v2(44, "num"), (*SHARE_TEXT, 255), align="m")

    def draw_tile_value(self, img: Any, state: dict[str, Any], index: int) -> None:
        _, _, target, color, _, fmt = self.tiles[index]
        box = self.tile_boxes[index]
        progress = self.progress(f"tile{index}", state.get("intro"))
        font = share_fit_font_v2(fmt(float(target)), box[2] - box[0] - 44, 42, "num", min_size=24)
        value = float(target) * progress
        share_gradient_text(img, box[0] + 21, box[1] + 100, fmt(value), font, color, color, glow=color, glow_alpha=34, glow_blur=10)

    def chart_layer(self) -> tuple[Any, list[tuple[float, float]]]:
        from PIL import Image, ImageDraw

        if self._chart_layer is not None:
            return self._chart_layer
        s = SHARE_SCALE
        rx1, ry1, rx2, ry2 = self.chart_region
        layer = Image.new("RGBA", ((rx2 - rx1) * s, (ry2 - ry1) * s), (0, 0, 0, 0))
        px1, py1, px2, py2 = self.plot
        placed = list(self.stats.get("timeline_placed") or [0, 0])
        cancelled = list(self.stats.get("timeline_cancelled") or [0, 0])
        peak = max(max(placed), max(cancelled), 1) * 1.12

        def curve(values: list[int]) -> list[tuple[float, float]]:
            n = len(values)
            pts = [((px1 + (px2 - px1) * i / (n - 1) - rx1) * s, (py2 - (py2 - py1) * v / peak - ry1) * s) for i, v in enumerate(values)]
            out: list[tuple[float, float]] = []
            for i in range(n - 1):
                p0, p1, p2, p3 = pts[max(i - 1, 0)], pts[i], pts[i + 1], pts[min(i + 2, n - 1)]
                for step in range(12):
                    t = step / 12
                    t2, t3 = t * t, t * t * t
                    x = 0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
                    y = 0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
                    out.append((x, min(max(y, (py1 - ry1) * s - 4 * s), (py2 - ry1) * s)))
            out.append(pts[-1])
            return out

        base_y = (py2 - ry1) * s
        placed_pts = curve(placed)
        cancelled_pts = curve(cancelled)

        area = Image.new("L", layer.size, 0)
        ImageDraw.Draw(area).polygon(placed_pts + [(placed_pts[-1][0], base_y), (placed_pts[0][0], base_y)], fill=255)
        share_paint(layer, area, share_gradient(layer.size, share_rgba(self.accent, 120), share_rgba(self.accent, 0)), 0, 0)

        line = Image.new("L", layer.size, 0)
        ImageDraw.Draw(line).line(cancelled_pts, fill=170, width=round(2 * s), joint="curve")
        share_paint(layer, line, share_rgba(SHARE_CORAL), 0, 0)

        line = Image.new("L", layer.size, 0)
        ImageDraw.Draw(line).line(placed_pts, fill=255, width=round(3 * s), joint="curve")
        share_paint(layer, line, share_gradient(layer.size, self.accent2, self.accent, horizontal=True), 0, 0)

        peak_index = max(range(len(placed)), key=lambda i: placed[i])
        if placed[peak_index] > 0:
            draw = ImageDraw.Draw(layer)
            n = len(placed)
            peak_x = (px1 + (px2 - px1) * peak_index / (n - 1) - rx1) * s
            peak_y = (py2 - (py2 - py1) * placed[peak_index] / peak - ry1) * s
            draw.ellipse((peak_x - 7 * s, peak_y - 7 * s, peak_x + 7 * s, peak_y + 7 * s), fill=share_rgba(self.accent, 70))
            draw.ellipse((peak_x - 4 * s, peak_y - 4 * s, peak_x + 4 * s, peak_y + 4 * s), fill=(255, 255, 255, 255))
            labels = self.stats.get("timeline_labels") or []
            label = f"PEAK {placed[peak_index]:,}" + (f" · {labels[peak_index]}" if peak_index < len(labels) else "")
            font = share_font_v2(10.5, "semibold")
            label_w = share_text_width(label, font, 1.0)
            lx = min(max(peak_x / s - label_w / 2, 8), (rx2 - rx1) - label_w - 8)
            ly = max(peak_y / s - 14, 14)
            draw.rounded_rectangle(((lx - 7) * s, (ly - 11) * s, (lx + label_w + 7) * s, (ly + 4) * s), radius=7 * s, fill=(12, 16, 25, 235), outline=share_rgba(self.accent, 90), width=s)
            share_text(draw, lx, ly, label, font, (*SHARE_TEXT, 255), tracking=1.0)
        self._chart_layer = (layer, placed_pts)
        return self._chart_layer

    def draw_chart_dynamic(self, img: Any, state: dict[str, Any]) -> None:
        from PIL import ImageDraw

        s = SHARE_SCALE
        progress = self.progress("chart", state.get("intro"))
        if progress <= 0:
            return
        layer, pts = self.chart_layer()
        rx1, ry1, _, _ = self.chart_region
        px1, _, px2, _ = self.plot
        head_x = (px1 - rx1 + (px2 - px1) * progress) * s
        if progress >= 1:
            share_composite(img, layer, rx1 * s, ry1 * s)
            return
        share_composite(img, layer.crop((0, 0, round(head_x), layer.height)), rx1 * s, ry1 * s)
        head_y = next((y for x, y in pts if x >= head_x), pts[-1][1])
        draw = ImageDraw.Draw(img, "RGBA")
        hx, hy = rx1 * s + head_x, ry1 * s + head_y
        draw.ellipse((hx - 9 * s, hy - 9 * s, hx + 9 * s, hy + 9 * s), fill=share_rgba(self.accent, 60))
        draw.ellipse((hx - 4 * s, hy - 4 * s, hx + 4 * s, hy + 4 * s), fill=(255, 255, 255, 255))

    def draw_sites_dynamic(self, img: Any, state: dict[str, Any]) -> None:
        from PIL import ImageDraw

        progress = self.progress("sites", state.get("intro"))
        draw = ImageDraw.Draw(img, "RGBA")
        font = share_font_v2(14, "num")
        for ry, share, color in self.site_rows:
            share_text(draw, 996, ry, f"{share * 100 * progress:.0f}%", font, (*SHARE_TEXT, 255), align="r")
            width = 260 * share * progress
            if width > 5:
                share_gradient_bar(img, (736, ry + 10, 736 + width, ry + 15), share_mix(color, (255, 255, 255), 0.15), color)

    def draw_top_dynamic(self, img: Any, state: dict[str, Any]) -> None:
        from PIL import ImageDraw

        x1, y1, _, _ = self.boxes["top"]
        progress = self.progress("top", state.get("intro"))
        item = self.items[0] if self.items else None
        value = self.item_value(item) if item else 0.0
        final_text = self.format_item_value(value)
        font = share_fit_font_v2(final_text, 250, 40 if self.show_money else 32, "num", min_size=22)
        share_gradient_text(img, 992, y1 + 72, self.format_item_value(value * progress), font, (255, 236, 170), SHARE_GOLD, align="r", glow=SHARE_GOLD, glow_alpha=56, glow_blur=12)
        share_pct = value / self.total_value if item else 0.0
        draw = ImageDraw.Draw(img, "RGBA")
        share_text(draw, 992, y1 + 100, f"{share_pct * 100 * progress:.1f}% OF TOTAL", share_font_v2(11, "semibold"), (*SHARE_MUTED, 255), align="r", tracking=1.6)
        width = 904 * share_pct * progress
        if width > 6:
            share_gradient_bar(img, (88, y1 + 114, 88 + width, y1 + 120), SHARE_GOLD_DEEP, (255, 228, 140), glow=True)

    def draw_board_row(self, img: Any, state: dict[str, Any], index: int) -> None:
        from PIL import ImageDraw

        s = SHARE_SCALE
        item = self.board_rows[index]
        rank = index + 2
        x1, y1, x2, y2 = self.row_boxes[index]
        progress = self.progress(f"row{index}", state.get("intro"))
        draw = ImageDraw.Draw(img, "RGBA")
        draw.rounded_rectangle(share_box((x1, y1, x2, y2)), radius=16 * s, fill=(255, 255, 255, 9 if index % 2 == 0 else 5), outline=(255, 255, 255, 18), width=s)

        value = self.item_value(item)
        top_value = self.item_value(self.items[0]) if self.items else 1.0
        fill_w = round((x2 - x1) * min(value / max(top_value, 1e-9), 1.0) * progress * s)
        if fill_w > 4 * s:
            size = ((x2 - x1) * s, (y2 - y1) * s)
            mask = share_round_mask(size, 16 * s)
            mask.paste(0, (fill_w, 0, size[0], size[1]))
            share_paint(img, mask, share_gradient(size, share_rgba(self.accent, 54), share_rgba(self.accent2, 14), horizontal=True), x1 * s, y1 * s)

        compact = y2 - y1 < 58
        medal = {2: ((236, 240, 248), (150, 162, 182)), 3: ((246, 196, 150), (176, 108, 62))}.get(rank)
        badge_size = 30 if compact else 36
        badge = (x1 + (12 if compact else 16), y1 + (y2 - y1 - badge_size) / 2)
        badge = (*badge, badge[0] + badge_size, badge[1] + badge_size)
        bsize = (badge_size * s, badge_size * s)
        if medal:
            share_paint(img, share_round_mask(bsize, 10 * s), share_gradient(bsize, medal[0], medal[1]), round(badge[0] * s), round(badge[1] * s))
            badge_text = (24, 26, 34, 255)
        else:
            draw.rounded_rectangle(share_box(badge), radius=10 * s, fill=share_rgba(self.accent, 30), outline=share_rgba(self.accent, 120), width=s)
            badge_text = (*share_mix(self.accent, (255, 255, 255), 0.4), 255)
        draw = ImageDraw.Draw(img, "RGBA")
        rank_size = 14 if compact else 17
        share_text(draw, (badge[0] + badge[2]) / 2, badge[3] - badge_size / 2 + rank_size * 0.36, f"{rank}", share_font_v2(rank_size, "num-bold"), badge_text, align="m")

        if compact:
            # One line: name · meta · value.
            baseline = y1 + (y2 - y1) / 2 + 6
            value_font = share_font_v2(18 if self.show_money else 16, "num")
            value_text = self.format_item_value(value * progress)
            value_w = max(share_text_width(self.format_item_value(value), value_font), 70)
            share_text(draw, x2 - 16, baseline, value_text, value_font, (*SHARE_GOLD, 255), align="r")
            meta_font = share_font_v2(11.5, "semibold")
            units = item.get("quantity", 0)
            meta = f"{str(item.get('site') or 'Unknown').upper()}  ·  {item.get('orders', 0):,} ord  ·  {units:,} unit{'' if units == 1 else 's'}"
            meta_right = x2 - 16 - value_w - 18
            share_text(draw, meta_right, baseline - 1, meta, meta_font, (*SHARE_MUTED, 255), align="r")
            name_font = share_font_v2(15.5, "semibold")
            name_x = badge[2] + 14
            name_max = meta_right - share_text_width(meta, meta_font) - 16 - name_x
            share_text(draw, name_x, baseline, share_ellipsize(item.get("product") or "Unknown item", name_font, name_max), name_font, (*SHARE_TEXT, 255))
            return

        name_font = share_font_v2(17, "semibold")
        share_text(draw, x1 + 68, y1 + 26, share_ellipsize(item.get("product") or "Unknown item", name_font, x2 - x1 - 68 - 160), name_font, (*SHARE_TEXT, 255))
        meta = f"{str(item.get('site') or 'Unknown').upper()}  ·  {item.get('orders', 0):,} orders  ·  {item.get('quantity', 0):,} units"
        share_text(draw, x1 + 68, y1 + 46, meta, share_font_v2(12.5, "semibold"), (*SHARE_MUTED, 255))
        value_font = share_font_v2(21 if self.show_money else 18, "num")
        share_text(draw, x2 - 18, y1 + 29, self.format_item_value(value * progress), value_font, (*SHARE_GOLD, 255), align="r")
        share_text(draw, x2 - 18, y1 + 47, f"{value / self.total_value * 100 * progress:.1f}%", share_font_v2(11.5, "semibold"), (*SHARE_DIM, 255), align="r", tracking=0.8)


def share_timeline(scene: ShareStatsScene, motion: str) -> list[tuple[dict[str, Any], int]]:
    pulse_period = 18
    timeline: list[tuple[dict[str, Any], int]] = []
    tick = 0

    def add(state: dict[str, Any], duration: int) -> None:
        nonlocal tick
        state["pulse"] = (tick % pulse_period) / pulse_period
        phase = (tick - 6) % SHARE_STRIKE_PERIOD
        if scene.strikes and tick >= 6 and phase < len(SHARE_STRIKE_FLICKER):
            state["strike"] = ((tick - 6) // SHARE_STRIKE_PERIOD, SHARE_STRIKE_FLICKER[phase])
        timeline.append((state, duration))
        tick += 1

    if motion in ("intro", "once"):
        for frame in range(scene.intro_end + 1):
            add({"intro": frame, "shimmer": None}, 40)
    for step in range(20):
        add({"intro": None, "shimmer": step / 19}, 40)
    hold = pulse_period * 2 - (tick % pulse_period)
    for _ in range(hold if motion != "ambient" else pulse_period * 3 - 20):
        add({"intro": None, "shimmer": None}, 50)
    timeline[-1][0].pop("strike", None)  # never hold or loop back on a flash
    if motion != "ambient":
        state, duration = timeline[-1]
        timeline[-1] = (state, duration + 1800)
    return timeline


def share_kmeans_palette(sample: Any, colors: int = 255, iterations: int = 12) -> Any:
    """255-color GIF palette from weighted k-means over the sample's distinct colors.

    Pillow builds without libimagequant only offer median cut / octree; measured on the share card, k-means cuts the
    color error by about a third overall and more on product photos. Falls back to octree without numpy.
    """
    from PIL import Image

    seed = sample.quantize(colors=colors, method=Image.Quantize.FASTOCTREE)
    try:
        import numpy as np
    except ImportError:
        return seed
    unique, counts = np.unique(np.asarray(sample.convert("RGB")).reshape(-1, 3), axis=0, return_counts=True)
    data = unique.astype(np.float32)
    weights = counts.astype(np.float32)
    centers = np.array(seed.getpalette()[: colors * 3], dtype=np.float32).reshape(-1, 3)
    center_norms = None
    for _ in range(iterations):
        center_norms = (centers ** 2).sum(1)
        labels = np.empty(len(data), dtype=np.int32)
        for start in range(0, len(data), 65536):
            chunk = data[start:start + 65536]
            labels[start:start + 65536] = (center_norms[None, :] - 2 * chunk @ centers.T).argmin(1)
        sums = np.zeros_like(centers)
        np.add.at(sums, labels, data * weights[:, None])
        totals = np.bincount(labels, weights=weights, minlength=len(centers)).astype(np.float32)
        used = totals > 0
        centers[used] = sums[used] / totals[used, None]
    palette = Image.new("P", (1, 1))
    flat = np.clip(np.rint(centers), 0, 255).astype(np.uint8).reshape(-1).tolist()
    palette.putpalette(flat + [0, 0, 0] * (256 - len(centers)))
    return palette


def share_downscale(img: Any, out_scale: int) -> Any:
    """Hi-res canvas (SHARE_SCALE) to output size: an exact box reduce at 1x, Lanczos for 2x."""
    from PIL import Image

    if out_scale == 1:
        return img.reduce(SHARE_SCALE)
    size = (img.width * out_scale // SHARE_SCALE, img.height * out_scale // SHARE_SCALE)
    return img.resize(size, Image.Resampling.LANCZOS)


def render_share_scene_frames(
    scene: ShareStatsScene,
    base: Any,
    timeline: list[tuple[dict[str, Any], int]],
    out_scale: int = 1,
) -> Any:
    """Yields the output-size RGB frame for each timeline entry, redrawing only regions that changed."""
    s = SHARE_SCALE
    regions = scene.regions()
    shimmer_regions = getattr(scene, "shimmer_regions", ("hero",))
    work = base.copy()
    frame_lo = share_downscale(base, out_scale).convert("RGB")
    first = True
    prev_strike = None
    for state, _ in timeline:
        intro = state.get("intro")
        strike = state.get("strike")
        for name, box, draw_fn, keys in regions:
            active = first or name == "pulse" or bool(state.get("_settle"))
            if name.startswith("storm") and (strike or prev_strike):
                active = True
            if name in shimmer_regions and (state.get("shimmer") is not None or state.get("_after_shimmer")):
                active = True
            if intro is not None and any(intro <= sum(scene.timing[key]) + 1 for key in keys if key in scene.timing):
                active = True
            if not active:
                continue
            hi = share_box(box)
            work.paste(base.crop(hi), hi[:2])
            draw_fn(work, state)
            if out_scale == 1:
                frame_lo.paste(share_downscale(work.crop(hi), 1).convert("RGB"), tuple(box[:2]))
            else:
                # Lanczos reads past a crop's edge; resize with a margin and keep the middle so regions leave no seams.
                # The margin comes from the static base: some draws spill a pixel past their box, and `work` never
                # resets those pixels, so they would build up frame after frame.
                pad = min(4, box[0], box[1], SHARE_W - box[2], scene.height - box[3])
                padded = share_box((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
                source = base.crop(padded)
                source.paste(work.crop(hi), (pad * s, pad * s))
                region = share_downscale(source, out_scale).convert("RGB")
                inner = (pad * out_scale, pad * out_scale, region.width - pad * out_scale, region.height - pad * out_scale)
                frame_lo.paste(region.crop(inner), (box[0] * out_scale, box[1] * out_scale))
        first = False
        prev_strike = strike
        yield frame_lo


def render_share_scene_still(scene: ShareStatsScene, base: Any, state: dict[str, Any]) -> Any:
    """Full hi-res render of one state (used for the palette and PNG export)."""
    img = base.copy()
    for _, _, draw_fn, _ in scene.regions():
        draw_fn(img, state)
    return img


def render_gif(
    stats: dict[str, Any],
    cards: set[str] | None = None,
    theme: str = "titanium",
    title: str | None = None,
    motion: str = "intro",
    scene_class: type[ShareStatsScene] | None = None,
    resolution: int = 1,
) -> BytesIO:
    from PIL import Image, ImageChops

    motion = motion if motion in SHARE_MOTIONS else "intro"
    out = 2 if resolution == 2 else 1
    scene = (scene_class or BetaShareStatsScene)(stats, cards or CARDS, theme, title)
    base = scene.build_base()
    timeline = share_timeline(scene, motion)
    for state, _ in timeline:
        if state.get("intro") is None:
            state["_settle"] = True
            break
    for index in range(1, len(timeline)):
        if timeline[index][0].get("shimmer") is None and timeline[index - 1][0].get("shimmer") is not None:
            timeline[index][0]["_after_shimmer"] = True

    # One shared palette, built from the finished card plus a shimmer pass, keeps colors stable across frames.
    # Text-heavy panels are repeated in the sample so the palette spends more entries on bright type than on grain.
    # The palette is sampled at 1x even for 2x output: same colors, a quarter of the pixels to cluster.
    final_rgb = render_share_scene_still(scene, base, {"intro": None, "shimmer": 0.45, "pulse": 0.3, "strike": (0, 1.0)}).reduce(SHARE_SCALE).convert("RGB")
    crops = [final_rgb.crop(box) for box in [(40, 40, 640, 170), *(scene.boxes[key] for key in ("hero", "tiles", "top", "board") if key in scene.boxes)]]
    sample = Image.new("RGB", (SHARE_W, scene.height * 2 + sum(crop.height for crop in crops) * 2))
    sample.paste(final_rgb, (0, 0))
    sample.paste(base.reduce(SHARE_SCALE).convert("RGB"), (0, scene.height))
    sample_y = scene.height * 2
    for crop in crops * 2:
        sample.paste(crop, (0, sample_y))
        sample_y += crop.height
    palette_img = share_kmeans_palette(sample)

    # Frame 0 is a full image; later frames only carry changed pixels (index 255 = transparent, keep previous).
    transparent = 255
    frames: list[Any] = []
    prev_rgb = None
    for frame_rgb in render_share_scene_frames(scene, base, timeline, out_scale=out):
        if prev_rgb is None:
            frames.append(frame_rgb.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG))
        else:
            diff = ImageChops.difference(frame_rgb, prev_rgb)
            bbox = diff.getbbox()
            delta = Image.new("P", frame_rgb.size, transparent)
            delta.putpalette(palette_img.getpalette())
            if bbox:
                r, g, b = diff.crop(bbox).split()
                changed = ImageChops.lighter(ImageChops.lighter(r, g), b).point(lambda v: 255 if v else 0)
                region_q = frame_rgb.crop(bbox).quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
                delta.paste(region_q, bbox[:2], changed)
            frames.append(delta)
        prev_rgb = frame_rgb.copy()

    output = BytesIO()
    save_kwargs: dict[str, Any] = {
        "format": "GIF",
        "save_all": True,
        "append_images": frames[1:],
        "duration": [duration for _, duration in timeline],
        "optimize": False,
        "disposal": 1,
        "transparency": transparent,
    }
    if motion != "once":
        save_kwargs["loop"] = 0
    frames[0].save(output, **save_kwargs)
    output.seek(0)
    return output


def render_png(
    stats: dict[str, Any],
    cards: set[str] | None = None,
    theme: str = "titanium",
    title: str | None = None,
    scene_class: type[ShareStatsScene] | None = None,
) -> BytesIO:
    from PIL import Image

    scene = (scene_class or BetaShareStatsScene)(stats, cards or CARDS, theme, title)
    still = render_share_scene_still(scene, scene.build_base(), {"intro": None, "shimmer": None, "pulse": 0.0}).convert("RGB").resize((SHARE_W * 2, scene.height * 2), Image.Resampling.LANCZOS)
    output = BytesIO()
    still.save(output, format="PNG", optimize=True)
    output.seek(0)
    return output

# ---- Product photos ---------------------------------------------------------------------------------------------
# Checkout webhooks carry the retailer's product photo as the embed thumbnail (target.scene7.com for Target).
# db.recap_card_stats puts that URL on each top item as "image"; the card shows it beside the Top Hit and every
# leaderboard row.

PRODUCT_IMAGE_DIR = BASE_DIR / "data" / "product_images"
PRODUCT_IMAGE_MAX_BYTES = 6 * 1024 * 1024
PRODUCT_IMAGE_PX = 320


def product_image_fetch_url(url: str) -> str:
    # Scene7 serves the full-size original by default; ask it for a card-sized PNG instead.
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.endswith("scene7.com") and not parsed.query:
        return f"{url}?wid={PRODUCT_IMAGE_PX}&hei={PRODUCT_IMAGE_PX}&fmt=png-alpha&qlt=90"
    return url


def public_host(url: str) -> bool:
    """Only https URLs on the public internet: a webhook can't point the site at the VPS's own network."""
    import ipaddress
    import socket

    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def load_product_image(url: str) -> Any:
    """Product photo as RGBA, cached on disk by URL; None when it cannot be fetched or decoded."""
    from PIL import Image

    PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    cached = PRODUCT_IMAGE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.png"
    try:
        if cached.exists():
            with Image.open(cached) as image:
                return image.convert("RGBA")
        fetch = product_image_fetch_url(url)
        if not public_host(fetch):
            return None
        request = urllib.request.Request(fetch, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/*"})
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=10) as response:
            if response.status != 200 or not response.headers.get("Content-Type", "").startswith("image/"):
                return None
            data = response.read(PRODUCT_IMAGE_MAX_BYTES + 1)
        if len(data) > PRODUCT_IMAGE_MAX_BYTES:
            return None
        with Image.open(BytesIO(data)) as image:
            image = image.convert("RGBA")
        image.thumbnail((PRODUCT_IMAGE_PX, PRODUCT_IMAGE_PX))
        image.save(cached, format="PNG")
        return image
    except Exception as exc:
        print(f"Could not load product image {url}: {exc}")
        return None


def share_item_images(items: list[dict[str, Any]]) -> list[Any]:
    """One image (or None) per leaderboard item, fetched in parallel."""
    item_urls = [item.get("image") or None for item in items]
    unique = sorted({url for url in item_urls if url})
    with ThreadPoolExecutor(max_workers=8) as executor:
        loaded = dict(zip(unique, executor.map(load_product_image, unique)))
    return [loaded.get(url) if url else None for url in item_urls]


def share_glass_finish(tile: Any) -> Any:
    """Glossy-card finish for a product tile: soft top highlight, faint bottom falloff and an inner rim."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    px = tile.width
    alpha = tile.getchannel("A")

    def clipped(color: tuple[int, int, int], mask: Any) -> Any:
        layer = Image.new("RGBA", (px, px), (*color, 0))
        layer.putalpha(ImageChops.multiply(mask, alpha))
        return layer

    falloff = Image.linear_gradient("L").resize((px, px)).point(lambda v: int(max(0, v - 150) * 0.35))
    finished = Image.alpha_composite(tile, clipped((10, 14, 22), falloff))
    highlight = Image.new("L", (px, px), 0)
    ImageDraw.Draw(highlight).ellipse((-px * 0.35, -px * 0.78, px * 1.35, px * 0.42), fill=46)
    finished = Image.alpha_composite(finished, clipped((255, 255, 255), highlight.filter(ImageFilter.GaussianBlur(px * 0.03))))
    rim = max(1, px // 90)
    ImageDraw.Draw(finished).rounded_rectangle(
        (rim, rim, px - 1 - rim, px - 1 - rim), radius=px * 0.2, outline=(255, 255, 255, 90), width=rim
    )
    return finished


def share_glare_streak(px: int) -> Any:
    """RGBA glare streak four tiles wide; cropping a moving window from it sweeps the glare across a photo.

    A white highlight alone vanishes on white product photos, so the bright core rides inside a faint cool shadow
    band, the way a reflection on glossy packaging darkens either side of the highlight.
    """
    from PIL import Image, ImageFilter

    width = px * 4
    ramp = Image.linear_gradient("L").rotate(90)

    def band(band_width: float, peak: int, power: float) -> Any:
        profile = ramp.resize((max(1, round(px * band_width)), width))
        return profile.point(lambda v: int(peak * (1 - abs(v - 127.5) / 127.5) ** power))

    def layer(color: tuple[int, int, int], mask: Any, center: float) -> Any:
        full = Image.new("L", (width, width), 0)
        full.paste(mask, (round(width / 2 + px * center - mask.width / 2), 0))
        out = Image.new("RGBA", (width, width), (*color, 0))
        out.putalpha(full)
        return out

    streak = layer((22, 30, 48), band(0.62, 64, 1.1), 0.0)
    streak = Image.alpha_composite(streak, layer((255, 255, 255), band(0.17, 225, 1.5), 0.0))
    streak = Image.alpha_composite(streak, layer((255, 255, 255), band(0.05, 170, 1.2), 0.15))
    return streak.rotate(-28, resample=Image.Resampling.BICUBIC).filter(ImageFilter.GaussianBlur(px * 0.008))


class BetaShareStatsScene(ShareStatsScene):
    """ShareStatsScene with product photos beside the Top Hit and each leaderboard row."""

    def __init__(self, stats: dict[str, Any], cards: set[str], theme: str, title: str | None = None) -> None:
        # "glare" is a Beta-only theme: Titanium colors, glass-finish photos and a light sweep over the Top Hit.
        self.glare = theme == "glare"
        super().__init__(stats, cards, "titanium" if self.glare else theme, title)
        if self.glare:
            self.theme_name = "Glare"
            self.shimmer_regions = ("hero", "top")
        self.images = share_item_images(self.items) if "products" in cards else []
        self._thumbs: dict[tuple[int, int], Any] = {}
        self._glare_streak = None
        self.top_photo: tuple[float, float, int] | None = None

    def thumb(self, index: int, size: int) -> Any:
        """Rounded product tile at hi-res: photo on white, or an accent tile with the item's initial."""
        from PIL import Image, ImageChops, ImageDraw, ImageOps

        key = (index, size)
        if key in self._thumbs:
            return self._thumbs[key]
        s = SHARE_SCALE
        px = size * s
        radius = max(8, size * 0.22) * s
        photo = self.images[index] if index < len(self.images) else None
        if photo is not None:
            tile = Image.new("RGBA", (px, px), (255, 255, 255, 255))
            inner = ImageOps.contain(photo, (round(px * 0.86), round(px * 0.86)), Image.Resampling.LANCZOS)
            tile.alpha_composite(inner, ((px - inner.width) // 2, (px - inner.height) // 2))
        else:
            tile = share_gradient((px, px), share_mix(self.accent, (20, 24, 32), 0.35), share_mix(self.accent2, (10, 12, 18), 0.55))
            tile = tile.convert("RGBA")
            name = str(self.items[index].get("product") or "?") if index < len(self.items) else "?"
            initial = next((char for char in name if char.isalnum()), "?").upper()
            ImageDraw.Draw(tile).text(
                (px / 2, px / 2), initial, font=share_font_v2(size * 0.46, "bold"), fill=(255, 255, 255, 230), anchor="mm"
            )
        mask = share_round_mask((px, px), radius)
        tile.putalpha(ImageChops.multiply(tile.getchannel("A"), mask))
        ImageDraw.Draw(tile).rounded_rectangle((0, 0, px - 1, px - 1), radius=radius, outline=(255, 255, 255, 60), width=s)
        if self.glare:
            tile = share_glass_finish(tile)
        self._thumbs[key] = tile
        return tile

    def paste_thumb(self, img: Any, index: int, x: float, y: float, size: int) -> None:
        share_composite(img, self.thumb(index, size), round(x * SHARE_SCALE), round(y * SHARE_SCALE))

    def draw_top_static(self, img: Any) -> None:
        from PIL import ImageDraw

        x1, y1, x2, y2 = self.boxes["top"]
        share_glass_panel(img, self.boxes["top"], radius=26, tint=SHARE_GOLD, tint_alpha=36, tint_at="tr", border=share_mix((255, 255, 255), SHARE_GOLD, 0.5), border_alpha=(80, 14))
        item = self.items[0] if self.items else None
        tile, tx, ty = 88, 88, y1 + 16
        if item:
            self.paste_thumb(img, 0, tx, ty, tile)
            self.top_photo = (tx, ty, tile)
        # "#1" medal pinned to the photo's corner.
        badge = (tx - 10, ty - 8, tx + 26, ty + 28)
        share_paint(img, share_round_mask((36 * SHARE_SCALE, 36 * SHARE_SCALE), 12 * SHARE_SCALE), share_gradient((36 * SHARE_SCALE, 36 * SHARE_SCALE), (255, 228, 140), SHARE_GOLD_DEEP), badge[0] * SHARE_SCALE, badge[1] * SHARE_SCALE)
        draw = ImageDraw.Draw(img, "RGBA")
        share_text(draw, badge[0] + 18, badge[1] + 24, "#1", share_font_v2(15, "num-bold"), (40, 26, 4, 255), align="m")
        text_x = tx + tile + 22
        share_text(draw, text_x, y1 + 40, "TOP HIT", share_font_v2(12.5, "semibold"), (*share_mix(SHARE_GOLD, (255, 255, 255), 0.2), 255), tracking=2.4)
        name_font = share_font_v2(24, "semibold")
        name = item.get("product") if item else "No checkouts in this range"
        share_text(draw, text_x, y1 + 73, share_ellipsize(name, name_font, 992 - 270 - text_x), name_font, (*SHARE_TEXT, 255))
        if item:
            meta = f"{str(item.get('site') or 'Unknown').upper()}  ·  {item.get('orders', 0):,} orders  ·  {item.get('quantity', 0):,} units"
            share_text(draw, text_x, y1 + 99, meta, share_font_v2(13.5, "semibold"), (*SHARE_MUTED, 255))
        draw.rounded_rectangle(share_box((88, y1 + 114, 992, y1 + 120)), radius=3 * SHARE_SCALE, fill=(255, 255, 255, 16))

    def draw_top_dynamic(self, img: Any, state: dict[str, Any]) -> None:
        super().draw_top_dynamic(img, state)
        shimmer = state.get("shimmer")
        if not self.glare or shimmer is None or not self.top_photo:
            return
        from PIL import Image, ImageChops

        tx, ty, size = self.top_photo
        px = size * SHARE_SCALE
        if self._glare_streak is None:
            streak = share_glare_streak(px)
            top = (streak.height - px) // 2
            band = streak.getchannel("A").crop((0, top, streak.width, top + px)).getbbox() or (0, 0, streak.width, px)
            # Window offsets where the streak is just off the right edge, then just off the left edge.
            self._glare_streak = (streak, top, min(band[2], streak.width - px), max(band[0] - px, 0))
        streak, top, start, end = self._glare_streak
        # Smoothstep keeps the light moving for the whole shimmer pass instead of darting across and parking.
        t = min(max(shimmer, 0.0), 1.0)
        offset = round(start + (end - start) * t * t * (3 - 2 * t))
        layer = streak.crop((offset, top, offset + px, top + px))
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), self.thumb(0, size).getchannel("A")))
        share_composite(img, layer, round(tx * SHARE_SCALE), round(ty * SHARE_SCALE))

    def draw_board_row(self, img: Any, state: dict[str, Any], index: int) -> None:
        from PIL import ImageDraw

        s = SHARE_SCALE
        item = self.board_rows[index]
        rank = index + 2
        x1, y1, x2, y2 = self.row_boxes[index]
        progress = self.progress(f"row{index}", state.get("intro"))
        draw = ImageDraw.Draw(img, "RGBA")
        draw.rounded_rectangle(share_box((x1, y1, x2, y2)), radius=16 * s, fill=(255, 255, 255, 9 if index % 2 == 0 else 5), outline=(255, 255, 255, 18), width=s)

        value = self.item_value(item)
        top_value = self.item_value(self.items[0]) if self.items else 1.0
        fill_w = round((x2 - x1) * min(value / max(top_value, 1e-9), 1.0) * progress * s)
        if fill_w > 4 * s:
            size = ((x2 - x1) * s, (y2 - y1) * s)
            mask = share_round_mask(size, 16 * s)
            mask.paste(0, (fill_w, 0, size[0], size[1]))
            share_paint(img, mask, share_gradient(size, share_rgba(self.accent, 54), share_rgba(self.accent2, 14), horizontal=True), x1 * s, y1 * s)

        compact = y2 - y1 < 58
        medal = {2: ((236, 240, 248), (150, 162, 182)), 3: ((246, 196, 150), (176, 108, 62))}.get(rank)
        badge_size = 26 if compact else 30
        badge = (x1 + (10 if compact else 14), y1 + (y2 - y1 - badge_size) / 2)
        badge = (*badge, badge[0] + badge_size, badge[1] + badge_size)
        bsize = (badge_size * s, badge_size * s)
        if medal:
            share_paint(img, share_round_mask(bsize, 9 * s), share_gradient(bsize, medal[0], medal[1]), round(badge[0] * s), round(badge[1] * s))
            badge_text = (24, 26, 34, 255)
        else:
            draw.rounded_rectangle(share_box(badge), radius=9 * s, fill=share_rgba(self.accent, 30), outline=share_rgba(self.accent, 120), width=s)
            badge_text = (*share_mix(self.accent, (255, 255, 255), 0.4), 255)
        draw = ImageDraw.Draw(img, "RGBA")
        rank_size = 13 if compact else 15
        share_text(draw, (badge[0] + badge[2]) / 2, badge[3] - badge_size / 2 + rank_size * 0.36, f"{rank}", share_font_v2(rank_size, "num-bold"), badge_text, align="m")

        thumb_size = (y2 - y1) - (8 if compact else 10)
        thumb_x = badge[2] + 10
        self.paste_thumb(img, index + 1, thumb_x, y1 + ((y2 - y1) - thumb_size) / 2, thumb_size)
        draw = ImageDraw.Draw(img, "RGBA")
        name_x = thumb_x + thumb_size + 12

        if compact:
            baseline = y1 + (y2 - y1) / 2 + 6
            value_font = share_font_v2(18 if self.show_money else 16, "num")
            value_text = self.format_item_value(value * progress)
            value_w = max(share_text_width(self.format_item_value(value), value_font), 70)
            share_text(draw, x2 - 16, baseline, value_text, value_font, (*SHARE_GOLD, 255), align="r")
            meta_font = share_font_v2(11.5, "semibold")
            units = item.get("quantity", 0)
            meta = f"{str(item.get('site') or 'Unknown').upper()}  ·  {item.get('orders', 0):,} ord  ·  {units:,} unit{'' if units == 1 else 's'}"
            meta_right = x2 - 16 - value_w - 18
            share_text(draw, meta_right, baseline - 1, meta, meta_font, (*SHARE_MUTED, 255), align="r")
            name_font = share_font_v2(15.5, "semibold")
            name_max = meta_right - share_text_width(meta, meta_font) - 16 - name_x
            share_text(draw, name_x, baseline, share_ellipsize(item.get("product") or "Unknown item", name_font, name_max), name_font, (*SHARE_TEXT, 255))
            return

        name_font = share_font_v2(17, "semibold")
        share_text(draw, name_x, y1 + 26, share_ellipsize(item.get("product") or "Unknown item", name_font, x2 - name_x - 160), name_font, (*SHARE_TEXT, 255))
        meta = f"{str(item.get('site') or 'Unknown').upper()}  ·  {item.get('orders', 0):,} orders  ·  {item.get('quantity', 0):,} units"
        share_text(draw, name_x, y1 + 46, meta, share_font_v2(12.5, "semibold"), (*SHARE_MUTED, 255))
        value_font = share_font_v2(21 if self.show_money else 18, "num")
        share_text(draw, x2 - 18, y1 + 29, self.format_item_value(value * progress), value_font, (*SHARE_GOLD, 255), align="r")
        share_text(draw, x2 - 18, y1 + 47, f"{value / self.total_value * 100 * progress:.1f}%", share_font_v2(11.5, "semibold"), (*SHARE_DIM, 255), align="r", tracking=0.8)



# Every panel the card can show; the drop recap shows them all.
CARDS = {"spend", "units", "placed", "cancelled", "success", "avg", "activity", "top_issue", "products"}
# Theme key -> name, in the order the admin panel lists them. Glare is Beta's own: Titanium with glossy photos.
THEMES = {"glare": "Glare", **{key: palette["name"] for key, palette in SHARE_THEMES_V2.items()}}
