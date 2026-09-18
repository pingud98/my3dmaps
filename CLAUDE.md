# my3dmaps

Automated pipeline that turns real-world topographic/land-cover data into multi-color 3D-printable landscape models. Pick an area and scale on a map, adjust a height-exaggeration slider, preview the colored 3D model, and export sliced G-code ready for the printer.

Repo: `github.com/pingud98/my3dmaps`

## Why this exists

The user wants physical relief maps of real places (starting with Geneva/Lake Geneva up to Mont Blanc, and Liverpool, UK) printed as tiled, multi-color landscapes — water in blue, shoreline in sand, two vegetation tiers, rock, and snow-capped peaks — on an Elegoo Centauri Carbon 2 Combo. The whole thing (data fetch → mesh → color assignment → sliced G-code) should be one workflow driven from a simple UI, not a manual multi-tool process.

Coding is being done by Fable against this spec. Keep this file the single source of truth for scope and decisions; update it as design decisions are made or revised, don't let it drift from the actual implementation.

## Hardware target

**Elegoo Centauri Carbon 2 Combo**
- Build volume: 256 × 256 × 256 mm
- 4 filament slots loaded simultaneously (combo/AMS-style unit) — printing a 5th color requires a manual filament swap mid-print
- Slicer: OrcaSlicer (Elegoo fork). We need this to (a) accept a multi-material 3MF/STL set, (b) auto-assign objects to the 4 slots, (c) produce G-code headlessly via CLI. Exact CLI flags/behavior need to be verified against the real slicer install early — treat this as a spike, don't assume the flag names below are final.

## Tiling

Models are built in **200 × 200 mm modules** (footprint), leaving margin inside the 256 × 256 bed for adhesion/skirt. Reasons:
- Fits the bed with margin.
- Lets a long/thin region (e.g. a lake shoreline, a valley) be printed as a strip of tiles rather than one oversized model.
- Lets a larger area (e.g. full Geneva-to-Mont-Blanc transect) be assembled from a 2×2 or 3×3 (etc.) grid of tiles.

Requirements:
- Adjacent tiles must share elevation data at their border so terrain lines up seamlessly (no visible seam/step).
- Tiles are **glued** edge to edge (decision 2026-09-18: no keying/self-assembly geometry). What matters is that the cooled tiles measure the nominal size, so the build offers an XY shrinkage compensation (`shrinkage_pct`).
- Batch mode: given a region larger than one tile, auto-split into an N×M grid at the chosen scale and generate each tile as a separate job.
- Keep a safe max model height budget (e.g. ~180–200 mm) that leaves margin under the 256 mm ceiling once base thickness is accounted for.

## Data sources (must be genuinely reusable — prefer true public domain over merely "free")

- **Elevation (DEM):** NASA/USGS SRTM 1 Arc-Second Global (~30 m) as the default — it's a US government work and is public domain, and it covers both target areas (Alps and UK). Note as an optional higher-accuracy alternate for the Alps: swisstopo swissALTI3D (Swiss open data, free reuse) — flag clearly in code/docs that this is an *opt-in* alternate source with its own license terms, not the default.
- **Water bodies / coastline:** Natural Earth vector data (lakes, ocean/coastline) — explicitly public domain, good match for the licensing bar here. Use this to mask/flatten water rather than deriving it purely from DEM noise.
- **Built-up areas / major roads (optional, off by default):** OpenStreetMap landuse polygons and motorway/trunk/primary roads, same ODbL terms as rivers. Decision 2026-09-18: an opt-in "urban" overlay in dark grey, because natural terrain usually reads better and it costs a slot colour.
- **Rivers, canals, streams:** OpenStreetMap via the Overpass API (decision 2026-09-18, user's call). **ODbL, not public domain** — a model containing them must be credited "© OpenStreetMap contributors"; the UI, `PRINT_PLAN.txt` and SOURCES.md all say so, and `rivers.enabled=false` / `--no-rivers` keeps a model strictly public-domain. Natural Earth rivers only hold continental rivers and every public-domain alternative was too coarse (see SOURCES.md).
- **Land cover:** not used as a separate dataset initially. Terrain type (sand/vegetation tiers/rock/snow) is derived from **elevation + slope** relative to the local water level, not from a separate land-cover raster — this avoids pulling in a CC-BY-only dataset (e.g. ESA WorldCover) for something we can approximate well from the DEM itself. Revisit as a stretch goal if elevation/slope classification proves too crude for a specific region (dense urban Liverpool in particular — may need a cheap heuristic or an optional land-cover overlay later).

Document the exact license and attribution requirement next to each data source in code (e.g. a `SOURCES.md` or inline metadata), since "public domain" was a specific, deliberate requirement — don't silently swap in a CC-BY dataset later without flagging it.

## Color / elevation-band system

Palette is config-driven (YAML/JSON), not hardcoded, and **auto-prunes** bands that don't apply to a given region (Liverpool has no rock or snow bands; they're simply dropped rather than forced).

Default band set, bottom to top:
0. **Base/support** (optional, user-selectable) — blue or black. Doubles as the water layer if set to blue.
1. **Water** — blue
2. **Sand/shore** — tan, a narrow band just above local water level
3. **Lowland vegetation** — mid-green
4. **Upland vegetation / forest** — dark green
5. **Rock** — grey, driven by slope threshold and/or elevation (treeline to snowline); omitted where not applicable
6. **Snow** — white, top elevation band above a configurable snowline; omitted for low-relief regions

Key design principle for minimizing color changes: **bands are strictly elevation-ordered, and the printer prints bottom-to-top** — so if the mesh is decomposed correctly, each color's regions are already contiguous in the print sequence and each spool only needs to be loaded once per print, even with 5-6 bands, as long as slot swaps happen at clean layer-height boundaries. This should be a first-class constraint in how the mesh/print job is planned, not an afterthought.

When the applicable band count exceeds 4 (the simultaneous slot limit):
- Default: merge the two least visually-distinct adjacent bands automatically (e.g. merge rock into upland vegetation if a region barely has any exposed rock).
- Alternative (user-selectable): plan a single manual mid-print filament swap rather than merging, since the bottom-to-top ordering means this only costs one pause, not several.

Band boundaries (snowline elevation, treeline, sand-band width, etc.) are per-project config, not global constants — Geneva/Mont Blanc and Liverpool need very different values.

## UI

Local web app (not a native app) so map interaction and 3D preview can reuse mature JS libraries:
- **Area & scale selection:** Leaflet map with a draggable/resizable bounding box. Box dimensions are locked to the physical tile size × chosen scale (e.g. 200 mm tile at 1:25,000 = 5 km per tile edge), so dragging/resizing the box directly manipulates scale, and the UI shows resulting real-world coverage and tile count live.
- **Height exaggeration:** a slider (multiplier, e.g. 1×–8×) applied after scale conversion, live-updating the 3D preview. No fixed default — flat regions typically want more exaggeration to read as interesting terrain, mountainous regions want less; consider suggesting a starting value based on the region's actual relief (max−min elevation over footprint) rather than a flat constant.
- **3D preview:** Three.js viewer showing the actual colored mesh (post band-classification) so the user can inspect before committing to slicing.
- **Palette controls:** pick support-layer color (blue/black/none), tweak band boundaries, see which bands are present/pruned for the current selection.
- Also runnable **headless via CLI** once bbox/scale/exaggeration/palette are known, for scripting repeat exports (e.g. regenerating a tile after tweaking one parameter).

## Pipeline (end to end)

1. Area + scale selection (UI) → bounding box in real-world coordinates, tile grid computed.
2. Height exaggeration + palette chosen (UI).
3. Fetch & mosaic SRTM tiles covering the bbox; reproject to an auto-selected local UTM zone for correct metric scaling.
4. Fetch Natural Earth water vectors for the bbox; rasterize/clip to flatten water areas to a fixed level. Fetch OSM waterways for the bbox and rasterize them, widened to a printable minimum width, as river cells.
5. Classify terrain per-pixel into elevation/slope bands per the active palette; auto-prune inapplicable bands.
6. Generate heightmap → 3D mesh, scaled to the tile's physical footprint and Z-scaled by (real elevation × exaggeration multiplier), clamped to the safe height budget.
7. Segment the mesh into per-band sub-solids (with slight overlap at boundaries to avoid gaps/z-fighting), preserving elevation-ordering for the "load each color once" property.
8. Ensure border elevations match neighboring tiles in a multi-tile job; apply shrinkage compensation so glued tiles meet cleanly.
9. Export as 3MF with per-object materials (OrcaSlicer-compatible multi-material format) — one object per color band.
10. Drive OrcaSlicer (Elegoo fork) CLI headlessly: load the 3MF, map each color-band object to one of the 4 physical slots (applying the merge/swap logic above if >4 bands), slice, emit G-code.
11. Preview stays available in the UI at every stage for inspection before committing to a slice/print.

## Suggested tech stack

- **Backend/processing:** Python — `rasterio`/`GDAL` (DEM I/O + reprojection), `numpy`/`scipy` (classification, slope), `shapely`/`pyproj` (water vectors, CRS), `trimesh` or `pyvista` (heightmap → mesh, mesh export to 3MF/STL).
- **Frontend:** Leaflet (area/scale selection) + Three.js (3D preview), served by a small FastAPI (or Flask) app that also shells out to the OrcaSlicer CLI.
- **CLI entry point:** same core pipeline, invocable without the UI for scripted/batch tile generation.

## Test/example projects

Use these as the initial validation targets, but keep them as **config presets** (bbox + scale + exaggeration + palette saved as JSON), not hardcoded pipeline behavior — the pipeline must work for an arbitrary region a user draws on the map:
- **Geneva → Mont Blanc:** Lake Geneva basin extending to the Mont Blanc massif. Full band set exercised (water through snow); likely needs a multi-tile grid given the elevation range and area.
- **Liverpool, UK:** low-relief, no rock/snow bands — exercises band auto-pruning and tests whether exaggeration + sand/vegetation contrast alone reads as an interesting print.

## Implementation status & decisions (2026-09-18)

v1 is implemented and validated end to end on both presets (offline unit
tests + real SRTM/Natural Earth runs + headless-browser UI check).  Layout:

```
my3dmaps/config.py    ProjectConfig / PaletteConfig (JSON presets), validation
my3dmaps/geo.py       UTM zone choice, shared global node grid, tile outlines
my3dmaps/dem.py       SRTMGL1 fetch (ESA STEP mirror, no login; Earthdata fallback), HGT sampling
my3dmaps/water.py     Natural Earth lakes/land -> DEM-snapped water masks & levels
my3dmaps/rivers.py    OpenStreetMap waterways (Overpass, cached) -> widened river cell mask
my3dmaps/urban.py     optional OSM built-up areas + major roads -> urban cell mask (dark grey skin)
my3dmaps/classify.py  bands: threshold resolution, slab/skin classification, prune, merge
my3dmaps/tile.py      per-tile solids: slabs + skins + plain base, shrinkage scaling -> one object per colour
my3dmaps/mesh.py      sheet mesher (closed solids), STL / 3MF / GLB writers
my3dmaps/plan.py      4-slot planning with manual-swap detection
my3dmaps/slicer.py    OrcaSlicer CLI driver (assemble-list route), probe, pause insertion
my3dmaps/pipeline.py  orchestration, preview payload, build_project
my3dmaps/cli.py       `my3dmaps build|info|grid|serve|slicer probe|slicer run`
my3dmaps/server.py    FastAPI: presets, grid, preview, jobs, file downloads
my3dmaps/web/         Leaflet picker + Three.js preview (single page, CDN libs)
presets/              geneva.json, liverpool.json
tests/                offline synthetic tests; `--network` for live-data tests
tools/render_tiles.py headless-Chromium renders of built tiles / assemblies (docs/images)
docs/images/          README renders (regenerate with tools/render_tiles.py after a build)
```

Decisions made while implementing (these refine the sections above):

- **No GDAL/rasterio.** HGT tiles are raw int16; sampling is done with
  scipy `map_coordinates` on a pyproj-derived UTM grid.  Keeps the install to
  pure wheels on any platform (developed on aarch64 Linux).
- **Slab + skin decomposition** (resolves the mesh-segmentation spike).  Band
  lower thresholds become horizontal cutting planes; each band's *slab* is the
  terrain volume between its planes minus a `skin_depth_mm` (1.2 mm) cap.  The
  *skin* is painted per cell from the classification, which is where slope
  (rock), distance-to-water (shore) and flat water act.  Pure elevation bands
  therefore print as strictly bottom-to-top colour ranges; non-elevation rules
  only ever cost 2 simultaneous colours over a thin range.  All solids come
  from one closed "sheet" primitive (top, bottom, walls, welded zero-thickness
  contour edges), so no boolean library is needed.  Objects of the same colour
  are concatenated (blue base + water = one object).
- **Water:** Natural Earth 10m is far too coarse to use directly.  Its polygons
  only seed bodies; each body's level is read from SRTM v3 (which already has
  lakes/sea flattened) and the mask grows to the connected DEM region within
  ±1.5 m (lakes) / ≤0.5 m (sea).  Water cells need all four corners in water
  so the blue skin never climbs a shore.
- **Rivers (2026-09-18):** OSM `waterway=river|canal|stream` lines and
  `natural=water` river/canal areas from one Overpass query per bbox (cached
  in `~/.cache/my3dmaps/osm/`; tunnels/culverts skipped).  Lines are buffered
  to the OSM `width` tag or 30/15/5 m per class, × `width_scale`, but never
  narrower than `min_width_mm` (1.2 mm ≈ 3 lines of a 0.4 mm nozzle, deliberately
  generous) nor than one grid cell, then rasterised onto cell centres.  River
  cells are painted with the water colour as a thinner skin (`depth_mm` 0.6 =
  3 layers) that follows the terrain; the slab beneath rises to meet it (skin
  depth is per node so all sheets share one interface).  Rivers keep the water
  band alive in lake-less regions and are exempt from the small-area prune,
  but do **not** feed the shore-distance rule or the reference water level.
  Streams are only requested at 1:60,000 or larger (`stream_max_scale`) —
  Overpass times out on every Alpine stream over 150 km, and they would be
  clutter at that scale anyway.  Blue therefore spans the whole height of a
  hilly tile, so with `overflow: "swap"` the planner keeps blue resident and
  swaps among the other three slots.
- **Urban overlay (2026-09-18):** `urban.enabled` (default false; `--urban`
  on the CLI, checkbox in the UI).  One Overpass query for
  `landuse=residential|industrial|commercial|retail` (ways + multipolygon
  relations) and `highway=motorway|trunk|primary` (buffered to OSM
  `width`/`lanes`×3.5 m or 30/20/12 m, never under `road_min_width_mm` 1.2 mm;
  tunnels skipped).  Painted as a skin-only "urban" band (`kind: "urban"`, no
  slab, `depth_mm` 0.6, colour `#4a4a4a`), never on lakes/sea; rivers are
  painted afterwards so they still cross towns.  Merging the urban band means
  reverting its cells to the terrain class underneath (not painting them the
  neighbour's colour), and its merge score is halved so it tends to be the
  first colour given up under `overflow: "merge"` (Liverpool still merges its
  two near-identical greens first and keeps urban).  Liverpool at 1:50k comes
  out 43 % urban, which is why it is off by default.
- **Review fixes (2026-09-18):** the shore distance rule now repaints
  far-from-water cells only with an elevation band (never water/urban when sand
  is the top band); `base_color: "none"` follows the lowest *elevation* band,
  not the urban overlay.  Both have regression tests.
- **Lossless decimation (2026-09-18):** in `sheet_mesh`, cells whose four
  top (or bottom) corners share exactly one height are covered greedily by
  rectangles (single cells excluded) and each rectangle is a fan around a new
  centre vertex over its *required* boundary nodes — nodes touched by any
  non-merged cell, wall or rectangle corner — so neighbouring faces still
  share edges (no T-junctions).  Slab bottoms, clipped slab tops and lake
  surfaces collapse; terrain skins are untouched.  Volumes are bit-identical
  and every shell stays closed (tests + trimesh).  Measured: Liverpool
  tile_x0_y0 STLs 94.7 → 50.2 MB (sand 32 → 4.7 MB), 3MF 17.7 → 10.0 MB;
  whole projects Liverpool 480 → 244 MB, Geneva 1015 → 488 MB.  What remains
  is genuine terrain surface (no lossy simplification, by decision).
- **Model Z:** `z = base_mm + skin + (elev − min_elev) · (1000/scale) · exaggeration`;
  auto exaggeration targets ~45 mm of relief per 200 mm tile, clamped to 1–8×
  and to `max_height_mm` (180).  Geneva 1:250k → 2.5×, Liverpool 1:50k → 8×.
- **Pruning/merging:** bands with no area above their threshold, or covering
  <0.3 % of the footprint, are dropped (Liverpool loses rock & snow).  With
  more colours than slots, `overflow: "merge"` merges the adjacent pair with
  the lowest (ΔE·√min-area) score, keeping the larger band's colour;
  `overflow: "swap"` keeps all bands and plans manual swaps where a finished
  colour frees a slot (Geneva preset: 6 colours, 2 swaps on the Mont Blanc
  tile, at 13.0 mm and 28.6 mm).  Slots are only reused when all 4 are taken.
- **No keying (2026-09-18):** the user glues tiles, so the base plate is a
  plain square and the earlier tab-and-slot keying was removed.
  `shrinkage_pct` scales each tile's XY about its centre so the cooled part
  measures `tile_mm` (0.3 % is typical for PLA; leave 0 if the slicer's
  filament profile already compensates — never both).  Z is not scaled.
- **Tile placement:** each tile's STL/3MF is centred on the 256 mm bed
  (offset 28 mm), so files drop straight into the slicer.
- **Slicer CLI (spike result, from the OrcaSlicer sources, not a live binary):**
  `--load-filament-ids` cannot be combined with a 3MF input, so the driver uses
  `--load-assemble-list` with per-band STLs (one filament per part, merged into
  one object, no re-arrangement) plus `--load-settings`, `--load-filaments`,
  `--slice 0`, `--export-3mf`, `--outputdir`; G-code appears as
  `plate_1.gcode`.  Manual-swap pauses are inserted into the G-code before the
  first layer at/above the planned height (`machine_pause_gcode` from the
  exported machine preset, else `M601`).  The 3MF also carries per-object
  `extruder` metadata and `custom_gcode_per_layer.xml` pauses for GUI use.
  **Still to verify on the real Elegoo fork:** run `my3dmaps slicer probe`, export
  machine/process/filament presets from the GUI, then `build --slice`.
- **Preview** is rendered client-side from the elevation + cell-class grid at
  96 nodes/tile, so the exaggeration slider is instant; tile walls show the slab
  colours.  Rivers appear in the preview because they are ordinary water-class
  cells (rasterised at ≥1 preview cell wide).
- **Not done / next:** headless-slicer verification on a real install;
  urban-heuristic for Liverpool (elevation+slope reads fine as tidal flats +
  low hills, so no extra land-cover layer was needed).  Lossy terrain
  simplification is deliberately not done.

## Open questions / spikes for Fable to resolve early

- ~~Exact OrcaSlicer CLI surface~~ → resolved from source (see decisions); **live verification against the installed Elegoo fork still pending** (`my3dmaps slicer probe`).
- ~~Elevation+slope for urban Liverpool~~ → looked acceptable in the preview (estuary, tidal flats, low hills); revisit only if a print reads badly.
- ~~Mesh segmentation~~ → slab + skin decomposition, see decisions.

## Stretch goals (not required for v1, worth keeping in mind)

- ~~Printed base-plate legend~~ → dropped by the user (2026-09-18), not wanted.
- ~~Single-color "preview print" mode~~ → done: `monochrome` project flag / UI checkbox.
- ~~Saved project presets~~ → done: `presets/*.json`, save/load in the UI.
- ~~Auto-suggested exaggeration~~ → done (`suggest_exaggeration`, "auto" in the UI).

## Non-goals

- Not building a general-purpose GIS tool — scope is DEM-in, printable-landscape-out.
- Not targeting printers other than the Centauri Carbon 2 Combo for v1; keep the slicing-automation layer isolated enough that another printer/slicer could be swapped in later, but don't generalize prematurely.
