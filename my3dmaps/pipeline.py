"""End-to-end orchestration: data -> classification -> tiles -> files (-> G-code)."""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import __version__
from .classify import Classification, cap_exaggeration, classify, suggest_exaggeration
from .config import ProjectConfig
from .dem import sample_srtm
from .geo import Grid, make_grid
from .mesh import CustomGCodeItem, write_3mf, write_glb, write_stl
from .rivers import RiverFeatures, load_rivers
from .slicer import SliceInput, find_executable, slice_tile
from .tile import TileBuild, build_tile
from .urban import UrbanFeatures, load_urban
from .water import WaterResult, detect_water, flatten

log = logging.getLogger(__name__)

Progress = Callable[[str, str, Optional[float]], None]


def _noop(stage: str, message: str, fraction: Optional[float] = None) -> None:
    log.info("[%s] %s", stage, message)


@dataclass
class Terrain:
    grid: Grid
    elev_raw: np.ndarray
    elev: np.ndarray  # water-flattened
    water: Optional[WaterResult]
    dem_info: dict
    rivers: Optional[RiverFeatures] = None
    urban: Optional[UrbanFeatures] = None

    @property
    def relief_m(self) -> float:
        return float(self.elev.max() - self.elev.min())


def prepare_terrain(cfg: ProjectConfig, progress: Progress = _noop, nodes_per_tile: int | None = None) -> Terrain:
    progress("grid", "building UTM node grid", 0.02)
    grid = make_grid(cfg, nodes_per_tile)
    lon, lat = grid.lonlat()
    progress("dem", f"sampling SRTM onto {grid.shape[1]}x{grid.shape[0]} nodes", 0.05)
    elev, info = sample_srtm(lon, lat, cfg.dem_source, lambda m: progress("dem", m, None))
    water = None
    flat = elev
    if cfg.water:
        progress("water", "detecting water bodies (Natural Earth seeds, DEM-snapped)", 0.25)
        water = detect_water(grid, elev, lambda m: progress("water", m, None))
        flat = flatten(elev, water)
        progress("water", f"{len(water.bodies)} water bodies", 0.3)
    rivers = None
    if cfg.rivers.enabled:
        progress("rivers", "fetching OpenStreetMap waterways", 0.31)
        rivers = load_rivers(cfg, grid, lambda m: progress("rivers", m, None))
    urban = None
    if cfg.urban.enabled:
        progress("urban", "fetching OpenStreetMap built-up areas and roads", 0.33)
        urban = load_urban(cfg, grid, lambda m: progress("urban", m, None))
    return Terrain(grid, elev, flat, water, info, rivers, urban)


def choose_exaggeration(cfg: ProjectConfig, terrain: Terrain) -> tuple[float, float]:
    """(exaggeration to use, suggested exaggeration)."""
    suggested = suggest_exaggeration(cfg, terrain.relief_m)
    if cfg.exaggeration is None:
        return suggested, suggested
    return cap_exaggeration(cfg, terrain.relief_m, float(cfg.exaggeration)), suggested


def analyze(cfg: ProjectConfig, terrain: Terrain) -> Classification:
    return classify(cfg, terrain.grid, terrain.elev, terrain.water, terrain.rivers, terrain.urban)


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def preview_payload(cfg: ProjectConfig, terrain: Terrain, cls: Classification) -> dict:
    """Everything the browser needs to draw the colored heightmap itself.

    The elevation grid and per-cell band classes are exaggeration-independent,
    so the client can re-scale Z live as the slider moves.
    """
    exaggeration, suggested = choose_exaggeration(cfg, terrain)
    ny, nx = terrain.elev.shape
    return {
        "version": __version__,
        "shape": [ny, nx],
        "tiles": [cfg.tiles_x, cfg.tiles_y],
        "nodes_per_tile": terrain.grid.nodes_per_tile,
        "tile_mm": cfg.tile_mm,
        "base_mm": cfg.base_mm,
        "skin_mm": cfg.palette.skin_depth_mm,
        "river_depth_mm": cfg.rivers.depth_mm,
        "mm_per_m": cfg.mm_per_m,
        "z_datum_m": float(terrain.elev.min()),
        "elevation_min_m": float(terrain.elev.min()),
        "elevation_max_m": float(terrain.elev.max()),
        "relief_m": terrain.relief_m,
        "exaggeration": exaggeration,
        "suggested_exaggeration": suggested,
        "max_exaggeration": cap_exaggeration(cfg, terrain.relief_m, 1e9),
        "max_height_mm": cfg.max_height_mm,
        "elev_b64": _b64(terrain.elev.astype(np.float32)),
        "cells_b64": _b64(cls.cells.astype(np.uint8)),
        "classification": cls.summary(),
        "water": None if terrain.water is None else {
            "bodies": [b.__dict__ for b in terrain.water.bodies],
            "reference_level_m": terrain.water.reference_level_m,
        },
        "rivers": None if terrain.rivers is None else terrain.rivers.summary(),
        "urban": None if terrain.urban is None else terrain.urban.summary(),
        "dem": terrain.dem_info,
        "crs": terrain.grid.crs.to_string(),
        "bbox_lonlat": terrain.grid.bbox_lonlat(0.0),
    }


def build_tiles(cfg: ProjectConfig, terrain: Terrain, cls: Classification, progress: Progress = _noop) -> list[TileBuild]:
    """Build every tile; if a tile needs more simultaneous colors than slots, merge and retry."""
    exaggeration, _ = choose_exaggeration(cfg, terrain)
    z_datum = float(terrain.elev.min())
    while True:
        tiles: list[TileBuild] = []
        infeasible = False
        total = cfg.tiles_x * cfg.tiles_y
        for k, (ty, tx) in enumerate((ty, tx) for ty in range(cfg.tiles_y) for tx in range(cfg.tiles_x)):
            progress("mesh", f"meshing tile x{tx} y{ty} ({k + 1}/{total})", 0.4 + 0.4 * k / total)
            tb = build_tile(cfg, terrain.grid, cls, tx, ty, exaggeration, z_datum)
            tiles.append(tb)
            if not tb.plan.feasible:
                infeasible = True
                break
        if not infeasible:
            return tiles
        m = cls.merge_least_distinct("more simultaneous colors than slots (swap strategy could not fit)")
        if m is None:
            return tiles
        progress("plan", f"merged {m['from']} into {m['into']} to fit {cfg.palette.max_slots} slots", None)


def _plan_text(cfg: ProjectConfig, tiles: list[TileBuild], cls: Classification) -> str:
    lines = [f"my3dmaps print plan for '{cfg.name}'", "=" * 60, "",
             f"Scale 1:{cfg.scale:.0f}, {cfg.tiles_x}x{cfg.tiles_y} tiles of {cfg.tile_mm:.0f} mm "
             f"({cfg.tile_m / 1000:.1f} km per tile edge), height exaggeration {tiles[0].exaggeration:.2f}x" if tiles else "",
             f"Elevation {cls.elev.min():.0f}-{cls.elev.max():.0f} m, model height up to "
             f"{max(t.height_mm for t in tiles):.1f} mm" if tiles else "", ""]
    if cls.pruned:
        lines.append("Bands dropped for this region: " + ", ".join(f"{p['id']} ({p['reason']})" for p in cls.pruned))
    if cls.merges:
        lines.append("Bands merged: " + ", ".join(f"{m['from']} -> {m['into']}" for m in cls.merges))
    if cls.rivers_info:
        ri = cls.rivers_info
        if ri.get("error"):
            lines.append(f"Rivers: NOT included ({ri['error']})")
        else:
            lines.append(f"Rivers: {ri['features']} OpenStreetMap features ({ri['length_km']} km of centreline), "
                         f"painted {cfg.rivers.depth_mm} mm deep, >= {cfg.rivers.min_width_mm} mm wide. "
                         "Data (c) OpenStreetMap contributors, ODbL.")
    if cls.urban_info:
        ui = cls.urban_info
        if ui.get("error"):
            lines.append(f"Urban layer: NOT included ({ui['error']})")
        elif cls.urban_index is None:
            lines.append("Urban layer: enabled but dropped (" + next((p["reason"] for p in cls.pruned if p["id"] == "urban"),
                                                                     "merged away to fit the slots") + ")")
        else:
            lines.append(f"Urban layer: {ui['features']} OpenStreetMap features ({ui['built_up_km2']} km2 built-up, "
                         f"{ui['road_km']} km of major roads) painted {cfg.urban.color}, {cfg.urban.depth_mm} mm deep. "
                         "Data (c) OpenStreetMap contributors, ODbL.")
    if cfg.shrinkage_pct:
        lines.append(f"Shrinkage compensation {cfg.shrinkage_pct}%: tiles are modelled "
                     f"{cfg.tile_mm * (1 + cfg.shrinkage_pct / 100):.2f} mm square so they cool to {cfg.tile_mm:.0f} mm; "
                     "make sure the slicer filament profile is NOT also compensating.")
    lines.append("")
    for t in tiles:
        lines.append(f"--- {t.name} (height {t.height_mm:.1f} mm) ---")
        for o in t.objects:
            lines.append(f"  slot {o.extruder}: {o.name:24s} {o.color}  z {min(r.z_min for r in t.ranges if r.color == o.color):6.1f}"
                         f" - {max(r.z_max for r in t.ranges if r.color == o.color):6.1f} mm")
        if t.plan.swaps:
            for s in t.plan.swaps:
                lines.append(f"  MANUAL SWAP at z={s.z_mm:.1f} mm: slot {s.slot} {s.from_color} -> {s.to_color} "
                             f"({'+'.join(s.from_labels)} -> {'+'.join(s.to_labels)})")
        else:
            lines.append("  no manual filament swaps needed")
        lines.append("")
    return "\n".join(lines) + "\n"


def build_project(cfg: ProjectConfig, out_dir: str | Path, progress: Progress = _noop, do_slice: bool = False,
                  write_glb_preview: bool = True) -> dict:
    t0 = time.time()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    problems = cfg.validate()
    if problems:
        raise ValueError("; ".join(problems))
    terrain = prepare_terrain(cfg, progress)
    progress("classify", "classifying terrain into bands", 0.35)
    cls = analyze(cfg, terrain)
    tiles = build_tiles(cfg, terrain, cls, progress)
    exaggeration = tiles[0].exaggeration if tiles else 1.0

    tile_summaries = []
    for k, t in enumerate(tiles):
        progress("export", f"writing {t.name}", 0.8 + 0.15 * k / max(len(tiles), 1))
        tdir = out / t.name
        tdir.mkdir(exist_ok=True)
        stls: list[SliceInput] = []
        for o in t.objects:
            p = tdir / f"{o.name}.stl"
            write_stl(p, o.mesh, o.name)
            stls.append(SliceInput(p, o.extruder))
        pauses = [CustomGCodeItem(s.z_mm, 1, s.slot, s.to_color, f"swap {'+'.join(s.from_labels)} -> {'+'.join(s.to_labels)}")
                  for s in t.plan.swaps]
        write_3mf(tdir / f"{t.name}.3mf", t.objects, f"{cfg.name} {t.name}", pauses,
                  {"my3dmaps:scale": f"1:{cfg.scale:.0f}", "my3dmaps:exaggeration": f"{exaggeration:.3f}",
                   "my3dmaps:tile": f"{t.tx},{t.ty}"})
        if write_glb_preview:
            write_glb(tdir / "preview.glb", t.objects)
        (tdir / "plan.json").write_text(json.dumps(t.summary(), indent=2))
        summ = t.summary()
        summ["files"] = {"3mf": str(tdir / f"{t.name}.3mf"), "stl": [str(s.stl_path) for s in stls]}
        if do_slice:
            progress("slice", f"slicing {t.name}", None)
            res = slice_tile(cfg.slicer, stls, tdir / "sliced", t.name, t.plan.swaps)
            summ["slice"] = {"ok": res.ok, "message": res.message, "gcode": str(res.gcode_path) if res.gcode_path else None,
                             "log": str(res.log_path), "swaps_inserted": res.swaps_inserted, "command": res.command}
        tile_summaries.append(summ)

    summary = {
        "name": cfg.name, "version": __version__, "seconds": round(time.time() - t0, 1),
        "exaggeration": exaggeration, "relief_m": terrain.relief_m,
        "elevation_min_m": float(terrain.elev.min()), "elevation_max_m": float(terrain.elev.max()),
        "classification": cls.summary(), "dem": terrain.dem_info,
        "water": None if terrain.water is None else [b.__dict__ for b in terrain.water.bodies],
        "rivers": None if terrain.rivers is None else terrain.rivers.summary(),
        "urban": None if terrain.urban is None else terrain.urban.summary(),
        "tiles": tile_summaries, "slicer_found": find_executable(cfg.slicer),
        "out_dir": str(out),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    (out / "PRINT_PLAN.txt").write_text(_plan_text(cfg, tiles, cls))
    used = ProjectConfig.from_dict(cfg.to_dict())
    used.exaggeration = exaggeration
    used.save(out / "project.json")
    progress("done", f"finished in {summary['seconds']} s", 1.0)
    return summary
