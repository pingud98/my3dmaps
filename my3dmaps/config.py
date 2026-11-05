"""Project / palette configuration.

Everything a build needs is captured in a ProjectConfig (JSON on disk, see
``presets/``).  Palettes are config, not code: bands, thresholds, colors and
the >4-color overflow strategy all live here so Geneva and Liverpool can use
different numbers without touching the pipeline.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

TILE_MM_DEFAULT = 245.0  # 2 x 2 tiles fill a 50 x 50 cm frame with a 5 mm border
BED_MM = 256.0
# Elegoo Centauri Carbon 2 no-print zone, from OrcaSlicer's official profile
# (resources/profiles/Elegoo/machine/ECC2, "bed_exclude_area"): x 246-256, y 0-20.
BED_EXCLUDE = [(246.0, 0.0, 256.0, 20.0)]
BED_CLEARANCE_MM = 0.5  # gap kept from the bed edge and the no-print zone


def _clear_of_exclusions(x: float, y: float, w: float, h: float) -> bool:
    return all(x + w <= ex0 - BED_CLEARANCE_MM or x >= ex1 + BED_CLEARANCE_MM
               or y + h <= ey0 - BED_CLEARANCE_MM or y >= ey1 + BED_CLEARANCE_MM
               for ex0, ey0, ex1, ey1 in BED_EXCLUDE)


def _placements(w: float, h: float) -> list[tuple[float, float]]:
    """Candidate lower-left corners for a w x h footprint: bed edges, centre, and
    positions touching each no-print zone (0.5 mm clearance kept everywhere)."""
    out = []
    axes = []
    for size in (w, h):
        lo, hi = BED_CLEARANCE_MM, BED_MM - BED_CLEARANCE_MM - size
        if hi < lo - 1e-9:
            return []
        c = {lo, hi, (BED_MM - size) / 2.0}
        for ex0, ey0, ex1, ey1 in BED_EXCLUDE:
            a0, a1 = (ex0, ex1) if not axes else (ey0, ey1)
            c |= {a0 - BED_CLEARANCE_MM - size, a1 + BED_CLEARANCE_MM}
        axes.append(sorted(v for v in c if lo - 1e-9 <= v <= hi + 1e-9))
    for x in axes[0]:
        for y in axes[1]:
            if _clear_of_exclusions(x, y, w, h):
                out.append((x, y))
    return out


def tower_strip(x: float, y: float, w: float, h: float) -> tuple[float, tuple[float, float, float, float]]:
    """Widest free rectangle beside a placed footprint for a prime tower:
    (width, (x0, y0, x1, y1)).  Strips run the full bed on the free side, minus
    the no-print zones they would overlap (trimmed in the long direction)."""
    gap = 2.0  # keep the tower off the tile's brim
    lim0, lim1 = BED_CLEARANCE_MM, BED_MM - BED_CLEARANCE_MM
    strips = [(lim0, lim0, x - gap, lim1), (x + w + gap, lim0, lim1, lim1),
              (lim0, lim0, lim1, y - gap), (lim0, y + h + gap, lim1, lim1)]
    best = (0.0, (0.0, 0.0, 0.0, 0.0))
    for k, (x0, y0, x1, y1) in enumerate(strips):
        vertical = k < 2
        for ex0, ey0, ex1, ey1 in BED_EXCLUDE:
            if x1 <= ex0 or x0 >= ex1 or y1 <= ey0 or y0 >= ey1:
                continue
            if vertical:  # trim the strip's length (y) away from the zone
                if ey0 - y0 > y1 - ey1:
                    y1 = min(y1, ey0 - BED_CLEARANCE_MM)
                else:
                    y0 = max(y0, ey1 + BED_CLEARANCE_MM)
            else:
                if ex0 - x0 > x1 - ex1:
                    x1 = min(x1, ex0 - BED_CLEARANCE_MM)
                else:
                    x0 = max(x0, ex1 + BED_CLEARANCE_MM)
        width = (x1 - x0) if vertical else (y1 - y0)
        length = (y1 - y0) if vertical else (x1 - x0)
        if length > 60.0 and width > best[0]:
            best = (width, (x0, y0, x1, y1))
    return best


def place_on_bed(w: float, h: float | None = None, tower_room_mm: float = 0.0) -> tuple[float, float] | None:
    """(x, y) of a w x h footprint's lower-left corner on the bed.

    Without a tower: centred when that clears the no-print zones, otherwise
    shifted the least distance that does.  With ``tower_room_mm`` > 0: the
    position (among the same candidates) that leaves a free strip at least that
    wide for the prime tower, preferring the widest strip, then the most central.
    Falls back to the no-tower placement when no position leaves enough room.
    None if the footprint cannot fit at all."""
    h = w if h is None else h
    cands = _placements(w, h)
    if not cands:
        return None
    cx, cy = (BED_MM - w) / 2.0, (BED_MM - h) / 2.0
    dist = lambda p: abs(p[0] - cx) + abs(p[1] - cy)  # noqa: E731
    if tower_room_mm > 0:
        roomy = [p for p in cands if tower_strip(p[0], p[1], w, h)[0] >= tower_room_mm]
        if roomy:
            return max(roomy, key=lambda p: (round(tower_strip(p[0], p[1], w, h)[0], 3), -dist(p)))
    return min(cands, key=dist)


def max_square_on_bed() -> float:
    """Largest square footprint (mm, 0.01 mm resolution) that place_on_bed accepts."""
    lo, hi = 0.0, BED_MM
    while hi - lo > 0.005:
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if place_on_bed(mid) is not None else (lo, mid)
    return round(lo, 2)

NAMED_COLORS = {
    "blue": "#1f5fbf",
    "black": "#202020",
    "white": "#f4f4f4",
}


def _pick(d: dict, cls):
    """Build dataclass ``cls`` from ``d`` ignoring unknown keys."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class BandConfig:
    """One color band.

    Exactly one of ``from_m`` / ``from_water_m`` sets the lower elevation
    threshold of an elevation band (the upper threshold is the next band's
    lower one).  ``kind="water"`` marks the band painted onto flattened water
    surfaces; it has no elevation threshold.
    """

    id: str
    color: str
    kind: str = "elevation"  # "elevation" | "water"
    label: str = ""
    from_m: Optional[float] = None  # absolute lower threshold (metres a.s.l.)
    from_water_m: Optional[float] = None  # lower threshold relative to reference water level
    requires_water: bool = False  # drop band if the region has no water (sand/shore)
    max_distance_m: Optional[float] = None  # only paint within this distance of water (skin)
    slope_deg: Optional[float] = None  # also paint wherever slope >= this ...
    slope_from_m: Optional[float] = None  # ... but only above this elevation (default: band's own threshold)
    slot: Optional[int] = None  # pin this band's colour to a physical filament slot (1-based) on every tile
    filament: str = ""  # what is loaded for it, e.g. "PLA Matte Army Beige" (print plan only)

    @property
    def is_water(self) -> bool:
        return self.kind == "water"


@dataclass
class PaletteConfig:
    name: str = "default"
    bands: list[BandConfig] = field(default_factory=list)
    base_color: str = "blue"  # "blue" | "black" | "none" | "#rrggbb"
    skin_depth_mm: float = 1.2  # thickness of the surface-colored cap over the elevation slabs
    min_fraction: float = 0.003  # prune bands covering less than this fraction of the footprint
    overflow: str = "merge"  # "merge" | "swap"  (what to do with >max_slots colors)
    classify_smooth_m: float = 0.0  # Gaussian sigma (ground metres) applied to elevations before choosing skin colours; 0 = off
    despeckle_mm2: float = 0.0  # repaint elevation-band skin islands smaller than this (mm^2 on the print); 0 = off
    max_slots: int = 4

    @staticmethod
    def from_dict(d: dict) -> "PaletteConfig":
        d = dict(d)
        d["bands"] = [_pick(b, BandConfig) if isinstance(b, dict) else b for b in d.get("bands", [])]
        return _pick(d, PaletteConfig)

    def band(self, band_id: str) -> BandConfig:
        for b in self.bands:
            if b.id == band_id:
                return b
        raise KeyError(band_id)


def default_palette() -> PaletteConfig:
    """Full alpine band set.  Bands that don't apply to a region are auto-pruned."""
    return PaletteConfig(
        name="default",
        bands=[
            BandConfig("water", NAMED_COLORS["blue"], kind="water", label="Water"),
            BandConfig("sand", "#d9c58c", label="Sand / shore", from_water_m=0.0, requires_water=True,
                       max_distance_m=4000.0),
            BandConfig("lowland", "#5aa62c", label="Lowland vegetation", from_water_m=15.0),
            BandConfig("upland", "#2f6b1e", label="Upland forest", from_m=1000.0),
            BandConfig("rock", "#8b8b8b", label="Rock", from_m=1900.0, slope_deg=38.0, slope_from_m=1200.0),
            BandConfig("snow", NAMED_COLORS["white"], label="Snow", from_m=2800.0),
        ],
    )


@dataclass
class RiversConfig:
    """Rivers/canals/streams painted as a thin blue skin along the terrain surface.

    Source is OpenStreetMap (ODbL - attribution required, see SOURCES.md).
    Widths: the OSM ``width`` tag when present, else ``default_width_m`` per
    class, times ``width_scale``; then widened to at least ``min_width_mm`` on
    the print so a 0.4 mm nozzle can lay the colour down (1.2 mm = ~3 lines).
    Streams and canals are only included at scales of 1:``stream_max_scale`` /
    1:``canal_max_scale`` or larger: at small scales canals are mostly drainage
    ditches, mill races and irrigation channels running a few hundred metres
    beside the river, which print as clutter (Martigny, 2026-09-30).

    Width hierarchy (``width_by_length``): a river's printed minimum width grows
    with the total length of the river (all OSM ways sharing its name) inside
    the project, sqrt(length / ``length_ref_km``) times ``min_width_mm``, capped
    at ``max_width_mult``, so the Rhône reads as the main river.

    Clean-up (after rasterising): green gaps narrower than ``min_gap_mm``
    between blue areas are filled (unprintable slivers), and isolated blue
    fragments smaller than ``min_fragment_mm2`` are dropped.

    Culverts and tunnels are hidden, except chains no longer than
    ``culvert_bridge_m`` and (``culvert_join``) chains whose every end meets
    visible water, so brooks don't print as dashes.  ``canal_bank_mm`` keeps
    a land strip between a canal and an adjacent lake/sea (the Manchester Ship
    Canal runs along the Mersey shore at Eastham).
    """

    enabled: bool = True
    source: str = "osm"
    classes: list[str] = field(default_factory=lambda: ["river", "canal", "stream"])
    stream_max_scale: float = 60000.0
    canal_max_scale: float = 60000.0
    min_width_mm: float = 1.2
    width_scale: float = 1.0
    depth_mm: float = 0.6  # blue skin thickness along rivers (3 layers at 0.2 mm)
    default_width_m: dict[str, float] = field(default_factory=lambda: {"river": 30.0, "canal": 15.0, "stream": 5.0})
    width_by_length: bool = True
    length_ref_km: float = 15.0
    max_width_mult: float = 2.5
    min_gap_mm: float = 1.2
    min_fragment_mm2: float = 5.0
    # culverts / tunnels (2026-11-02): drawn when short or when they join two visible stretches
    culvert_bridge_m: float = 300.0
    culvert_join: bool = True
    # holes in the OSM data: join a dangling downstream end to a waterway this close (0 = off)
    gap_join_m: float = 0.0
    # canals running beside a lake/sea get a land bank this wide so they don't merge into it (0 = off)
    canal_bank_mm: float = 0.0

    def active_classes(self, scale: float) -> list[str]:
        out = [c for c in self.classes if c in ("river", "canal", "stream")]
        if scale > self.stream_max_scale:
            out = [c for c in out if c != "stream"]
        if scale > self.canal_max_scale:
            out = [c for c in out if c != "canal"]
        return out or ["river"]


@dataclass
class UrbanConfig:
    """Optional built-up areas and major roads (OpenStreetMap, ODbL) painted as a
    dark-grey surface skin.  Off by default: natural terrain usually looks
    better, and every extra colour competes for the 4 slots."""

    enabled: bool = False
    source: str = "osm"
    color: str = "#4a4a4a"
    label: str = "Urban"
    landuse: list[str] = field(default_factory=lambda: ["residential", "industrial", "commercial", "retail"])
    roads: list[str] = field(default_factory=lambda: ["motorway", "trunk", "primary"])
    road_min_width_mm: float = 1.2  # printable minimum for a road line (0.4 mm nozzle ~ 3 lines)
    road_width_scale: float = 1.0
    road_default_width_m: dict[str, float] = field(default_factory=lambda: {"motorway": 30.0, "trunk": 20.0, "primary": 12.0})
    depth_mm: float = 0.6  # skin thickness over urban cells (like rivers)


@dataclass
class SlicerConfig:
    """How to drive OrcaSlicer (Elegoo fork) headlessly.  See slicer.py."""

    executable: str = ""  # auto-detect when empty (also honours $ORCA_SLICER)
    machine_preset: str = ""  # path to exported machine .json
    process_preset: str = ""  # path to exported process .json
    filament_presets: list[str] = field(default_factory=list)  # one .json per physical slot (up to 4)
    extra_args: list[str] = field(default_factory=list)
    timeout_s: int = 3600
    # "auto": switch the prime tower off when the tile leaves no room for one on the bed
    # (needs a prime_tower_room_mm strip on one side of the placed, centred tile; a 200 mm tile leaves only 28 mm per side); "on"/"off" force it.  Applied by writing a copy of
    # process_preset with enable_prime_tower overridden.  The CC2 purges each colour
    # change itself (purge_in_prime_tower = 0), so no tower only loses the nozzle wipe
    # and OrcaSlicer's flush-into-infill, which needs a tower to take effect.
    prime_tower: str = "auto"
    prime_tower_room_mm: float = 35.0  # free bed width "auto" needs beside the tile to keep the tower


@dataclass
class OverlayConfig:
    """Black-only SVG overlay (contours, symbols, names, grid) printed 1:1 on
    transparent film and laid over the relief behind the frame glass.  See overlay.py.

    The overlay covers the frame's visible ``window_mm`` (width, height), centred
    on the assembled model; the model is made larger than the window so its
    edges never show.  The map neatline sits ``margin_mm`` inside the window and
    the border between carries grid references and the data credits."""

    enabled: bool = False
    window_mm: list[float] = field(default_factory=lambda: [300.0, 400.0])
    margin_mm: float = 9.0  # top / left / right border
    bottom_margin_mm: float = 15.0  # bottom border: grid numbers, title, scale bar, credits
    grid_crs: str = "EPSG:27700"  # grid drawn on the overlay (OS National Grid)
    grid_m: float = 1000.0
    grid_letters: bool = True  # OS 100 km square letters (SJ, SD, ...) in the corners
    contour_m: float = 10.0
    index_contour_m: float = 50.0
    contour_smooth_m: float = 75.0  # SRTM is a surface model (roofs, trees): smooth before contouring
    contour_min_len_mm: float = 6.0  # drop tiny closed rings (noise)
    title: str = ""
    subtitle: str = ""
    places: list[str] = field(default_factory=lambda: ["city", "town", "suburb", "village"])
    symbols: list[str] = field(default_factory=lambda: ["station", "lighthouse", "peak", "airport", "stadium",
                                                         "ferry", "railway", "motorway"])
    font_family: str = "Helvetica, Arial, 'Liberation Sans', sans-serif"
    line_mm: float = 0.18  # thinnest printed line (film printers manage ~0.1 mm)
    labels: list[dict] = field(default_factory=list)  # hand-placed names: {text, e, n (grid_crs m) or lon, lat, size, angle, italic, spacing}
    max_peaks: int = 0  # label at most this many peaks, highest first (0 = all that fit)
    peak_min_ele_m: float = 0.0  # skip peaks below this elevation (0 = off)
    peak_spacing_mm: float = 0.0  # no two labelled peaks closer than this on the film (0 = off)
    town_city_gap_m: float = 0.0  # drop towns/suburbs/villages this close to a city (ground metres; 0 = off)
    airport_require_iata: bool = False  # only airports with an IATA code (drops altiports and airfields)


@dataclass
class ProjectConfig:
    name: str = "untitled"
    center_lat: float = 46.35
    center_lon: float = 6.55
    scale: float = 100000.0  # 1:scale
    tiles_x: int = 1
    tiles_y: int = 1
    tile_mm: float = TILE_MM_DEFAULT  # tile width (east-west) on the print
    tile_mm_y: Optional[float] = None  # tile height (north-south); None = square tiles
    crs: str = "utm"  # model projection: "utm" (auto zone) or any projected CRS, e.g. "EPSG:27700" (OS National Grid)
    center_xy: Optional[list[float]] = None  # grid centre in ``crs`` metres; overrides center_lat/lon (snaps grid lines)
    exaggeration: Optional[float] = None  # None -> auto-suggest from measured relief
    base_mm: float = 3.0
    max_height_mm: float = 180.0
    pitch_mm: float = 0.25  # mesh grid pitch on the printed tile (finer than a 0.4 mm nozzle, so no visible facets)
    dem_source: str = "srtmgl1"  # "srtmgl1" | "local:<dir>"  (see dem.py, SOURCES.md)
    water: bool = True
    rivers: RiversConfig = field(default_factory=RiversConfig)
    urban: UrbanConfig = field(default_factory=UrbanConfig)
    shrinkage_pct: float = 0.0  # XY scale-up so the cooled print measures tile_mm (set to your filament's shrinkage, e.g. 0.3 for PLA, unless the slicer filament profile already compensates)
    palette: PaletteConfig = field(default_factory=default_palette)
    slicer: SlicerConfig = field(default_factory=SlicerConfig)
    overlay: OverlayConfig = field(default_factory=OverlayConfig)
    monochrome: bool = False  # single-color draft print: every band gets monochrome_color -> one object per tile
    monochrome_color: str = "#c8c8c8"
    notes: str = ""

    # ---- derived geometry -------------------------------------------------
    @property
    def mm_per_m(self) -> float:
        """Horizontal model millimetres per real-world metre (before exaggeration)."""
        return 1000.0 / self.scale

    @property
    def tile_w_mm(self) -> float:
        return float(self.tile_mm)

    @property
    def tile_h_mm(self) -> float:
        return float(self.tile_mm if self.tile_mm_y is None else self.tile_mm_y)

    @property
    def square_tiles(self) -> bool:
        return abs(self.tile_w_mm - self.tile_h_mm) < 1e-9

    @property
    def tile_m(self) -> float:
        """Real-world east-west edge length of one tile in metres."""
        return self.tile_w_mm * self.scale / 1000.0

    @property
    def tile_h_m(self) -> float:
        """Real-world north-south edge length of one tile in metres."""
        return self.tile_h_mm * self.scale / 1000.0

    @property
    def model_mm(self) -> tuple[float, float]:
        """Whole assembled model (all tiles) in mm, (width, height)."""
        return (self.tiles_x * self.tile_w_mm, self.tiles_y * self.tile_h_mm)

    @property
    def pitch_m(self) -> float:
        return self.pitch_mm * self.scale / 1000.0

    @property
    def nodes_per_tile(self) -> int:
        """Grid cells along a tile's width (x)."""
        return int(round(self.tile_w_mm / self.pitch_mm))

    def nodes_y_for(self, nodes_x: int) -> int:
        """Grid cells along a tile's height for ``nodes_x`` cells across (same pitch both ways)."""
        return max(1, int(round(nodes_x * self.tile_h_mm / self.tile_w_mm)))

    def tower_room_mm(self) -> float:
        """Free bed width the placement must keep for a prime tower (0 = don't care)."""
        return self.slicer.prime_tower_room_mm if self.slicer.prime_tower == "on" else 0.0

    def bed_position(self) -> tuple[float, float] | None:
        f = 1 + self.shrinkage_pct / 100.0
        return place_on_bed(self.tile_w_mm * f, self.tile_h_mm * f, self.tower_room_mm())

    # ---- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ProjectConfig":
        d = dict(d)
        if "palette" in d and isinstance(d["palette"], dict):
            d["palette"] = PaletteConfig.from_dict(d["palette"])
        if "rivers" in d and isinstance(d["rivers"], dict):
            d["rivers"] = _pick(d["rivers"], RiversConfig)
        if "urban" in d and isinstance(d["urban"], dict):
            d["urban"] = _pick(d["urban"], UrbanConfig)
        if "slicer" in d and isinstance(d["slicer"], dict):
            d["slicer"] = _pick(d["slicer"], SlicerConfig)
        if "overlay" in d and isinstance(d["overlay"], dict):
            d["overlay"] = _pick(d["overlay"], OverlayConfig)
        return _pick(d, ProjectConfig)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2) + "\n")

    @staticmethod
    def load(path: str | Path) -> "ProjectConfig":
        return ProjectConfig.from_dict(json.loads(Path(path).read_text()))

    def validate(self) -> list[str]:
        problems = []
        if not (0.05 <= self.pitch_mm <= 5):
            problems.append("pitch_mm should be between 0.05 and 5")
        f = 1 + self.shrinkage_pct / 100.0
        fw, fh = self.tile_w_mm * f, self.tile_h_mm * f
        if place_on_bed(fw, fh) is None:
            if self.square_tiles:
                m = max_square_on_bed()
                msg = (f"a {fw:.2f} mm tile does not fit the bed outside the printer's no-print zone "
                       f"(largest printable square is {m:.2f} mm)")
                if self.shrinkage_pct > 0 and self.tile_mm <= m:
                    # a 245 mm tile already uses the whole bed, so compensation has to come out of the nominal size
                    msg += (f"; shrinkage compensation needs room: use shrinkage_pct <= "
                            f"{max(0.0, (m / self.tile_mm - 1) * 100):.2f} or tile_mm <= "
                            f"{m / f:.2f}")
            else:
                msg = f"a {fw:.2f} x {fh:.2f} mm tile does not fit the bed outside the printer's no-print zone"
            problems.append(msg)
        elif self.slicer.prime_tower == "on":
            p = self.bed_position()
            room = tower_strip(p[0], p[1], fw, fh)[0]
            if room < self.slicer.prime_tower_room_mm:
                problems.append(f"slicer.prime_tower is 'on' but a {fw:.1f} x {fh:.1f} mm tile leaves at most "
                                f"{room:.1f} mm of bed for it (needs prime_tower_room_mm = "
                                f"{self.slicer.prime_tower_room_mm:.0f}); use smaller tiles or prime_tower 'auto'")
        if self.tile_mm_y is not None and not (20 <= self.tile_mm_y <= BED_MM):
            problems.append("tile_mm_y should be between 20 and 256 mm")
        if self.center_xy is not None and (self.crs == "utm" or len(self.center_xy) != 2):
            problems.append("center_xy needs an explicit projected crs (e.g. EPSG:27700) and two numbers")
        if not (-2.0 <= self.shrinkage_pct <= 5.0):
            problems.append("shrinkage_pct should be a small percentage (e.g. 0.3)")
        if self.rivers.enabled and self.rivers.depth_mm > self.palette.skin_depth_mm:
            problems.append("rivers.depth_mm cannot exceed palette.skin_depth_mm")
        if self.rivers.enabled and not (0.3 <= self.rivers.min_width_mm <= 20):
            problems.append("rivers.min_width_mm should be between 0.3 and 20 mm")
        if self.urban.enabled and self.urban.depth_mm > self.palette.skin_depth_mm:
            problems.append("urban.depth_mm cannot exceed palette.skin_depth_mm")
        if self.urban.enabled and not (self.urban.landuse or self.urban.roads):
            problems.append("urban is enabled but has no landuse or road classes")
        if self.base_mm < self.palette.skin_depth_mm + 0.8:
            problems.append("base_mm must be at least skin_depth_mm + 0.8 mm")
        if self.tiles_x < 1 or self.tiles_y < 1:
            problems.append("tiles_x/tiles_y must be >= 1")
        if self.max_height_mm > BED_MM - 10:
            problems.append("max_height_mm leaves no margin under the 256 mm ceiling")
        if len(self.palette.bands) == 0:
            problems.append("palette has no bands")
        return problems


def resolve_color(spec: str, water_color: str | None = None) -> str:
    """Turn 'blue' / 'black' / '#rrggbb' into a hex color; 'blue' follows the water band."""
    if spec == "blue" and water_color:
        return water_color
    return NAMED_COLORS.get(spec, spec)
