"""Small color helpers (hex <-> sRGB <-> CIELAB) used for band merging."""
from __future__ import annotations

import math


def hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{int(round(max(0, min(1, c)) * 255)):02x}" for c in rgb)


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb) -> tuple[float, float, float]:
    r, g, b = (_lin(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 1.0
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def color_distance(h1: str, h2: str) -> float:
    """CIE76 delta-E between two hex colors (0 = identical, ~100 = opposite)."""
    a = rgb_to_lab(hex_to_rgb(h1))
    b = rgb_to_lab(hex_to_rgb(h2))
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(a, b)))


def normalize_hex(h: str) -> str:
    return rgb_to_hex(hex_to_rgb(h))
