"""Terrain classification into color bands.

Two related things are produced from the (water-flattened) elevation grid:

* ``slab`` - per node, the *elevation band* the node's surface falls in.  The
  bands' lower thresholds are horizontal cuts through the model, so the solid
  under the surface is split into strictly elevation-ordered slabs.  This is
  what gives the "each spool loaded once, bottom to top" property.
* ``cells`` - per grid cell, the band painted on the *surface skin*.  Usually
  identical to the slab band, but rules that are not pure elevation (slope
  -> rock, distance-to-water -> sand, water surfaces) act here.

Bands that do not occur in the region are pruned, and when more colors remain
than physical slots the two least visually distinct adjacent bands are merged
(or the caller plans a manual filament swap instead - see plan.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import ndimage

from .colors import color_distance, normalize_hex
from .config import BandConfig, PaletteConfig, ProjectConfig, resolve_color
from .geo import Grid
from .rivers import RiverFeatures, rasterize_rivers
from .urban import UrbanFeatures, rasterize_urban
from .water import WaterResult


@dataclass
class ActiveBand:
    id: str
    label: str
    color: str
    kind: str  # "water" | "urban" | "elevation"
    cfg: BandConfig
    lower_m: float = -math.inf  # elevation bands only
    merged_ids: list[str] = field(default_factory=list)

    @property
    def is_water(self) -> bool:
        return self.kind == "water"

    @property
    def is_surface(self) -> bool:
        """Skin-only band (water, urban): no elevation slab of its own."""
        return self.kind != "elevation"

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "color": self.color, "kind": self.kind,
                "lower_m": None if not math.isfinite(self.lower_m) else self.lower_m,
                "merged": list(self.merged_ids)}


@dataclass
class Classification:
    bands: list[ActiveBand]  # water (if any) first, then elevation bands ascending
    elev: np.ndarray  # flattened node elevations, metres [ny, nx]
    cells: np.ndarray  # uint8 [ny-1, nx-1] index into bands (surface skin class)
    slope_deg: np.ndarray  # float32 [ny-1, nx-1]
    water_mask: np.ndarray  # bool nodes
    reference_water_m: Optional[float]
    base_color: str
    pruned: list[dict] = field(default_factory=list)
    merges: list[dict] = field(default_factory=list)
    river_cells: Optional[np.ndarray] = None  # bool [ny-1, nx-1]: cells painted as river (thin water skin)
    rivers_info: Optional[dict] = None
    urban_cells: Optional[np.ndarray] = None  # bool [ny-1, nx-1]: cells painted urban (thin grey skin)
    urban_info: Optional[dict] = None

    # ---- elevation-band helpers -------------------------------------------
    @property
    def water_index(self) -> Optional[int]:
        for i, b in enumerate(self.bands):
            if b.is_water:
                return i
        return None

    @property
    def urban_index(self) -> Optional[int]:
        for i, b in enumerate(self.bands):
            if b.kind == "urban":
                return i
        return None

    @property
    def elev_band_indices(self) -> list[int]:
        return [i for i, b in enumerate(self.bands) if not b.is_surface]

    def lowest_elevation_color(self) -> str:
        idx = self.elev_band_indices
        return self.bands[idx[0]].color if idx else self.bands[0].color

    def cell_elev(self) -> np.ndarray:
        e = self.elev
        return 0.25 * (e[:-1, :-1] + e[:-1, 1:] + e[1:, :-1] + e[1:, 1:])

    @property
    def thresholds_m(self) -> list[float]:
        """Lower thresholds of the elevation bands, ascending (first is -inf)."""
        return [self.bands[i].lower_m for i in self.elev_band_indices]

    def slab_index(self, elev: np.ndarray) -> np.ndarray:
        """Index (into ``bands``) of the elevation band containing each elevation."""
        idx = self.elev_band_indices
        thr = np.array(self.thresholds_m[1:], dtype=np.float64)
        k = np.searchsorted(thr, elev, side="right")
        return np.asarray(idx, dtype=np.int16)[k]

    def distinct_colors(self) -> list[str]:
        cols = [self.base_color] + [b.color for b in self.bands]
        seen: list[str] = []
        for c in cols:
            if c not in seen:
                seen.append(c)
        return seen

    def fractions(self) -> dict[str, float]:
        tot = self.cells.size
        return {b.id: float((self.cells == i).sum()) / tot for i, b in enumerate(self.bands)}

    def summary(self) -> dict:
        fr = self.fractions()
        return {
            "bands": [dict(b.to_dict(), fraction=fr[b.id]) for b in self.bands],
            "base_color": self.base_color,
            "distinct_colors": self.distinct_colors(),
            "pruned": self.pruned,
            "merges": self.merges,
            "reference_water_m": self.reference_water_m,
            "rivers": self.rivers_info,
            "river_fraction": None if self.river_cells is None else float(self.river_cells.mean()),
            "urban": self.urban_info,
            "urban_fraction": None if self.urban_cells is None else float(self.urban_cells.mean()),
            "elevation_min_m": float(self.elev.min()),
            "elevation_max_m": float(self.elev.max()),
        }

    # ---- merging -----------------------------------------------------------
    def merge_into(self, upper: int, lower: int, reason: str) -> None:
        """Merge band ``upper`` into band ``lower`` (both indices into ``bands``)."""
        up, lo = self.bands[upper], self.bands[lower]
        if up.kind == "urban":
            # dropping the urban overlay: its cells go back to the terrain class underneath
            sel = self.cells == upper
            self.cells[sel] = self.slab_index(self.cell_elev())[sel]
            self.merges.append({"from": up.id, "into": "terrain", "reason": reason})
            del self.bands[upper]
            self.cells[self.cells > upper] -= 1
            self.urban_cells = None
            return
        fr = self.fractions()
        # the merged band keeps the identity/color of whichever band covers more area,
        # but always the *lower* elevation threshold (the slab range is the union)
        keep_upper_look = (fr[up.id] > fr[lo.id]) and not lo.is_surface
        self.cells[self.cells == upper] = lower
        merged = [up.id] + up.merged_ids
        if keep_upper_look:
            merged = [lo.id] + lo.merged_ids
            lo.id, lo.label, lo.color, lo.merged_ids = up.id, up.label, up.color, up.merged_ids
            lo.cfg = up.cfg
        lo.merged_ids.extend(merged)
        self.merges.append({"from": merged[0], "into": lo.id, "reason": reason})
        del self.bands[upper]
        self.cells[self.cells > upper] -= 1

    def least_distinct_pair(self) -> Optional[tuple[int, int]]:
        """(upper, lower) adjacent pair with the smallest visual-distinctness score."""
        fr = self.fractions()
        order = [i for i, b in enumerate(self.bands)]  # bands are already ordered water, then ascending
        best = None
        for a, b in zip(order[:-1], order[1:]):
            ba, bb = self.bands[a], self.bands[b]
            de = color_distance(ba.color, bb.color)
            score = de * math.sqrt(min(fr[ba.id], fr[bb.id]) + 0.01)
            if ba.is_water and bb.kind != "urban":
                score *= 3.0  # merging water into land is rarely what anyone wants
            if bb.kind == "urban":
                score *= 0.5  # the optional urban overlay is the first thing to give up
            if best is None or score < best[0]:
                best = (score, b, a)
        return None if best is None else (best[1], best[2])

    def merge_least_distinct(self, reason: str = "more colors than slots") -> Optional[dict]:
        pair = self.least_distinct_pair()
        if pair is None:
            return None
        upper, lower = pair
        self.merge_into(upper, lower, reason)
        return self.merges[-1]


# ---------------------------------------------------------------------------

def cell_slope_deg(elev: np.ndarray, pitch_m: float) -> np.ndarray:
    e00 = elev[:-1, :-1]
    e01 = elev[:-1, 1:]
    e10 = elev[1:, :-1]
    e11 = elev[1:, 1:]
    dzdx = ((e01 + e11) - (e00 + e10)) / (2 * pitch_m)
    dzdy = ((e10 + e11) - (e00 + e01)) / (2 * pitch_m)
    return np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)


def classify(cfg: ProjectConfig, grid: Grid, elev: np.ndarray, water: Optional[WaterResult],
             rivers: Optional[RiverFeatures] = None, urban: Optional[UrbanFeatures] = None) -> Classification:
    pal: PaletteConfig = cfg.palette
    ny, nx = elev.shape
    has_water = water is not None and water.any
    river_cells = None
    if cfg.rivers.enabled and rivers is not None and rivers.count:
        river_cells = rasterize_rivers(rivers, grid, cfg)
        if not river_cells.any():
            river_cells = None
    has_rivers = river_cells is not None
    urban_cells = None
    if cfg.urban.enabled and urban is not None and urban.count:
        urban_cells = rasterize_urban(urban, grid, cfg)
        if not urban_cells.any():
            urban_cells = None
    has_urban = urban_cells is not None
    water_mask = water.mask if has_water else np.zeros_like(elev, dtype=bool)
    ref_water = water.reference_level_m if has_water else None
    pruned: list[dict] = []

    # 1. resolve elevation-band thresholds
    water_cfg = next((b for b in pal.bands if b.is_water), None)
    elev_bands: list[ActiveBand] = []
    for b in pal.bands:
        if b.is_water:
            continue
        if b.requires_water and not has_water:
            pruned.append({"id": b.id, "reason": "region has no water"})
            continue
        if b.from_m is not None:
            lower = float(b.from_m)
        elif b.from_water_m is not None:
            lower = float((ref_water if ref_water is not None else float(elev.min())) + b.from_water_m)
        else:
            lower = -math.inf
        elev_bands.append(ActiveBand(b.id, b.label or b.id, normalize_hex(b.color), "elevation", b, lower))
    if not elev_bands:
        raise ValueError("palette needs at least one elevation band")
    elev_bands.sort(key=lambda a: a.lower_m)
    elev_bands[0].lower_m = -math.inf
    emax = float(elev.max())
    kept = []
    for a in elev_bands:
        if a.lower_m >= emax:
            pruned.append({"id": a.id, "reason": f"nothing above {a.lower_m:.0f} m (region max {emax:.0f} m)"})
        else:
            kept.append(a)
    elev_bands = kept

    bands: list[ActiveBand] = []
    if (has_water or has_rivers) and water_cfg is not None:
        bands.append(ActiveBand(water_cfg.id, water_cfg.label or "Water", normalize_hex(water_cfg.color),
                                "water", water_cfg))
    elif water_cfg is not None:
        pruned.append({"id": water_cfg.id, "reason": "region has no water"})
    if has_urban:
        ucfg = BandConfig("urban", cfg.urban.color, kind="urban", label=cfg.urban.label)
        bands.append(ActiveBand("urban", cfg.urban.label, normalize_hex(cfg.urban.color), "urban", ucfg))
    elif cfg.urban.enabled:
        pruned.append({"id": "urban", "reason": "no built-up areas or major roads here"
                       if urban is None or not urban.error else f"urban data unavailable: {urban.error}"})
    bands.extend(elev_bands)

    base_color = normalize_hex(resolve_color(pal.base_color if pal.base_color != "none" else "blue",
                                             bands[0].color if bands and bands[0].is_water else None))
    if pal.base_color == "none":
        base_color = elev_bands[0].color  # base becomes part of the lowest elevation object

    cls = Classification(bands, elev, np.zeros((ny - 1, nx - 1), dtype=np.uint8),
                         cell_slope_deg(elev, grid.pitch_m), water_mask, ref_water, base_color, pruned)
    cls.rivers_info = rivers.summary() if rivers is not None else None
    cls.urban_info = urban.summary() if urban is not None else None

    # 2. skin classification per cell
    cell_elev = 0.25 * (elev[:-1, :-1] + elev[:-1, 1:] + elev[1:, :-1] + elev[1:, 1:])
    cells = cls.slab_index(cell_elev).astype(np.int16)

    # slope rule (promote upward only)
    for i, a in enumerate(bands):
        if a.is_water or a.cfg.slope_deg is None:
            continue
        floor = a.cfg.slope_from_m if a.cfg.slope_from_m is not None else a.lower_m
        hit = (cls.slope_deg >= a.cfg.slope_deg) & (cell_elev >= floor) & (cells < i)
        cells[hit] = i

    # water surfaces
    wcount = (water_mask[:-1, :-1].astype(np.int8) + water_mask[:-1, 1:] + water_mask[1:, :-1] + water_mask[1:, 1:])
    water_cells = wcount >= 4  # all four corners: the blue skin must never climb a shore
    wi = cls.water_index
    if wi is not None:
        cells[water_cells] = wi

    # distance-to-water rule (sand only near shores)
    if water_cells.any():
        dist = ndimage.distance_transform_edt(~water_cells) * grid.pitch_m
    else:
        dist = np.full(cells.shape, np.inf)
    for i, a in enumerate(bands):
        if a.is_water or a.cfg.max_distance_m is None:
            continue
        far = (cells == i) & (dist > a.cfg.max_distance_m)
        if far.any():
            # repaint with the next elevation band up (or, at the top, the one below) - never a surface band
            above = [k for k in cls.elev_band_indices if k > i]
            below = [k for k in cls.elev_band_indices if k < i]
            target = above[0] if above else (below[-1] if below else None)
            if target is not None:
                cells[far] = target

    # urban overlay (never on lakes/sea); rivers are painted afterwards so they cross towns
    ui = cls.urban_index
    if has_urban and ui is not None:
        urban_cells = urban_cells & ~water_cells
        cells[urban_cells] = ui
        cls.urban_cells = urban_cells

    # rivers: thin blue skin along the surface (lakes/sea already blue; they don't feed the shore rule)
    if has_rivers and wi is not None:
        river_cells = river_cells & ~water_cells
        cells[river_cells] = wi
        cls.river_cells = river_cells
        if cls.urban_cells is not None:
            cls.urban_cells = cls.urban_cells & ~river_cells

    cls.cells = cells.astype(np.uint8)

    # 3. prune bands with negligible area (skin fraction), merging into the band below
    changed = True
    while changed:
        changed = False
        fr = cls.fractions()
        for i, a in enumerate(cls.bands):
            if fr[a.id] < pal.min_fraction and len(cls.bands) > 1:
                if a.is_water and cls.river_cells is not None:
                    continue  # rivers are thin by nature; never prune the water colour under them
                if a.is_surface:
                    target = next((k for k, b in enumerate(cls.bands) if not b.is_surface), None)
                else:
                    target = i - 1 if i - 1 >= 0 and not cls.bands[i - 1].is_surface else i + 1
                if target is None or target >= len(cls.bands):
                    continue
                cls.pruned.append({"id": a.id, "reason": f"covers only {fr[a.id] * 100:.2f}% of the area"})
                cls.merge_into(i, target, "pruned: negligible area")
                cls.merges.pop()  # recorded as pruned, not as a merge
                changed = True
                break
    if cls.elev_band_indices:
        cls.bands[cls.elev_band_indices[0]].lower_m = -math.inf
    if pal.base_color == "none":
        cls.base_color = cls.lowest_elevation_color()

    # 4. overflow: more colors than slots -> merge least distinct adjacent pair
    if pal.overflow == "merge":
        while len(cls.distinct_colors()) > pal.max_slots:
            if cls.merge_least_distinct() is None:
                break
            if pal.base_color == "none":
                cls.base_color = cls.lowest_elevation_color()

    # 5. single-color draft print: keep the band structure (for the report) but one filament
    if cfg.monochrome:
        mono = normalize_hex(cfg.monochrome_color)
        for b in cls.bands:
            b.color = mono
        cls.base_color = mono
    return cls


def suggest_exaggeration(cfg: ProjectConfig, relief_m: float, target_relief_mm: float = 45.0) -> float:
    """Pick a starting height multiplier from measured relief.

    Aims for ~45 mm of relief on a 200 mm tile (flat regions get more,
    mountains less), clamped to 1-8x and to the printable height budget.
    """
    if relief_m <= 0:
        return 1.0
    natural = relief_m * cfg.mm_per_m * (cfg.tile_mm / 200.0)
    ex = target_relief_mm / max(natural, 1e-6)
    ex = max(1.0, min(8.0, ex))
    return cap_exaggeration(cfg, relief_m, round(ex * 4) / 4)


def cap_exaggeration(cfg: ProjectConfig, relief_m: float, exaggeration: float) -> float:
    budget = cfg.max_height_mm - cfg.base_mm - cfg.palette.skin_depth_mm
    natural = relief_m * cfg.mm_per_m
    if natural <= 0:
        return exaggeration
    return float(min(exaggeration, budget / natural))
