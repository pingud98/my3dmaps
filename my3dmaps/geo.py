"""Coordinate systems and tile-grid geometry.

The model grid lives in a local UTM zone (metric, conformal enough over a few
hundred km) so 1 mm on the print maps to a fixed number of metres on the
ground in every direction.  All tiles of a project share one global node grid
so neighbouring tiles have bit-identical border elevations.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pyproj import CRS, Transformer

from .config import ProjectConfig


def utm_crs_for(lat: float, lon: float) -> CRS:
    zone = int(math.floor((lon + 180) / 6) % 60) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


@dataclass
class Grid:
    """Global node grid for a whole project, in UTM metres.

    ``easting[i]``/``northing[j]`` are node coordinates; ``northing`` increases
    with ``j`` (south to north), so row 0 is the *southern* edge.
    """

    crs: CRS
    easting: np.ndarray  # [nx]
    northing: np.ndarray  # [ny]
    nodes_per_tile: int  # cells along a tile's width (x)
    tiles_x: int
    tiles_y: int
    pitch_m: float
    nodes_per_tile_y: int = 0  # cells along a tile's height (y); 0 = square tiles

    def __post_init__(self):
        if not self.nodes_per_tile_y:
            self.nodes_per_tile_y = self.nodes_per_tile

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.northing), len(self.easting))

    def tile_slices(self, tx: int, ty: int) -> tuple[slice, slice]:
        n, m = self.nodes_per_tile, self.nodes_per_tile_y
        return slice(ty * m, ty * m + m + 1), slice(tx * n, tx * n + n + 1)

    def lonlat(self) -> tuple[np.ndarray, np.ndarray]:
        """Longitude/latitude arrays [ny, nx] of every node."""
        tr = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        E, N = np.meshgrid(self.easting, self.northing)
        lon, lat = tr.transform(E, N)
        return np.asarray(lon), np.asarray(lat)

    def bbox_lonlat(self, margin_deg: float = 0.02) -> tuple[float, float, float, float]:
        """(min_lon, min_lat, max_lon, max_lat) covering the grid."""
        tr = Transformer.from_crs(self.crs, "EPSG:4326", always_xy=True)
        es = np.array([self.easting[0], self.easting[-1], self.easting[0], self.easting[-1],
                       self.easting[len(self.easting) // 2], self.easting[len(self.easting) // 2]])
        ns = np.array([self.northing[0], self.northing[0], self.northing[-1], self.northing[-1],
                       self.northing[0], self.northing[-1]])
        lon, lat = tr.transform(es, ns)
        return (float(min(lon)) - margin_deg, float(min(lat)) - margin_deg,
                float(max(lon)) + margin_deg, float(max(lat)) + margin_deg)


def project_crs(cfg: ProjectConfig) -> CRS:
    if not cfg.crs or cfg.crs.lower() == "utm":
        return utm_crs_for(cfg.center_lat, cfg.center_lon)
    crs = CRS.from_user_input(cfg.crs)
    if not crs.is_projected:
        raise ValueError(f"crs {cfg.crs} is not a projected (metric) CRS")
    return crs


def grid_center(cfg: ProjectConfig, crs: CRS | None = None) -> tuple[float, float]:
    """Grid centre in the project CRS (``center_xy`` when given, else the projected lat/lon)."""
    crs = crs or project_crs(cfg)
    if cfg.center_xy is not None and cfg.crs.lower() != "utm":
        return float(cfg.center_xy[0]), float(cfg.center_xy[1])
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    return fwd.transform(cfg.center_lon, cfg.center_lat)


def make_grid(cfg: ProjectConfig, nodes_per_tile: int | None = None) -> Grid:
    crs = project_crs(cfg)
    ce, cn = grid_center(cfg, crs)
    n = nodes_per_tile or cfg.nodes_per_tile
    m = cfg.nodes_y_for(n)
    pitch = cfg.tile_m / n
    width_m = cfg.tiles_x * cfg.tile_m
    height_m = cfg.tiles_y * m * pitch
    e0 = ce - width_m / 2
    n0 = cn - height_m / 2
    easting = e0 + pitch * np.arange(cfg.tiles_x * n + 1)
    northing = n0 + pitch * np.arange(cfg.tiles_y * m + 1)
    return Grid(crs, easting, northing, n, cfg.tiles_x, cfg.tiles_y, pitch, m)


def tile_outlines_lonlat(cfg: ProjectConfig) -> list[dict]:
    """Per-tile corner polygons (lon/lat) for drawing the grid on a map."""
    crs = project_crs(cfg)
    ce, cn = grid_center(cfg, crs)
    e0 = ce - cfg.tiles_x * cfg.tile_m / 2
    n0 = cn - cfg.tiles_y * cfg.tile_h_m / 2
    easting = e0 + cfg.tile_m * np.arange(cfg.tiles_x + 1)
    northing = n0 + cfg.tile_h_m * np.arange(cfg.tiles_y + 1)
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    out = []
    for ty in range(cfg.tiles_y):
        for tx in range(cfg.tiles_x):
            es = [easting[tx], easting[tx + 1], easting[tx + 1], easting[tx]]
            ns = [northing[ty], northing[ty], northing[ty + 1], northing[ty + 1]]
            lon, lat = tr.transform(es, ns)
            out.append({"tx": tx, "ty": ty,
                        "corners": [[float(a), float(b)] for a, b in zip(lat, lon)]})
    return out


def scale_from_extent(cfg: ProjectConfig, half_width_m: float, half_height_m: float) -> float:
    """Scale denominator so the tile grid spans the given half extents (metres)."""
    need_w = 2 * half_width_m / (cfg.tiles_x * cfg.tile_mm / 1000.0)
    need_h = 2 * half_height_m / (cfg.tiles_y * cfg.tile_h_mm / 1000.0)
    return max(need_w, need_h)


NICE_SCALES = [5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000, 60000, 75000, 100000,
               125000, 150000, 200000, 250000, 300000, 400000, 500000, 750000, 1000000]


def nice_scale(scale: float) -> int:
    return min(NICE_SCALES, key=lambda s: abs(math.log(s) - math.log(max(scale, 1))))
