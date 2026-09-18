"""Built-up areas and major roads from OpenStreetMap (optional, ODbL).

Landuse polygons (``landuse=residential|industrial|commercial|retail``,
including multipolygon relations) are rasterised as they are; road
centrelines (``highway=motorway|trunk|primary`` by default) are buffered to
their OSM ``width``/``lanes`` or a per-class default and then to a printable
minimum, exactly like rivers.  Both become one "urban" surface class painted
in a dark grey skin.  The layer is off by default - see ``UrbanConfig``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union

from .config import ProjectConfig
from .geo import Grid
from .rivers import (RiverFeature, _ring, _underground, fetch_overpass, parse_width, project_features,
                     rasterize_projected)

log = logging.getLogger(__name__)

LANE_WIDTH_M = 3.5


def overpass_query(bbox_lonlat: tuple[float, float, float, float], landuse: list[str], roads: list[str]) -> str:
    min_lon, min_lat, max_lon, max_lat = bbox_lonlat
    parts = []
    if landuse:
        lu = "|".join(landuse)
        parts.append(f'  way["landuse"~"^({lu})$"];\n  relation["landuse"~"^({lu})$"];')
    if roads:
        rd = "|".join(roads)
        parts.append(f'  way["highway"~"^({rd})$"];')
    return (f"[out:json][timeout:180][bbox:{min_lat:.5f},{min_lon:.5f},{max_lat:.5f},{max_lon:.5f}];\n(\n"
            + "\n".join(parts) + "\n);\nout geom;\n")


def road_width(tags: dict) -> Optional[float]:
    w = parse_width(tags.get("width"))
    if w is not None:
        return w
    lanes = parse_width(tags.get("lanes"))
    if lanes is not None and lanes <= 12:
        return lanes * LANE_WIDTH_M
    return None


def parse_elements(elements: list[dict], landuse: list[str], roads: list[str]) -> list[RiverFeature]:
    """(kind, geometry) features: kind is the landuse value or the highway class."""
    feats: list[RiverFeature] = []
    lu_set, rd_set = set(landuse), set(roads)
    for e in elements:
        tags = e.get("tags", {}) or {}
        lu = tags.get("landuse", "")
        hw = tags.get("highway", "")
        if e["type"] == "way":
            pts = _ring(e.get("geometry") or [])
            if len(pts) < 2:
                continue
            if lu in lu_set and len(pts) >= 4 and pts[0] == pts[-1]:
                g = Polygon(pts)
                if not g.is_valid:
                    g = shapely.make_valid(g)
                if not g.is_empty:
                    feats.append(RiverFeature(lu, g, None, tags.get("name", ""), e["id"]))
            elif hw in rd_set and not _underground(tags):
                feats.append(RiverFeature(hw, LineString(pts), road_width(tags), tags.get("name", ""), e["id"]))
        elif e["type"] == "relation" and lu in lu_set:
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
            except Exception as ex:  # noqa: BLE001
                log.debug("skipping relation %s: %s", e.get("id"), ex)
                continue
            if not g.is_empty:
                feats.append(RiverFeature(lu, g, None, tags.get("name", ""), e["id"]))
    return feats


@dataclass
class UrbanFeatures:
    features: list[RiverFeature] = field(default_factory=list)
    projected: list[tuple[str, object, Optional[float]]] = field(default_factory=list)
    source: str = "osm"
    error: str = ""

    @property
    def count(self) -> int:
        return len(self.projected)

    def summary(self) -> dict:
        kinds: dict[str, int] = {}
        for k, _, _ in self.projected:
            kinds[k] = kinds.get(k, 0) + 1
        area_km2 = sum(g.area for k, g, w in self.projected if g.geom_type == "Polygon") / 1e6
        road_km = sum(g.length for k, g, w in self.projected if g.geom_type == "LineString") / 1000.0
        return {"source": self.source, "features": self.count, "by_kind": kinds,
                "built_up_km2": round(float(area_km2), 1), "road_km": round(float(road_km), 1), "error": self.error}


def load_urban(cfg: ProjectConfig, grid: Grid, progress: Callable[[str], None] | None = None) -> UrbanFeatures:
    """Fetch (cached) and project built-up areas + major roads.  Never raises."""
    uc = cfg.urban
    bbox = grid.bbox_lonlat()
    try:
        data = fetch_overpass(overpass_query(bbox, uc.landuse, uc.roads), "built-up areas and roads", progress)
        feats = parse_elements(data.get("elements", []), uc.landuse, uc.roads)
        proj = project_features(feats, grid, bbox)
        if progress:
            progress(f"{len(proj)} urban features on the grid")
        return UrbanFeatures(feats, proj, "osm")
    except Exception as e:  # noqa: BLE001
        log.warning("urban layer unavailable: %s", e)
        if progress:
            progress(f"urban layer unavailable: {e}")
        return UrbanFeatures([], [], "osm", str(e))


def rasterize_urban(urban: UrbanFeatures | None, grid: Grid, cfg: ProjectConfig) -> np.ndarray:
    if urban is None or not urban.projected:
        ny, nx = grid.shape
        return np.zeros((ny - 1, nx - 1), dtype=bool)
    uc = cfg.urban
    return rasterize_projected(urban.projected, grid, cfg.scale, uc.road_min_width_mm, uc.road_width_scale,
                               uc.road_default_width_m)
