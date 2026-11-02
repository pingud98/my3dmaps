"""Black-only SVG overlay for printing 1:1 on transparent film.

The film sits on the frame glass over the relief, so everything here is drawn
in the relief's own model coordinates (same projection, scale and centre) and
lines up with the print.  Content:

* **contours** from the SRTM surface (public domain), smoothed first because
  SRTM includes roofs and tree canopy; index contours are thicker and labelled,
* **place names** and **symbols** (stations, railways, motorways, ferries,
  lighthouses, stadiums, airports, peaks, river names) from OpenStreetMap
  (ODbL: credited on the border),
* a **grid** (default OS National Grid, 1 km) with grid numbers, 100 km square
  letters and seam ticks (where the glued tiles meet) in the border,
* title, scale bar and the data credits along the bottom edge.

Only black ink is used.  Where a line must stay clear (contour under its label,
the middle of a motorway casing) it is cut out with an SVG mask rather than
painted white, so the film stays transparent there.

The page is the frame's visible window (``overlay.window_mm``); print at 100 %
("actual size", no fit-to-page).
"""
from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import linemerge

from .config import ProjectConfig
from .geo import Grid
from .rivers import fetch_overpass

log = logging.getLogger(__name__)

INK = "#000"


# ---------------------------------------------------------------------------
# geometry helpers

@dataclass
class Frame:
    """Maps project-CRS metres to SVG millimetres (origin top-left of the window)."""

    e0: float  # project easting of model x = 0
    n0: float  # project northing of model y = 0
    mm_per_m: float
    win_x0: float  # model mm of the window's left edge
    win_y0: float  # model mm of the window's bottom edge
    win_w: float
    win_h: float
    margins: tuple[float, float, float, float]  # top, right, bottom, left (mm)

    def svg(self, e, n):
        x = (np.asarray(e) - self.e0) * self.mm_per_m - self.win_x0
        y = self.win_h - ((np.asarray(n) - self.n0) * self.mm_per_m - self.win_y0)
        return x, y

    def proj(self, x, y):
        """SVG mm -> project metres."""
        e = (np.asarray(x) + self.win_x0) / self.mm_per_m + self.e0
        n = (self.win_h - np.asarray(y) + self.win_y0) / self.mm_per_m + self.n0
        return e, n

    @property
    def inner(self) -> tuple[float, float, float, float]:
        """Neatline (x0, y0, x1, y1) in SVG mm."""
        t, r, b, l = self.margins
        return (l, t, self.win_w - r, self.win_h - b)


def make_frame(cfg: ProjectConfig, grid: Grid) -> Frame:
    ov = cfg.overlay
    W, H = cfg.model_mm
    ww, wh = float(ov.window_mm[0]), float(ov.window_mm[1])
    m = ov.margin_mm
    return Frame(float(grid.easting[0]), float(grid.northing[0]), cfg.mm_per_m,
                 (W - ww) / 2.0, (H - wh) / 2.0, ww, wh, (m, m, ov.bottom_margin_mm, m))


def _f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 1e5 else f"{v:.1f}"


def _path(coords) -> str:
    pts = list(coords)
    if not pts:
        return ""
    return "M" + " L".join(f"{_f(x)} {_f(y)}" for x, y in pts)


def _lines(geom):
    if geom.is_empty:
        return []
    if isinstance(geom, LineString):
        return [geom]
    if isinstance(geom, MultiLineString):
        return list(geom.geoms)
    if hasattr(geom, "geoms"):
        out = []
        for g in geom.geoms:
            out += _lines(g)
        return out
    return []


# ---------------------------------------------------------------------------
# contours (marching squares, no extra dependency)

# per case: list of (edge_a, edge_b); edges 0=bottom 1=right 2=top 3=left
_MS = {
    1: [(3, 0)], 2: [(0, 1)], 3: [(3, 1)], 4: [(1, 2)], 6: [(0, 2)], 7: [(3, 2)],
    8: [(2, 3)], 9: [(2, 0)], 11: [(2, 1)], 12: [(1, 3)], 13: [(1, 0)], 14: [(0, 3)],
}


def contour_lines(z: np.ndarray, xs: np.ndarray, ys: np.ndarray, level: float) -> list[LineString]:
    """Iso-lines of ``z`` (rows = ys, cols = xs) at ``level`` as merged LineStrings."""
    z = np.where(z == level, level + 1e-6, z)
    above = z > level
    bl, br, tr, tl = above[:-1, :-1], above[:-1, 1:], above[1:, 1:], above[1:, :-1]
    case = bl.astype(np.uint8) | (br.astype(np.uint8) << 1) | (tr.astype(np.uint8) << 2) | (tl.astype(np.uint8) << 3)
    # crossing points on horizontal edges (j, i)-(j, i+1) and vertical edges (j, i)-(j+1, i)
    with np.errstate(divide="ignore", invalid="ignore"):
        th = (level - z[:, :-1]) / (z[:, 1:] - z[:, :-1])
        tv = (level - z[:-1, :]) / (z[1:, :] - z[:-1, :])
    hx = xs[:-1][None, :] + th * (xs[1:] - xs[:-1])[None, :]
    hy = np.broadcast_to(ys[:, None], hx.shape)
    vy = ys[:-1][:, None] + tv * (ys[1:] - ys[:-1])[:, None]
    vx = np.broadcast_to(xs[None, :], vy.shape)

    def edge_pt(edge, j, i):
        if edge == 0:
            return hx[j, i], hy[j, i]
        if edge == 2:
            return hx[j + 1, i], hy[j + 1, i]
        if edge == 3:
            return vx[j, i], vy[j, i]
        return vx[j, i + 1], vy[j, i + 1]

    segs = []
    centre = (z[:-1, :-1] + z[:-1, 1:] + z[1:, 1:] + z[1:, :-1]) / 4.0 > level
    for c, pairs in _MS.items():
        jj, ii = np.nonzero(case == c)
        for a, b in pairs:
            pa = edge_pt(a, jj, ii)
            pb = edge_pt(b, jj, ii)
            segs += list(zip(zip(pa[0], pa[1]), zip(pb[0], pb[1])))
    for c in (5, 10):  # saddles: resolve by the cell-centre value
        jj, ii = np.nonzero(case == c)
        for j, i in zip(jj, ii):
            hi = centre[j, i]
            if (c == 5) == hi:  # corners bl+tr above and centre above -> they connect: cut tl and br corners
                pairs = [(2, 3), (0, 1)] if c == 5 else [(3, 0), (1, 2)]
            else:
                pairs = [(3, 0), (1, 2)] if c == 5 else [(2, 3), (0, 1)]
            for a, b in pairs:
                segs.append((tuple(map(float, edge_pt(a, j, i))), tuple(map(float, edge_pt(b, j, i)))))
    if not segs:
        return []
    merged = linemerge(MultiLineString([LineString([tuple(map(float, p)), tuple(map(float, q))]) for p, q in segs]))
    return _lines(merged)


# ---------------------------------------------------------------------------
# OpenStreetMap features

def overlay_query(bbox_lonlat) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox_lonlat
    return (
        f"[out:json][timeout:180][bbox:{min_lat:.5f},{min_lon:.5f},{max_lat:.5f},{max_lon:.5f}];\n(\n"
        '  node["place"~"^(city|town|suburb|village|island|islet)$"]["name"];\n'
        '  node["railway"="station"];\n'
        '  nwr["man_made"="lighthouse"];\n'
        '  node["natural"~"^(peak|hill)$"]["name"];\n'
        '  nwr["aeroway"="aerodrome"]["name"];\n'
        '  nwr["leisure"="stadium"]["name"];\n'
        ");\nout center tags;\n(\n"
        '  way["route"="ferry"];\n'
        '  way["railway"="rail"][!"service"];\n'
        '  way["highway"="motorway"];\n'
        '  way["waterway"~"^(river|canal)$"]["name"];\n'
        ");\nout geom tags;\n"
    )


@dataclass
class OsmFeatures:
    points: list[dict] = field(default_factory=list)  # {kind, name, x, y, tags}
    lines: list[dict] = field(default_factory=list)  # {kind, name, geom (svg LineString), tags}
    error: str = ""


def load_overlay_osm(bbox_lonlat, frame: Frame, crs, progress: Callable[[str], None] | None = None) -> OsmFeatures:
    try:
        data = fetch_overpass(overlay_query(bbox_lonlat), "map labels and symbols", progress)
    except RuntimeError as e:
        return OsmFeatures(error=str(e))
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    out = OsmFeatures()
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name", "")
        if "geometry" in el:
            pts = el["geometry"]
            if len(pts) < 2:
                continue
            e, n = tr.transform([p["lon"] for p in pts], [p["lat"] for p in pts])
            x, y = frame.svg(e, n)
            if tags.get("route") == "ferry":
                kind = "ferry"
            elif tags.get("railway") == "rail":
                kind = "rail_tunnel" if tags.get("tunnel") in ("yes", "building_passage") else "rail"
            elif tags.get("highway") == "motorway":
                if tags.get("tunnel") == "yes":
                    continue
                kind = "motorway"
            elif tags.get("waterway") in ("river", "canal"):
                kind = tags["waterway"]
            else:
                continue
            out.lines.append({"kind": kind, "name": name, "geom": LineString(list(zip(x, y))), "tags": tags})
            continue
        if "lat" in el:
            lon, lat = el["lon"], el["lat"]
        elif "center" in el:
            lon, lat = el["center"]["lon"], el["center"]["lat"]
        else:
            continue
        e, n = tr.transform(lon, lat)
        x, y = frame.svg(e, n)
        if "place" in tags:
            kind = tags["place"]
        elif tags.get("railway") == "station":
            if tags.get("station") in ("subway", "light_rail", "monorail", "miniature") or tags.get("usage") == "tourism":
                continue
            kind = "station"
        elif tags.get("man_made") == "lighthouse":
            kind = "lighthouse"
        elif tags.get("natural") in ("peak", "hill"):
            kind = "peak"
        elif tags.get("aeroway") == "aerodrome":
            if tags.get("aerodrome:type") in ("private",) or tags.get("aerodrome") == "private":
                continue
            kind = "airport"
        elif tags.get("leisure") == "stadium":
            kind = "stadium"
        else:
            continue
        out.points.append({"kind": kind, "name": name, "x": float(x), "y": float(y), "tags": tags})
    return out


# ---------------------------------------------------------------------------
# label placement

class Placer:
    """Greedy collision avoidance with axis-aligned boxes (rotated labels use their bounding box)."""

    def __init__(self, bounds):
        self.bounds = bounds
        self.boxes: list[tuple[float, float, float, float]] = []

    def free(self, b, pad=0.6) -> bool:
        x0, y0, x1, y1 = self.bounds
        if b[0] < x0 + 0.8 or b[1] < y0 + 0.8 or b[2] > x1 - 0.8 or b[3] > y1 - 0.8:
            return False
        return all(b[2] + pad <= o[0] or b[0] - pad >= o[2] or b[3] + pad <= o[1] or b[1] - pad >= o[3]
                   for o in self.boxes)

    def take(self, b):
        self.boxes.append(b)


def text_width(text: str, size: float, bold: bool = False, spacing: float = 0.0, caps: bool = False) -> float:
    """Rough Helvetica advance width in mm (good enough for collision boxes)."""
    w = 0.0
    for ch in text:
        if ch == " ":
            w += 0.28
        elif ch in "il.,'!|:;":
            w += 0.25
        elif ch in "mwMW":
            w += 0.85
        elif ch.isupper() or caps:
            w += 0.68
        elif ch.isdigit():
            w += 0.56
        else:
            w += 0.52
    return (w * (1.07 if bold else 1.0)) * size + spacing * max(len(text) - 1, 0)


# ---------------------------------------------------------------------------
# OS National Grid letters

def os_square_letters(e: float, n: float) -> str:
    """Two-letter OS 100 km square for National Grid metres (e.g. 'SJ')."""
    e100, n100 = int(e // 100000), int(n // 100000)
    if not (0 <= e100 < 7 and 0 <= n100 < 13):
        return ""
    l1 = (19 - n100) - (19 - n100) % 5 + (e100 + 10) // 5
    l2 = (19 - n100) * 5 % 25 + e100 % 5
    letters = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
    return letters[l1] + letters[l2]


# ---------------------------------------------------------------------------

@dataclass
class OverlayResult:
    svg_path: Path
    stats: dict


def build_overlay(cfg: ProjectConfig, grid: Grid, elev: np.ndarray, out_path: str | Path,
                  exaggeration: Optional[float] = None, progress: Callable[[str], None] | None = None) -> OverlayResult:
    """Write the overlay SVG.  ``elev`` is the (water-flattened) node elevation grid of ``grid``."""
    ov = cfg.overlay
    fr = make_frame(cfg, grid)
    ix0, iy0, ix1, iy1 = fr.inner
    neat = box(ix0, iy0, ix1, iy1)
    lw = ov.line_mm
    parts_defs: list[str] = []
    body: list[str] = []
    label_masks: list[str] = []  # rects cut out of the contour/grid layers
    stats: dict = {}
    placer = Placer(fr.inner)

    def text(x, y, s, size, anchor="middle", weight="normal", style="normal", spacing=0.0, rotate=0.0, extra=""):
        tr = f' transform="rotate({_f(rotate)} {_f(x)} {_f(y)})"' if rotate else ""
        ls = f' letter-spacing="{_f(spacing)}"' if spacing else ""
        return (f'<text x="{_f(x)}" y="{_f(y)}" fill="{INK}" stroke="none" font-size="{_f(size)}" text-anchor="{anchor}" font-weight="{weight}" '
                f'font-style="{style}"{ls}{tr}{extra}>{html.escape(s)}</text>')

    def knock(b, pad=0.5):
        label_masks.append(f'<rect x="{_f(b[0] - pad)}" y="{_f(b[1] - pad)}" width="{_f(b[2] - b[0] + 2 * pad)}" '
                           f'height="{_f(b[3] - b[1] + 2 * pad)}" fill="#000"/>')

    # ---- OSM features first (labels claim space before contour labels) ----------
    if progress:
        progress("overlay: fetching OpenStreetMap names and symbols")
    e_c, n_c = fr.proj(np.array([0, fr.win_w, 0, fr.win_w]), np.array([0, 0, fr.win_h, fr.win_h]))
    to_ll = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(e_c, n_c)
    bbox_ll = (min(lon) - 0.01, min(lat) - 0.01, max(lon) + 0.01, max(lat) + 0.01)
    osm = load_overlay_osm(bbox_ll, fr, grid.crs, progress)
    stats["osm_error"] = osm.error
    stats["osm_points"] = len(osm.points)
    stats["osm_lines"] = len(osm.lines)

    sym: list[str] = []
    lines_svg: list[str] = []
    mwy_paths: list[str] = []

    def clipped(g):
        return [l for l in _lines(g.intersection(neat)) if l.length > 0.3]

    want = set(ov.symbols)
    for f in osm.lines:
        k = f["kind"]
        if k == "rail" and "railway" in want:
            for l in clipped(f["geom"]):
                d = _path(l.coords)  # OS-style: thin line with heavy dashes
                lines_svg.append(f'<path d="{d}" stroke-width="{_f(lw)}"/>'
                                 f'<path d="{d}" stroke-width="{_f(lw * 3.6)}" stroke-dasharray="2.4 2.4"/>')
        elif k == "rail_tunnel" and "railway" in want:
            for l in clipped(f["geom"]):
                lines_svg.append(f'<path d="{_path(l.coords)}" stroke-width="{_f(lw * 1.2)}" stroke-dasharray="1.2 0.8"/>')
        elif k == "ferry" and "ferry" in want:
            if not neat.contains(f["geom"]):
                continue  # only local crossings; sea routes leaving the map just trace the shipping channel
            for l in clipped(f["geom"]):
                lines_svg.append(f'<path d="{_path(l.coords)}" stroke-width="{_f(lw * 1.3)}" stroke-dasharray="2 1.2"/>')
        elif k == "motorway" and "motorway" in want:
            for l in clipped(f["geom"].simplify(0.05)):
                mwy_paths.append(_path(l.coords))

    # place names
    place_style = {
        "city": (7.0, "bold", "normal", 1.1, True),
        "town": (4.2, "bold", "normal", 0.35, False),
        "suburb": (2.7, "normal", "normal", 0.12, False),
        "village": (2.7, "normal", "normal", 0.12, False),
        "island": (2.6, "normal", "italic", 0.12, False),
        "islet": (2.2, "normal", "italic", 0.1, False),
    }
    order = ["city", "town", "suburb", "village", "island", "islet"]
    pts = [p for p in osm.points if p["kind"] in place_style and p["kind"] in set(ov.places) | {"island", "islet"}
           and p["name"]]
    pts.sort(key=lambda p: (order.index(p["kind"]), -float(p["tags"].get("population", 0) or 0)
                            if str(p["tags"].get("population", "0")).isdigit() else 0))
    names_svg: list[str] = []
    placed_names = 0
    seen_names: set[str] = set()
    for p in pts:
        size, weight, style, spacing, caps = place_style[p["kind"]]
        name = p["name"].upper() if caps else p["name"]
        if name in seen_names:
            continue
        w = text_width(name, size, weight == "bold", spacing, caps)
        h = size * 0.72
        for dx, dy in ((0, 0), (0, -h * 0.9), (0, h * 0.9), (w * 0.35, 0), (-w * 0.35, 0)):
            cx, cy = p["x"] + dx, p["y"] + dy
            b = (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
            if placer.free(b):
                placer.take(b)
                knock(b, 0.4)
                names_svg.append(text(cx, cy + h / 2, name, size, "middle", weight, style, spacing))
                placed_names += 1
                seen_names.add(name)
                break
    stats["place_names"] = placed_names

    # point symbols (+ short labels for the named landmarks)
    sym_count = 0

    def place_label(x, y, s, size, r, style="normal"):
        w, h = text_width(s, size), size * 0.72
        for ax, ay, anchor in ((x + r + 0.6, y + h / 2, "start"), (x - r - 0.6, y + h / 2, "end"),
                               (x, y - r - 0.6, "middle"), (x, y + r + 0.6 + h, "middle")):
            x0 = ax if anchor == "start" else ax - w if anchor == "end" else ax - w / 2
            b = (x0, ay - h, x0 + w, ay)
            if placer.free(b, 0.3):
                placer.take(b)
                knock(b, 0.35)
                names_svg.append(text(ax, ay, s, size, anchor, "normal", style))
                return True
        return False

    for p in osm.points:
        k, x, y = p["kind"], p["x"], p["y"]
        if k not in want or not (ix0 + 1 < x < ix1 - 1 and iy0 + 1 < y < iy1 - 1):
            continue
        r = 0.9
        b = (x - r, y - r, x + r, y + r)
        if not placer.free(b, 0.15):
            continue
        if k == "station":
            sym.append(f'<circle cx="{_f(x)}" cy="{_f(y)}" r="0.55" fill="{INK}"/>')
            r = 0.55
        elif k == "lighthouse":
            sym.append(f'<path d="M{_f(x - 0.55)} {_f(y + 0.9)} L{_f(x - 0.3)} {_f(y - 0.6)} L{_f(x + 0.3)} {_f(y - 0.6)} '
                       f'L{_f(x + 0.55)} {_f(y + 0.9)} Z" fill="{INK}"/>'
                       f'<path d="M{_f(x - 1.1)} {_f(y - 0.9)} L{_f(x + 1.1)} {_f(y - 0.3)} M{_f(x - 1.1)} {_f(y - 0.3)} '
                       f'L{_f(x + 1.1)} {_f(y - 0.9)}" stroke-width="{_f(lw)}"/>')
        elif k == "peak":
            sym.append(f'<path d="M{_f(x)} {_f(y - 0.7)} L{_f(x + 0.7)} {_f(y + 0.5)} L{_f(x - 0.7)} {_f(y + 0.5)} Z" fill="{INK}"/>')
        elif k == "airport":
            # simple aircraft glyph, nose up
            s = 1.6
            d = [(0, -1), (0.12, -0.6), (0.12, -0.15), (1, 0.25), (1, 0.4), (0.12, 0.15), (0.1, 0.65), (0.4, 0.85),
                 (0.4, 0.95), (0, 0.85), (-0.4, 0.95), (-0.4, 0.85), (-0.1, 0.65), (-0.12, 0.15), (-1, 0.4), (-1, 0.25),
                 (-0.12, -0.15), (-0.12, -0.6)]
            sym.append(f'<path d="{_path([(x + a * s, y + c * s) for a, c in d])} Z" fill="{INK}"/>')
            r = 1.6
        elif k == "stadium":
            sym.append(f'<ellipse cx="{_f(x)}" cy="{_f(y)}" rx="1.0" ry="0.7" fill="none" stroke-width="{_f(lw * 1.4)}"/>'
                       f'<ellipse cx="{_f(x)}" cy="{_f(y)}" rx="0.45" ry="0.28" fill="{INK}"/>')
            r = 1.0
        else:
            continue
        placer.take((x - r, y - r, x + r, y + r))
        knock((x - r, y - r, x + r, y + r), 0.2)
        sym_count += 1
        label = ""
        if k in ("stadium", "lighthouse", "airport") and p["name"]:
            label = p["name"]
            if k == "airport":
                label = label.replace(" International", "").replace(" Airport", " Airport")
        elif k == "peak" and p["name"]:
            ele = p["tags"].get("ele", "")
            label = f"{p['name']} {ele.split('.')[0]}" if ele[:1].isdigit() else p["name"]
        if label:
            place_label(x, y, label, 1.9, r, "italic" if k == "lighthouse" else "normal")
    stats["symbols"] = sym_count

    # hand-placed labels (sea areas, estuaries...): {"text", "e", "n" (grid_crs metres), "size", "italic", "angle", "spacing"}
    if ov.labels:
        to_proj = Transformer.from_crs(ov.grid_crs, grid.crs, always_xy=True)
        for lab in ov.labels:
            x, y = fr.svg(*to_proj.transform(lab["e"], lab["n"]))
            size = float(lab.get("size", 3.0))
            sp = float(lab.get("spacing", 0.4))
            w, h = text_width(lab["text"], size, False, sp), size * 0.75
            ang = float(lab.get("angle", 0.0))
            ca, sa = abs(math.cos(math.radians(ang))), abs(math.sin(math.radians(ang)))
            b = (float(x) - (w * ca + h * sa) / 2, float(y) - (w * sa + h * ca) / 2,
                 float(x) + (w * ca + h * sa) / 2, float(y) + (w * sa + h * ca) / 2)
            placer.take(b)
            names_svg.append(text(float(x), float(y) + h * 0.35, lab["text"], size, "middle", "normal",
                                  "italic" if lab.get("italic", True) else "normal", sp, ang))

    # river / canal names along the line
    by_name: dict[tuple[str, str], list] = {}
    for f in osm.lines:
        if f["kind"] in ("river", "canal") and f["name"]:
            by_name.setdefault((f["kind"], f["name"]), []).append(f["geom"])
    for (wkind, nm), geoms in sorted(by_name.items(), key=lambda kv: (kv[0][0] != "river", -sum(g.length for g in kv[1]))):
        merged = _lines(linemerge(MultiLineString([c for g in geoms for c in _lines(g)])))
        merged = [l for g in merged for l in clipped(g)]
        if not merged:
            continue
        big = wkind == "river" and sum(l.length for l in merged) > 80
        size = 4.0 if big else 2.2
        s_txt = nm.upper() if big else nm
        spacing = 1.2 if big else 0.1
        w = text_width(s_txt, size, False, spacing, big)
        line = max(merged, key=lambda l: l.length)
        if line.length < w * 1.2:
            continue
        for frac in (0.5, 0.35, 0.65, 0.2, 0.8):
            pt = line.interpolate(frac, normalized=True)
            a = line.interpolate(max(0.0, frac * line.length - w / 2))
            bpt = line.interpolate(min(line.length, frac * line.length + w / 2))
            ang = math.degrees(math.atan2(bpt.y - a.y, bpt.x - a.x))
            if ang > 90:
                ang -= 180
            elif ang < -90:
                ang += 180
            # bounding box of the rotated label
            ca, sa = abs(math.cos(math.radians(ang))), abs(math.sin(math.radians(ang)))
            h = size * 0.75
            bw, bh = w * ca + h * sa, w * sa + h * ca
            b = (pt.x - bw / 2, pt.y - bh / 2, pt.x + bw / 2, pt.y + bh / 2)
            if placer.free(b, 0.3):
                placer.take(b)
                names_svg.append(text(pt.x, pt.y + h * 0.35, s_txt, size, "middle", "normal", "italic", spacing, ang))
                break

    # ---- contours -----------------------------------------------------------
    if progress:
        progress("overlay: tracing contours")
    xs_m = grid.easting
    ys_m = grid.northing
    step = max(1, int(round(25.0 / grid.pitch_m)))  # contour from a ~25 m grid
    z = elev[::step, ::step].astype(np.float64)
    xs_m, ys_m = xs_m[::step], ys_m[::step]
    pitch = grid.pitch_m * step
    z = ndimage.gaussian_filter(z, sigma=max(ov.contour_smooth_m / pitch, 0.5), mode="nearest")
    sx, _ = fr.svg(xs_m, np.full_like(xs_m, ys_m[0]))
    _, sy = fr.svg(np.full_like(ys_m, xs_m[0]), ys_m)
    contour_paths: list[str] = []
    index_paths: list[str] = []
    contour_labels: list[str] = []
    n_lines = 0
    lo = math.ceil(max(float(z.min()), 0.0) / ov.contour_m) * ov.contour_m
    if lo <= 0:
        lo = ov.contour_m
    levels = np.arange(lo, float(z.max()) + 1e-9, ov.contour_m)
    for lev in levels:
        is_index = abs(lev / ov.index_contour_m - round(lev / ov.index_contour_m)) < 1e-6
        for l in contour_lines(z, sx, sy, float(lev)):
            for c in clipped(l.simplify(0.06)):
                if c.length < ov.contour_min_len_mm:
                    continue
                n_lines += 1
                (index_paths if is_index else contour_paths).append(_path(c.coords))
                if is_index and c.length > 45:
                    # one label per ~120 mm of line
                    nlab = max(1, int(c.length // 120))
                    for k in range(nlab):
                        d0 = (k + 0.5) * c.length / nlab
                        pt = c.interpolate(d0)
                        a = c.interpolate(max(0, d0 - 2.5))
                        bq = c.interpolate(min(c.length, d0 + 2.5))
                        ang = math.degrees(math.atan2(bq.y - a.y, bq.x - a.x))
                        # contour numbers read uphill-up; keep simple: upright text
                        if ang > 90:
                            ang -= 180
                        elif ang < -90:
                            ang += 180
                        s_txt = f"{int(lev)}"
                        size = 1.8
                        w = text_width(s_txt, size)
                        h = size * 0.72
                        ca, sa = abs(math.cos(math.radians(ang))), abs(math.sin(math.radians(ang)))
                        bw, bh = w * ca + h * sa, w * sa + h * ca
                        b = (pt.x - bw / 2, pt.y - bh / 2, pt.x + bw / 2, pt.y + bh / 2)
                        if placer.free(b, 0.2):
                            placer.take(b)
                            knock(b, 0.25)
                            contour_labels.append(text(pt.x, pt.y + h / 2, s_txt, size, "middle", "normal", "normal", 0,
                                                       ang))
    stats["contour_lines"] = n_lines
    stats["contour_levels_m"] = [float(v) for v in levels]

    # ---- grid -----------------------------------------------------------------
    gtr_to = Transformer.from_crs(ov.grid_crs, grid.crs, always_xy=True)
    gtr_from = Transformer.from_crs(grid.crs, ov.grid_crs, always_xy=True)
    ge, gn = gtr_from.transform(*fr.proj(np.array([ix0, ix1, ix0, ix1]), np.array([iy0, iy0, iy1, iy1])))
    gm = ov.grid_m
    e_lines = np.arange(math.floor(min(ge) / gm) * gm, max(ge) + gm, gm)
    n_lines_ = np.arange(math.floor(min(gn) / gm) * gm, max(gn) + gm, gm)
    grid_paths: list[str] = []
    border: list[str] = []
    tick = 1.6
    num_size = 2.6
    t_, r_, b_, l_ = fr.margins

    def gline(xs_g, ys_g):
        e, n = gtr_to.transform(xs_g, ys_g)
        x, y = fr.svg(e, n)
        return LineString(list(zip(x, y)))

    def short(v):  # OS style: km within the 100 km square, 2 digits
        return f"{int(round(v / 1000)) % 100:02d}"

    for ev in e_lines:
        ln = gline(np.full(30, ev), np.linspace(min(gn) - gm, max(gn) + gm, 30))
        seg = ln.intersection(neat)
        for c in _lines(seg):
            grid_paths.append(_path(c.coords))
        for edge_y, sgn in ((iy0, -1), (iy1, 1)):
            hit = ln.intersection(LineString([(ix0 - 50, edge_y), (ix1 + 50, edge_y)]))
            if hit.is_empty or not isinstance(hit, Point):
                continue
            x = hit.x
            if not (ix0 - 0.01 <= x <= ix1 + 0.01):
                continue
            border.append(f'<path d="M{_f(x)} {_f(edge_y)} L{_f(x)} {_f(edge_y + sgn * tick)}" stroke-width="{_f(lw * 1.4)}"/>')
            ty = edge_y - tick - 0.9 if sgn < 0 else edge_y + tick + num_size * 0.75
            border.append(text(x, ty, short(ev), num_size))
    for nv in n_lines_:
        ln = gline(np.linspace(min(ge) - gm, max(ge) + gm, 30), np.full(30, nv))
        for c in _lines(ln.intersection(neat)):
            grid_paths.append(_path(c.coords))
        for edge_x, sgn in ((ix0, -1), (ix1, 1)):
            hit = ln.intersection(LineString([(edge_x, iy0 - 50), (edge_x, iy1 + 50)]))
            if hit.is_empty or not isinstance(hit, Point):
                continue
            y = hit.y
            if not (iy0 - 0.01 <= y <= iy1 + 0.01):
                continue
            border.append(f'<path d="M{_f(edge_x)} {_f(y)} L{_f(edge_x + sgn * tick)} {_f(y)}" stroke-width="{_f(lw * 1.4)}"/>')
            tx = edge_x - tick - 0.5 if sgn < 0 else edge_x + tick + 0.5
            border.append(text(tx, y + num_size * 0.36, short(nv), num_size, "end" if sgn < 0 else "start"))

    # full references at the south-west and north-east corners of the neatline, and square letters
    if ov.grid_letters and "27700" in ov.grid_crs:
        seen = []
        for ev in e_lines[:-1]:
            for nv in n_lines_[:-1]:
                L = os_square_letters(ev + 1, nv + 1)
                if L and L not in [s for s, _, _ in seen]:
                    seen.append((L, ev, nv))
        for L, ev, nv in seen:
            # put the letters in the inner corner of that square's part of the map
            e100, n100 = (ev // 100000) * 100000, (nv // 100000) * 100000
            ce = min(max(e100 + 1, min(ge)), max(ge))
            cn = min(max(n100 + 1, min(gn)), max(gn))
            x, y = fr.svg(*gtr_to.transform(ce, cn))
            x = float(np.clip(x, ix0, ix1))
            y = float(np.clip(y, iy0, iy1))
            wbox = text_width(L, 5.0, True) + 2.4
            bx = x + 1.5 if x < (ix0 + ix1) / 2 else x - wbox - 1.5
            by = y - 8.2 if y > (iy0 + iy1) / 2 else y + 1.5
            b = (bx, by, bx + wbox, by + 6.7)
            body_l = (f'<rect x="{_f(b[0])}" y="{_f(b[1])}" width="{_f(wbox)}" height="6.7" fill="none" '
                      f'stroke-width="{_f(lw * 1.6)}"/>' + text(bx + wbox / 2, by + 5.3, L, 5.0, "middle", "bold"))
            knock(b, 0.4)
            placer.take(b)
            border.append(body_l)
        stats["grid_squares"] = [s for s, _, _ in seen]

    # tile seams (where the glued tiles meet), as alignment ticks outside the neatline
    seam_ticks = []
    for k in range(1, cfg.tiles_x):
        x = k * cfg.tile_w_mm - fr.win_x0
        if ix0 < x < ix1:
            for yy, sg in ((iy0, -1), (iy1, 1)):
                seam_ticks.append(f'<path d="M{_f(x - 1.2)} {_f(yy + sg * 4.6)} L{_f(x + 1.2)} {_f(yy + sg * 4.6)} '
                                  f'L{_f(x)} {_f(yy + sg * 3.4)} Z" fill="{INK}"/>')
    for k in range(1, cfg.tiles_y):
        y = fr.win_h - (k * cfg.tile_h_mm - fr.win_y0)
        if iy0 < y < iy1:
            for xx, sg in ((ix0, -1), (ix1, 1)):
                seam_ticks.append(f'<path d="M{_f(xx + sg * 6.6)} {_f(y - 1.2)} L{_f(xx + sg * 6.6)} {_f(y + 1.2)} '
                                  f'L{_f(xx + sg * 5.4)} {_f(y)} Z" fill="{INK}"/>')

    # ---- bottom border: title, scale bar, credits ------------------------------
    by0 = iy1 + tick + num_size + 1.4  # below the easting numbers
    foot: list[str] = []
    title = ov.title or cfg.name.title()
    foot.append(text(ix0, by0 + 3.6, title, 4.0, "start", "bold", "normal", 0.5))
    sub = ov.subtitle or ""
    ex_txt = f" · heights ×{exaggeration:.1f}" if exaggeration else ""
    foot.append(text(ix0, by0 + 6.4, (sub + " · " if sub else "") + f"Scale 1:{int(cfg.scale):,}".replace(",", " ")
                     + ex_txt + f" · contours every {ov.contour_m:g} m", 1.9, "start"))
    # scale bar: 0-3 km, alternating segments
    km_mm = 1000 * cfg.mm_per_m
    nkm = 3 if km_mm * 3 <= 70 else 1
    sbx = (ix0 + ix1) / 2 - nkm * km_mm / 2 + 10
    sby = by0 + 2.0
    for k in range(nkm * 2):
        x0 = sbx + k * km_mm / 2
        fill = INK if k % 2 == 0 else "none"
        foot.append(f'<rect x="{_f(x0)}" y="{_f(sby)}" width="{_f(km_mm / 2)}" height="1.1" fill="{fill}" '
                    f'stroke-width="{_f(lw)}"/>')
    for k in range(nkm + 1):
        foot.append(text(sbx + k * km_mm, sby + 3.6, f"{k}" + (" km" if k == nkm else ""), 1.9))
    credits = [
        "Map data © OpenStreetMap contributors, ODbL — openstreetmap.org/copyright",
        "Relief & contours: NASA/USGS SRTM 1″ (public domain) · Sea & lakes: Natural Earth (public domain)",
        f"Grid: {'British National Grid (EPSG:27700), ' if '27700' in ov.grid_crs else ''}{gm / 1000:g} km squares"
        " · not Ordnance Survey mapping · made with my3dmaps",
    ]
    for k, c in enumerate(credits):
        foot.append(text(ix1, by0 + 2.3 + k * 2.3, c, 1.6, "end"))

    # ---- assemble ---------------------------------------------------------------
    W, H = fr.win_w, fr.win_h
    parts_defs.append(
        f'<mask id="labels" maskUnits="userSpaceOnUse" x="0" y="0" width="{_f(W)}" height="{_f(H)}">'
        f'<rect x="0" y="0" width="{_f(W)}" height="{_f(H)}" fill="#fff"/>' + "".join(label_masks) + "</mask>")
    if mwy_paths:
        parts_defs.append(
            f'<mask id="mwy" maskUnits="userSpaceOnUse" x="0" y="0" width="{_f(W)}" height="{_f(H)}">'
            f'<rect x="0" y="0" width="{_f(W)}" height="{_f(H)}" fill="#fff"/>'
            f'<g fill="none" stroke="#000" stroke-width="0.45" stroke-linecap="round" stroke-linejoin="round">'
            + "".join(f'<path d="{d}"/>' for d in mwy_paths) + "</g></mask>")
    parts_defs.append(f'<clipPath id="neat"><rect x="{_f(ix0)}" y="{_f(iy0)}" width="{_f(ix1 - ix0)}" '
                      f'height="{_f(iy1 - iy0)}"/></clipPath>')

    body.append(f'<g id="grid" mask="url(#labels)" clip-path="url(#neat)" stroke-width="{_f(lw * 0.8)}" '
                f'stroke-opacity="0.85">' + "".join(f'<path d="{d}"/>' for d in grid_paths) + "</g>")
    body.append(f'<g id="contours" mask="url(#labels)" clip-path="url(#neat)">'
                f'<g stroke-width="{_f(lw)}">' + "".join(f'<path d="{d}"/>' for d in contour_paths) + "</g>"
                f'<g stroke-width="{_f(lw * 2.0)}">' + "".join(f'<path d="{d}"/>' for d in index_paths) + "</g></g>")
    body.append(f'<g id="contour-labels" fill="{INK}" stroke="none">' + "".join(contour_labels) + "</g>")
    body.append(f'<g id="lines" mask="url(#labels)" clip-path="url(#neat)" stroke-linecap="butt">'
                + "".join(lines_svg) + "</g>")
    if mwy_paths:
        body.append(f'<g id="motorways" mask="url(#mwy)" clip-path="url(#neat)" stroke-width="1.05" '
                    f'stroke-linecap="round" stroke-linejoin="round">'
                    + "".join(f'<path d="{d}"/>' for d in mwy_paths) + "</g>")
    body.append(f'<g id="symbols" stroke-linecap="round">' + "".join(sym) + "</g>")
    body.append(f'<g id="names" fill="{INK}" stroke="none">' + "".join(names_svg) + "</g>")
    body.append(f'<g id="border" fill="{INK}">'
                f'<rect x="{_f(ix0)}" y="{_f(iy0)}" width="{_f(ix1 - ix0)}" height="{_f(iy1 - iy0)}" fill="none" '
                f'stroke-width="{_f(lw * 2.4)}"/>'
                + "".join(b if b.startswith("<text") else b.replace("<path ", '<path fill="none" ', 1) for b in border)
                + "".join(seam_ticks) + "</g>")
    body.append(f'<g id="footer" fill="{INK}">' + "".join(foot) + "</g>")

    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_f(W)}mm" height="{_f(H)}mm" viewBox="0 0 {_f(W)} {_f(H)}">\n'
        f'<title>{html.escape(title)} overlay, 1:{int(cfg.scale)}, print at 100 %</title>\n'
        f'<desc>Black-only overlay for transparent film. Map data (c) OpenStreetMap contributors (ODbL); '
        f'contours from NASA/USGS SRTM (public domain).</desc>\n'
        "<defs>" + "".join(parts_defs) + "</defs>\n"
        f'<g fill="none" stroke="{INK}" stroke-width="{_f(lw)}" font-family="{html.escape(ov.font_family)}" stroke-linejoin="round">\n'
        + "\n".join(body) + "\n</g>\n</svg>\n"
    )
    out = Path(out_path)
    out.write_text(svg)
    stats["window_mm"] = [W, H]
    stats["neatline_mm"] = [round(ix0, 2), round(iy0, 2), round(ix1, 2), round(iy1, 2)]
    sw_e, sw_n = gtr_from.transform(*fr.proj(np.array([0.0]), np.array([H])))
    ne_e, ne_n = gtr_from.transform(*fr.proj(np.array([W]), np.array([0.0])))
    stats["window_grid_extent"] = [round(float(sw_e[0])), round(float(sw_n[0])), round(float(ne_e[0])), round(float(ne_n[0]))]
    return OverlayResult(out, stats)


# ---------------------------------------------------------------------------
# top view of the relief (for checking the overlay against the print)

def _png_bytes(rgb: np.ndarray) -> bytes:
    import struct
    import zlib

    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[j].tobytes() for j in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_topview_png(cfg: ProjectConfig, grid: Grid, cls, exaggeration: float, path: str | Path,
                      window_only: bool = True) -> Path:
    """Hill-shaded, band-coloured top view of the model, one pixel per grid cell.
    With ``window_only`` it is cropped to the overlay window, so it lines up with overlay.svg."""
    from .colors import normalize_hex

    def rgb(h):
        h = normalize_hex(h).lstrip("#")
        return [int(h[k:k + 2], 16) for k in (0, 2, 4)]

    lut = np.array([rgb(b.color) for b in cls.bands], dtype=np.float64)
    img = lut[cls.cells]  # [ny-1, nx-1, 3], row 0 = south
    e = cls.elev * cfg.mm_per_m * exaggeration  # model mm
    pitch = cfg.tile_w_mm / grid.nodes_per_tile
    ce = (e[:-1, :-1] + e[:-1, 1:] + e[1:, :-1] + e[1:, 1:]) / 4.0
    gy, gx = np.gradient(ce, pitch)
    # light from the north-west, 45 degrees up (gy is +north)
    lx, ly, lz = -0.5, 0.5, 0.7071
    nz = 1.0 / np.sqrt(1 + gx ** 2 + gy ** 2)
    shade = (-gx * lx - gy * ly + lz) * nz
    shade = np.clip(0.55 + 0.6 * (shade - lz), 0.35, 1.25)
    img = np.clip(img * shade[..., None], 0, 255).astype(np.uint8)[::-1]  # north up
    if window_only:
        fr = make_frame(cfg, grid)
        H = img.shape[0]
        x0 = int(round(fr.win_x0 / pitch))
        x1 = int(round((fr.win_x0 + fr.win_w) / pitch))
        ytop = int(round((cfg.model_mm[1] - fr.win_y0 - fr.win_h) / pitch))
        y1 = int(round((cfg.model_mm[1] - fr.win_y0) / pitch))
        img = img[max(ytop, 0):min(y1, H), max(x0, 0):x1]
    p = Path(path)
    p.write_bytes(_png_bytes(np.ascontiguousarray(img)))
    return p
