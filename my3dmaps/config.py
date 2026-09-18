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

TILE_MM_DEFAULT = 200.0
BED_MM = 256.0

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
    Streams are only included at scales of 1:``stream_max_scale`` or larger
    (Overpass cannot serve every Alpine stream over a 150 km box anyway).
    """

    enabled: bool = True
    source: str = "osm"
    classes: list[str] = field(default_factory=lambda: ["river", "canal", "stream"])
    stream_max_scale: float = 60000.0
    min_width_mm: float = 1.2
    width_scale: float = 1.0
    depth_mm: float = 0.6  # blue skin thickness along rivers (3 layers at 0.2 mm)
    default_width_m: dict[str, float] = field(default_factory=lambda: {"river": 30.0, "canal": 15.0, "stream": 5.0})

    def active_classes(self, scale: float) -> list[str]:
        out = [c for c in self.classes if c in ("river", "canal", "stream")]
        if scale > self.stream_max_scale:
            out = [c for c in out if c != "stream"]
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


@dataclass
class ProjectConfig:
    name: str = "untitled"
    center_lat: float = 46.35
    center_lon: float = 6.55
    scale: float = 100000.0  # 1:scale
    tiles_x: int = 1
    tiles_y: int = 1
    tile_mm: float = TILE_MM_DEFAULT
    exaggeration: Optional[float] = None  # None -> auto-suggest from measured relief
    base_mm: float = 3.0
    max_height_mm: float = 180.0
    pitch_mm: float = 0.5  # mesh grid pitch on the printed tile
    dem_source: str = "srtmgl1"  # "srtmgl1" | "local:<dir>"  (see dem.py, SOURCES.md)
    water: bool = True
    rivers: RiversConfig = field(default_factory=RiversConfig)
    urban: UrbanConfig = field(default_factory=UrbanConfig)
    shrinkage_pct: float = 0.0  # XY scale-up so the cooled print measures tile_mm (set to your filament's shrinkage, e.g. 0.3 for PLA, unless the slicer filament profile already compensates)
    palette: PaletteConfig = field(default_factory=default_palette)
    slicer: SlicerConfig = field(default_factory=SlicerConfig)
    monochrome: bool = False  # single-color draft print: every band gets monochrome_color -> one object per tile
    monochrome_color: str = "#c8c8c8"
    notes: str = ""

    # ---- derived geometry -------------------------------------------------
    @property
    def mm_per_m(self) -> float:
        """Horizontal model millimetres per real-world metre (before exaggeration)."""
        return 1000.0 / self.scale

    @property
    def tile_m(self) -> float:
        """Real-world edge length of one tile in metres."""
        return self.tile_mm * self.scale / 1000.0

    @property
    def pitch_m(self) -> float:
        return self.pitch_mm * self.scale / 1000.0

    @property
    def nodes_per_tile(self) -> int:
        return int(round(self.tile_mm / self.pitch_mm))

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
        if self.tile_mm * (1 + self.shrinkage_pct / 100.0) > BED_MM - 8:
            problems.append("tile (after shrinkage compensation) leaves no margin on the 256 mm bed")
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
