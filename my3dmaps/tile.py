"""Assemble one printable tile from the project classification.

A tile's solid is decomposed into:

* the **base plate** (constant thickness, plain square: tiles are butt-glued),
* one **slab** per elevation band: the terrain volume between the band's
  lower and upper threshold planes, minus the skin,
* one **skin** per painted band: the top ``skin_depth_mm`` of the surface over
  the cells classified as that band (only ``rivers.depth_mm`` over river cells).

Sheets of the same color are concatenated into one object, so a print with
N distinct colors has N objects.  Objects are scaled in XY by the configured
shrinkage compensation and positioned so the tile sits in the middle of the
256 mm bed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .classify import Classification
from .config import BED_MM, ProjectConfig
from .geo import Grid
from .mesh import Mesh, MeshObject, concat, sheet_mesh
from .plan import ColorRange, SlotPlan, plan_slots


@dataclass
class TileBuild:
    tx: int
    ty: int
    objects: list[MeshObject]
    plan: SlotPlan
    height_mm: float
    z_datum_m: float
    exaggeration: float
    ranges: list[ColorRange] = field(default_factory=list)
    footprint_mm: float = 0.0  # printed footprint after shrinkage compensation

    @property
    def name(self) -> str:
        return f"tile_x{self.tx}_y{self.ty}"

    def summary(self) -> dict:
        return {
            "name": self.name, "tx": self.tx, "ty": self.ty,
            "height_mm": round(self.height_mm, 2), "exaggeration": self.exaggeration,
            "objects": [{"name": o.name, "color": o.color, "extruder": o.extruder, "bands": o.band_ids,
                         "triangles": int(len(o.mesh.faces))} for o in self.objects],
            "plan": self.plan.to_dict(),
            "footprint_mm": round(self.footprint_mm, 3),
        }


def z_of(elev_m, cfg: ProjectConfig, z_datum_m: float, exaggeration: float):
    """Model height (mm) of a real elevation: base + skin + exaggerated relief."""
    return cfg.base_mm + cfg.palette.skin_depth_mm + (np.asarray(elev_m) - z_datum_m) * cfg.mm_per_m * exaggeration


def base_plate(cfg: ProjectConfig) -> Mesh:
    """Plain square base plate, ``base_mm`` thick (tiles are glued edge to edge)."""
    xs = np.array([0.0, cfg.tile_mm])
    ys = np.array([0.0, cfg.tile_mm])
    top = np.full((2, 2), cfg.base_mm)
    bot = np.zeros_like(top)
    return sheet_mesh(xs, ys, top, bot, np.ones((1, 1), dtype=bool))


def skin_depth_nodes(skin_depth_mm: float, thin: list[tuple[np.ndarray | None, float]], shape: tuple[int, int]) -> np.ndarray:
    """Per-node skin thickness: ``skin_depth_mm`` everywhere, thinner at nodes
    touching a cell of a thin overlay (rivers, urban), so the overlay skin is
    thin and the slab below rises to meet it; neighbouring land skin tapers."""
    d = np.full(shape, skin_depth_mm, dtype=np.float64)
    for mask, depth in sorted(thin, key=lambda t: -t[1]):  # thinnest wins where overlays touch
        if mask is None or not mask.any() or depth >= skin_depth_mm:
            continue
        touch = np.zeros(shape, dtype=bool)
        touch[:-1, :-1] |= mask
        touch[:-1, 1:] |= mask
        touch[1:, :-1] |= mask
        touch[1:, 1:] |= mask
        d[touch] = depth
    return d


def build_tile(cfg: ProjectConfig, grid: Grid, cls: Classification, tx: int, ty: int,
               exaggeration: float, z_datum_m: float, center_on_bed: bool = True) -> TileBuild:
    js, is_ = grid.tile_slices(tx, ty)
    e = cls.elev[js, is_]
    cells_cls = cls.cells[js.start:js.stop - 1, is_.start:is_.stop - 1]
    n = e.shape[0]
    xs = np.linspace(0.0, cfg.tile_mm, n)
    ys = np.linspace(0.0, cfg.tile_mm, n)
    sub = (slice(js.start, js.stop - 1), slice(is_.start, is_.stop - 1))
    thin = [(None if cls.river_cells is None else cls.river_cells[sub], cfg.rivers.depth_mm),
            (None if cls.urban_cells is None else cls.urban_cells[sub], cfg.urban.depth_mm)]
    d = skin_depth_nodes(cfg.palette.skin_depth_mm, thin, e.shape)
    ez = z_of(e, cfg, z_datum_m, exaggeration)
    ez_sub = ez - d  # top of the slabs (per node, so every sheet shares the same interface)

    sheets: dict[str, list[tuple[Mesh, str]]] = {}  # color -> [(mesh, band id)]

    def add(color: str, band_id: str, m: Mesh):
        if not m.empty:
            sheets.setdefault(color, []).append((m, band_id))

    # slabs between horizontal threshold planes
    idx = cls.elev_band_indices
    thr = []
    for k, bi in enumerate(idx):
        lower = cls.bands[bi].lower_m
        t = cfg.base_mm if not math.isfinite(lower) else float(z_of(lower, cfg, z_datum_m, exaggeration))
        thr.append(max(t, cfg.base_mm))
    thr_upper = thr[1:] + [math.inf]
    for k, bi in enumerate(idx):
        lo, hi = thr[k], thr_upper[k]
        top = np.clip(ez_sub, lo, hi)
        bot = np.full_like(top, lo)
        cell_has = (np.maximum.reduce([top[:-1, :-1], top[:-1, 1:], top[1:, :-1], top[1:, 1:]]) > lo + 0.02)
        add(cls.bands[bi].color, cls.bands[bi].id, sheet_mesh(xs, ys, top, bot, cell_has))

    # surface skins
    for bi, band in enumerate(cls.bands):
        mask = cells_cls == bi
        if mask.any():
            add(band.color, band.id, sheet_mesh(xs, ys, ez, ez_sub, mask))

    # base plate
    add(cls.base_color, "base", base_plate(cfg))

    # merge same-color sheets into objects, ordered bottom-up by their lowest point
    objects: list[MeshObject] = []
    ranges: list[ColorRange] = []
    for color, items in sheets.items():
        m = concat([mm for mm, _ in items])
        ids = []
        for _, bid in items:
            if bid not in ids:
                ids.append(bid)
        lo, hi = m.bounds()
        objects.append(MeshObject("+".join(ids), color, m, 1, ids))
        ranges.append(ColorRange(color, ids, float(lo[2]), float(hi[2])))
    order = sorted(range(len(objects)), key=lambda i: (ranges[i].z_min, ranges[i].z_max))
    objects = [objects[i] for i in order]
    ranges = [ranges[i] for i in order]

    plan = plan_slots(ranges, cfg.palette.max_slots)
    for o in objects:
        o.extruder = plan.slots.get(o.color, 0) or 1

    # shrinkage compensation: scale XY about the tile centre so the cooled part measures tile_mm
    f = 1.0 + cfg.shrinkage_pct / 100.0
    if abs(f - 1.0) > 1e-9:
        c = cfg.tile_mm / 2.0
        for o in objects:
            v = o.mesh.vertices.copy()
            v[:, 0] = c + (v[:, 0] - c) * f
            v[:, 1] = c + (v[:, 1] - c) * f
            o.mesh = Mesh(v, o.mesh.faces)

    if center_on_bed:
        off = (BED_MM - cfg.tile_mm) / 2.0
        for o in objects:
            o.mesh = o.mesh.translated((off, off, 0.0))

    return TileBuild(tx, ty, objects, plan, float(ez.max()), z_datum_m, exaggeration, ranges, cfg.tile_mm * f)
