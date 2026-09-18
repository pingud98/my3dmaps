"""Water bodies from Natural Earth (public domain) snapped to the DEM.

Natural Earth 1:10m vectors are coarse (hundreds of metres), while SRTM v3
already has lake/sea surfaces flattened to a constant height.  We therefore
use the vectors only as *seeds*: each seeded body's level is read from the
DEM and the water mask grows to the connected DEM region within a small
tolerance of that level, which snaps shorelines to the elevation data.
Ocean = nodes outside the Natural Earth land polygons, level 0.
"""
from __future__ import annotations

import io
import logging
import os
import warnings
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import requests
import shapefile  # pyshp
import shapely
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import box, shape
from shapely.ops import unary_union

from .geo import Grid

log = logging.getLogger(__name__)

NE_BASE = "https://naciscdn.org/naturalearth/10m/physical/{name}.zip"
NE_LAKES = "ne_10m_lakes"
NE_LAND = "ne_10m_land"


def cache_dir() -> Path:
    root = os.environ.get("MY3DMAPS_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "my3dmaps")
    p = Path(root) / "naturalearth"
    p.mkdir(parents=True, exist_ok=True)
    return p


def fetch_ne(name: str, progress: Callable[[str], None] | None = None) -> Path:
    dst = cache_dir() / f"{name}.zip"
    if not dst.exists():
        if progress:
            progress(f"downloading Natural Earth {name}")
        r = requests.get(NE_BASE.format(name=name), timeout=180)
        r.raise_for_status()
        dst.write_bytes(r.content)
    return dst


def read_ne_geoms(zip_path: Path, bbox_lonlat: tuple[float, float, float, float]) -> list:
    """Shapely geometries (lon/lat) from a zipped shapefile intersecting bbox."""
    min_lon, min_lat, max_lon, max_lat = bbox_lonlat
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        shp = next(n for n in names if n.lower().endswith(".shp"))
        base = shp[:-4]
        shx = next((n for n in names if n.lower() == base.lower() + ".shx"), None)
        dbf = next((n for n in names if n.lower() == base.lower() + ".dbf"), None)
        rdr = shapefile.Reader(shp=io.BytesIO(zf.read(shp)),
                               shx=io.BytesIO(zf.read(shx)) if shx else None,
                               dbf=io.BytesIO(zf.read(dbf)) if dbf else None)
        geoms = []
        for s in rdr.iterShapes():
            if s.shapeType == shapefile.NULL or not s.points:
                continue
            bx = s.bbox
            if bx[2] < min_lon or bx[0] > max_lon or bx[3] < min_lat or bx[1] > max_lat:
                continue
            g = shape(s.__geo_interface__)
            if not g.is_valid:
                g = g.buffer(0)
            geoms.append(g)
    return geoms


@dataclass
class WaterBody:
    id: int
    kind: str  # "lake" | "ocean"
    level_m: float
    nodes: int


@dataclass
class WaterResult:
    mask: np.ndarray  # bool [ny, nx]
    level: np.ndarray  # float32 [ny, nx], NaN where not water
    bodies: list[WaterBody] = field(default_factory=list)
    reference_level_m: float | None = None  # level of the largest body

    @property
    def any(self) -> bool:
        return bool(self.mask.any())


def _to_grid_crs(geoms, grid: Grid, bbox_lonlat: tuple[float, float, float, float]):
    """Clip to the lon/lat bbox first (continent-sized polygons would project to
    NaN/inf far outside the UTM zone), then project into the grid's CRS."""
    tr = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)

    def f(coords):
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    clip_ll = box(*bbox_lonlat)
    clip = box(grid.easting[0] - 2 * grid.pitch_m, grid.northing[0] - 2 * grid.pitch_m,
               grid.easting[-1] + 2 * grid.pitch_m, grid.northing[-1] + 2 * grid.pitch_m)
    out = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # GEOS warns on degenerate slivers; harmless
        for g in geoms:
            g = g.intersection(clip_ll)
            if g.is_empty:
                continue
            gg = shapely.transform(g, f)
            if not gg.is_valid:
                gg = shapely.make_valid(gg)
            gg = gg.intersection(clip)
            if not gg.is_empty:
                out.append(gg)
    return out


def _mask_from_geoms(geoms, grid: Grid) -> np.ndarray:
    ny, nx = grid.shape
    if not geoms:
        return np.zeros((ny, nx), dtype=bool)
    u = unary_union(geoms)
    shapely.prepare(u)
    E, N = np.meshgrid(grid.easting, grid.northing)
    return shapely.contains_xy(u, E.ravel(), N.ravel()).reshape(ny, nx)


def _grow_body(seed: np.ndarray, elev: np.ndarray, level: float, tol_lo: float, tol_hi: float,
               min_keep_frac: float = 0.3) -> np.ndarray:
    """Connected DEM region within [level-tol_lo, level+tol_hi] touching the seed."""
    cand = (elev >= level - tol_lo) & (elev <= level + tol_hi)
    lab, n = ndimage.label(cand)
    if n == 0:
        return seed.copy()
    hit = np.unique(lab[seed & cand])
    hit = hit[hit != 0]
    grown = np.isin(lab, hit)
    if grown.sum() < min_keep_frac * seed.sum():
        # DEM isn't flat here (small lake not flattened in SRTM): trust the polygon
        return seed.copy()
    return grown


def detect_water(grid: Grid, elev: np.ndarray, progress: Callable[[str], None] | None = None,
                 lake_tol_m: float = 1.5, ocean_tol_m: float = 0.5, min_nodes: int = 16) -> WaterResult:
    ny, nx = grid.shape
    bbox = grid.bbox_lonlat()
    lakes = _to_grid_crs(read_ne_geoms(fetch_ne(NE_LAKES, progress), bbox), grid, bbox)
    land = _to_grid_crs(read_ne_geoms(fetch_ne(NE_LAND, progress), bbox), grid, bbox)

    lake_seed = _mask_from_geoms(lakes, grid)
    if land:
        ocean_seed = ~_mask_from_geoms(land, grid)
    else:
        ocean_seed = np.zeros((ny, nx), dtype=bool)  # no land polygon at all -> treat as fully inland

    mask = np.zeros((ny, nx), dtype=bool)
    level = np.full((ny, nx), np.nan, dtype=np.float32)
    bodies: list[WaterBody] = []
    bid = 0

    if ocean_seed.any():
        sea = _grow_body(ocean_seed, elev, 0.0, 1e9, ocean_tol_m)
        sea |= elev <= 0.0  # SRTM codes sea as 0
        if sea.sum() >= min_nodes:
            bid += 1
            mask |= sea
            level[sea] = 0.0
            bodies.append(WaterBody(bid, "ocean", 0.0, int(sea.sum())))

    lab, n = ndimage.label(lake_seed & ~mask)
    for k in range(1, n + 1):
        seed = lab == k
        if seed.sum() < 4:
            continue
        lvl = float(np.percentile(elev[seed], 20))
        body = _grow_body(seed, elev, lvl, lake_tol_m, lake_tol_m) & ~mask
        if body.sum() < min_nodes:
            continue
        lvl = float(np.median(elev[body]))
        bid += 1
        mask |= body
        level[body] = lvl
        bodies.append(WaterBody(bid, "lake", lvl, int(body.sum())))

    ref = None
    if bodies:
        ref = max(bodies, key=lambda b: b.nodes).level_m
    return WaterResult(mask, level, bodies, ref)


def flatten(elev: np.ndarray, water: WaterResult) -> np.ndarray:
    out = elev.copy()
    out[water.mask] = water.level[water.mask]
    return out
