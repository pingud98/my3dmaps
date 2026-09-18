"""Rivers, canals and streams from OpenStreetMap, rasterised as blue surface cells.

Natural Earth's river layer only carries the continental rivers, so line
detail comes from OpenStreetMap via the Overpass API.  **OSM is ODbL, not
public domain**: a printed model is an ODbL "produced work", which is fine to
make and share as long as it is credited "© OpenStreetMap contributors" (see
SOURCES.md).  Set ``rivers.enabled = false`` in a project to keep a model
strictly public-domain.

What is fetched (``waterway`` line features and ``natural=water`` river/canal
areas) only seeds geometry; widths are the OSM ``width`` tag when present,
else a per-class default, and are then widened to a printable minimum so a
0.4 mm nozzle can actually lay the blue down (``min_width_mm``).  Rivers are
painted as a thin skin (``depth_mm``) *on* the terrain surface - they follow
the DEM rather than cutting a channel into it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import requests
import shapely
from pyproj import Transformer
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import polygonize, unary_union

from .config import ProjectConfig, RiversConfig
from .geo import Grid

log = logging.getLogger(__name__)

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "my3dmaps/0.1 (+https://github.com/pingud98/my3dmaps)"
LINE_CLASSES = ("river", "canal", "stream")
AREA_WATER = ("river", "canal")


def cache_dir() -> Path:
    root = os.environ.get("MY3DMAPS_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "my3dmaps")
    p = Path(root) / "osm"
    p.mkdir(parents=True, exist_ok=True)
    return p


def overpass_query(bbox_lonlat: tuple[float, float, float, float], classes: list[str]) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox_lonlat
    cls = "|".join(c for c in classes if c in LINE_CLASSES)
    area = "|".join(AREA_WATER)
    return (
        f"[out:json][timeout:180][bbox:{min_lat:.5f},{min_lon:.5f},{max_lat:.5f},{max_lon:.5f}];\n(\n"
        f'  way["waterway"~"^({cls})$"];\n'
        f'  way["natural"="water"]["water"~"^({area})$"];\n'
        f'  way["waterway"="riverbank"];\n'
        f'  relation["natural"="water"]["water"~"^({area})$"];\n'
        f'  relation["waterway"="riverbank"];\n'
        ");\nout geom;\n"
    )


def fetch_osm(bbox_lonlat: tuple[float, float, float, float], classes: list[str],
              progress: Callable[[str], None] | None = None, timeout: int = 300) -> dict:
    """Overpass JSON for the bbox (cached on disk by bbox + classes)."""
    return fetch_overpass(overpass_query(bbox_lonlat, classes), f"waterways ({', '.join(classes)})", progress, timeout)


def fetch_overpass(q: str, label: str, progress: Callable[[str], None] | None = None, timeout: int = 300) -> dict:
    """Run an Overpass query (cached on disk by query text), trying mirrors and retrying on overload."""
    key = hashlib.sha1(q.encode()).hexdigest()[:16]
    dst = cache_dir() / f"{key}.json"
    if dst.exists():
        return json.loads(dst.read_text())
    if progress:
        progress(f"downloading OpenStreetMap {label} via Overpass")
    urls = [os.environ["MY3DMAPS_OVERPASS_URL"]] if os.environ.get("MY3DMAPS_OVERPASS_URL") else OVERPASS_URLS
    last: Exception | None = None
    for attempt, url in enumerate(urls * 2):
        try:
            r = requests.post(url, data={"data": q}, timeout=timeout, headers={"User-Agent": USER_AGENT})
            if r.status_code in (429, 504, 503):
                last = RuntimeError(f"Overpass {url} answered HTTP {r.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            if "elements" not in data:
                raise RuntimeError("Overpass answer has no elements")
            dst.write_text(json.dumps(data))
            return data
        except (requests.RequestException, ValueError, RuntimeError) as e:  # noqa: PERF203
            last = e
            log.warning("Overpass request failed (%s): %s", url, e)
    raise RuntimeError(f"could not fetch OSM {label}: {last}")


# ---------------------------------------------------------------------------

@dataclass
class RiverFeature:
    kind: str  # "river" | "canal" | "stream"
    geom: object  # shapely geometry in lon/lat: LineString (centreline) or (Multi)Polygon (water area)
    width_m: Optional[float]  # from the OSM width tag, else None
    name: str = ""
    osm_id: int = 0

    @property
    def is_area(self) -> bool:
        return isinstance(self.geom, (Polygon, MultiPolygon))


@dataclass
class RiverFeatures:
    """Fetched + projected river geometry for a grid (independent of print settings)."""

    features: list[RiverFeature] = field(default_factory=list)  # lon/lat
    projected: list[tuple[str, object, Optional[float]]] = field(default_factory=list)  # (kind, geom in grid CRS, width_m)
    source: str = "osm"
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.projected)

    def length_km(self) -> float:
        return float(sum(g.length for k, g, w in self.projected if isinstance(g, LineString)) / 1000.0)

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        for k, _, _ in self.projected:
            kinds[k] = kinds.get(k, 0) + 1
        return {"source": self.source, "features": self.count, "by_kind": kinds,
                "length_km": round(self.length_km(), 1), "error": self.error}


_num = re.compile(r"[-+]?\d*\.?\d+")


def parse_width(tag: object) -> Optional[float]:
    """'12', '12.5 m', '3-5' -> metres (first number); None if unparsable."""
    if tag is None:
        return None
    m = _num.search(str(tag))
    if not m:
        return None
    try:
        v = float(m.group())
    except ValueError:
        return None
    return v if 0 < v < 5000 else None


def _underground(tags: dict) -> bool:
    return tags.get("tunnel", "no") not in ("no", "") or tags.get("covered", "no") not in ("no", "") \
        or tags.get("location") == "underground"


def _ring(coords: list[dict]) -> list[tuple[float, float]]:
    return [(c["lon"], c["lat"]) for c in coords]


def parse_elements(elements: list[dict]) -> list[RiverFeature]:
    feats: list[RiverFeature] = []
    for e in elements:
        tags = e.get("tags", {}) or {}
        if _underground(tags):
            continue
        ww = tags.get("waterway", "")
        water = tags.get("water", "")
        is_area_tag = (tags.get("natural") == "water" and water in AREA_WATER) or ww == "riverbank"
        kind = ww if ww in LINE_CLASSES else (water if water in AREA_WATER else "river")
        width = parse_width(tags.get("width"))
        name = tags.get("name", "")
        if e["type"] == "way":
            pts = _ring(e.get("geometry") or [])
            if len(pts) < 2:
                continue
            if is_area_tag and len(pts) >= 4 and pts[0] == pts[-1]:
                g = Polygon(pts)
                if not g.is_valid:
                    g = shapely.make_valid(g)
                if not g.is_empty:
                    feats.append(RiverFeature(kind, g, None, name, e["id"]))
                continue
            if ww in LINE_CLASSES:
                feats.append(RiverFeature(kind, LineString(pts), width, name, e["id"]))
        elif e["type"] == "relation" and is_area_tag:
            outers, inners = [], []
            for m in e.get("members", []):
                if m.get("type") != "way" or not m.get("geometry"):
                    continue
                pts = _ring(m["geometry"])
                if len(pts) < 2:
                    continue
                (inners if m.get("role") == "inner" else outers).append(LineString(pts))
            if not outers:
                continue
            try:
                polys = list(polygonize(unary_union(outers)))
                if not polys:
                    continue
                g = unary_union(polys)
                if inners:
                    holes = list(polygonize(unary_union(inners)))
                    if holes:
                        g = g.difference(unary_union(holes))
            except Exception as ex:  # noqa: BLE001 - malformed multipolygons are common in OSM
                log.debug("skipping relation %s: %s", e.get("id"), ex)
                continue
            if not g.is_empty:
                feats.append(RiverFeature(kind, g, None, name, e["id"]))
    return feats


def project_features(feats: list[RiverFeature], grid: Grid,
                     bbox_lonlat: tuple[float, float, float, float]) -> list[tuple[str, object, Optional[float]]]:
    tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)

    def f(coords):
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    clip_ll = box(*bbox_lonlat)
    pad = 2 * grid.pitch_m
    clip = box(grid.easting[0] - pad, grid.northing[0] - pad, grid.easting[-1] + pad, grid.northing[-1] + pad)
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for ft in feats:
            g = ft.geom.intersection(clip_ll)
            if g.is_empty:
                continue
            gg = shapely.transform(g, f)
            if not gg.is_valid:
                gg = shapely.make_valid(gg)
            gg = gg.intersection(clip)
            if gg.is_empty:
                continue
            # keep only the geometry types we can rasterise
            parts = [gg] if gg.geom_type in ("LineString", "Polygon") else list(getattr(gg, "geoms", []))
            for p in parts:
                if p.geom_type == "LineString" and not ft.is_area:
                    out.append((ft.kind, p, ft.width_m))
                elif p.geom_type == "Polygon" and ft.is_area:
                    out.append((ft.kind, p, None))
    return out


def load_rivers(cfg: ProjectConfig, grid: Grid, progress: Callable[[str], None] | None = None) -> RiverFeatures:
    """Fetch (cached) and project every river feature for the grid.  Never raises:
    a network failure is reported in ``error`` and the model simply has no rivers."""
    rc = cfg.rivers
    classes = rc.active_classes(cfg.scale)
    bbox = grid.bbox_lonlat()
    try:
        data = fetch_osm(bbox, classes, progress)
        feats = parse_elements(data.get("elements", []))
        proj = project_features(feats, grid, bbox)
        if progress:
            progress(f"{len(proj)} river/canal features on the grid")
        return RiverFeatures(feats, proj, "osm")
    except Exception as e:  # noqa: BLE001
        log.warning("rivers unavailable: %s", e)
        if progress:
            progress(f"rivers unavailable: {e}")
        return RiverFeatures([], [], "osm", str(e))


def rasterize_rivers(rivers: RiverFeatures | None, grid: Grid, cfg: ProjectConfig) -> np.ndarray:
    """Boolean cell mask [ny-1, nx-1]: cells whose centre lies in a river.

    Line features are buffered to their printed width: the OSM width (or the
    per-class default), times ``width_scale``, but never narrower than
    ``min_width_mm`` on the print or than about one grid cell, so a river is
    always at least one cell wide at any preview/print resolution.
    """
    if rivers is None or not rivers.projected:
        ny, nx = grid.shape
        return np.zeros((ny - 1, nx - 1), dtype=bool)
    rc = cfg.rivers
    return rasterize_projected(rivers.projected, grid, cfg.scale, rc.min_width_mm, rc.width_scale, rc.default_width_m)


def rasterize_projected(projected: list[tuple[str, object, Optional[float]]], grid: Grid, scale: float,
                        min_width_mm: float, width_scale: float, default_width_m: dict[str, float],
                        line_floor_cells: float = 1.05) -> np.ndarray:
    """Cell mask from projected (kind, geometry, width_m) features: polygons as they
    are, lines buffered to max(width, printable minimum, ~one cell)."""
    ny, nx = grid.shape
    empty = np.zeros((ny - 1, nx - 1), dtype=bool)
    if not projected:
        return empty
    m_per_mm = scale / 1000.0
    floor_m = max(min_width_mm * m_per_mm, line_floor_cells * grid.pitch_m)
    geoms = []
    for kind, g, width in projected:
        if g.geom_type == "Polygon":
            geoms.append(g)
            continue
        w = (width if width is not None else default_width_m.get(kind, 10.0)) * width_scale
        w = max(w, floor_m)
        geoms.append(g.buffer(w / 2.0, cap_style="flat", join_style="round"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        u = unary_union(geoms)
    if u.is_empty:
        return empty
    shapely.prepare(u)
    cx = 0.5 * (grid.easting[:-1] + grid.easting[1:])
    cy = 0.5 * (grid.northing[:-1] + grid.northing[1:])
    # only test cells inside the union's bounding box (rivers are sparse)
    x0, y0, x1, y1 = u.bounds
    ix = np.nonzero((cx >= x0) & (cx <= x1))[0]
    iy = np.nonzero((cy >= y0) & (cy <= y1))[0]
    if len(ix) == 0 or len(iy) == 0:
        return empty
    CX, CY = np.meshgrid(cx[ix], cy[iy])
    hit = shapely.contains_xy(u, CX.ravel(), CY.ravel()).reshape(CX.shape)
    out = empty
    out[iy[0]:iy[-1] + 1, ix[0]:ix[-1] + 1] = hit
    return out
