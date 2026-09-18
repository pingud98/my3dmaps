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
    nodes_per_tile: int
    tiles_x: int
    tiles_y: int
    pitch_m: float

    @property
    def shape(self) -> tuple[int, int]:
        return (len(self.northing), len(self.easting))

    def tile_slices(self, tx: int, ty: int) -> tuple[slice, slice]:
        n = self.nodes_per_tile
        return slice(ty * n, ty * n + n + 1), slice(tx * n, tx * n + n + 1)

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


def make_grid(cfg: ProjectConfig, nodes_per_tile: int | None = None) -> Grid:
    crs = utm_crs_for(cfg.center_lat, cfg.center_lon)
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    ce, cn = fwd.transform(cfg.center_lon, cfg.center_lat)
    n = nodes_per_tile or cfg.nodes_per_tile
    pitch = cfg.tile_m / n
    width_m = cfg.tiles_x * cfg.tile_m
    height_m = cfg.tiles_y * cfg.tile_m
    e0 = ce - width_m / 2
    n0 = cn - height_m / 2
    easting = e0 + pitch * np.arange(cfg.tiles_x * n + 1)
    northing = n0 + pitch * np.arange(cfg.tiles_y * n + 1)
    return Grid(crs, easting, northing, n, cfg.tiles_x, cfg.tiles_y, pitch)


def tile_outlines_lonlat(cfg: ProjectConfig) -> list[dict]:
    """Per-tile corner polygons (lon/lat) for drawing the grid on a map."""
    grid = make_grid(cfg, nodes_per_tile=1)
    tr = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    out = []
    for ty in range(cfg.tiles_y):
        for tx in range(cfg.tiles_x):
            es = [grid.easting[tx], grid.easting[tx + 1], grid.easting[tx + 1], grid.easting[tx]]
            ns = [grid.northing[ty], grid.northing[ty], grid.northing[ty + 1], grid.northing[ty + 1]]
            lon, lat = tr.transform(es, ns)
            out.append({"tx": tx, "ty": ty,
                        "corners": [[float(a), float(b)] for a, b in zip(lat, lon)]})
    return out


def scale_from_extent(cfg: ProjectConfig, half_width_m: float, half_height_m: float) -> float:
    """Scale denominator so the tile grid spans the given half extents (metres)."""
    need_w = 2 * half_width_m / (cfg.tiles_x * cfg.tile_mm / 1000.0)
    need_h = 2 * half_height_m / (cfg.tiles_y * cfg.tile_mm / 1000.0)
    return max(need_w, need_h)


NICE_SCALES = [5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000, 60000, 75000, 100000,
               125000, 150000, 200000, 250000, 300000, 400000, 500000, 750000, 1000000]


def nice_scale(scale: float) -> int:
    return min(NICE_SCALES, key=lambda s: abs(math.log(s) - math.log(max(scale, 1))))
