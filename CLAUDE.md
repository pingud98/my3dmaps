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

Models are built in **245 × 245 mm modules** (footprint; decision 2026-09-28, originally 200 mm) so that 2 × 2 tiles fill a 50 × 50 cm IKEA frame with a 5 mm border. The user asked for 250 mm, but the CC2's OrcaSlicer profile excludes x 246–256, y 0–20 on the 256 × 256 bed, so 245 mm (placed at x 0.5) is the largest printable square; 3 × 3 of 167 mm and 2 × 3 of 250 × 167 mm were offered and declined. Reasons for tiling:
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
- **Area & scale selection:** Leaflet map with a draggable/resizable bounding box. Box dimensions are locked to the physical tile size × chosen scale (e.g. 245 mm tile at 1:25,000 = 6.1 km per tile edge), so dragging/resizing the box directly manipulates scale, and the UI shows resulting real-world coverage and tile count live.
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
- **River legibility (user report 2026-09-30, Martigny on Geneva tile x1_y0):**
  the Rhône plain printed as parallel blue stripes and fragments.  Cause:
  ~180 canal ways (Canal de Fully / du Syndicat drainage canals, meunières,
  bisses) beside ~40 river ways, each widened to the same 1.2 mm (240 m at
  1:200k) as the Rhône.  Fixes: (1) canals, like streams, only at 1:60,000 or
  larger (`canal_max_scale`; canal *areas* are dropped from the query too);
  Liverpool at 1:50k keeps the Ship Canal and the Leeds–Liverpool canal.
  (2) `classify.clean_river_cells` after rasterising: morphological closing of
  river ∪ lake/sea fills land strips narrower than `min_gap_mm` (1.2 mm), and
  8-connected blue components under `min_fragment_mm2` (5 mm²) are dropped;
  river mouths count as part of their lake.  (3) Width hierarchy
  (`width_by_length`): a river line's printable minimum is multiplied by
  sqrt(length / `length_ref_km` 15), capped at 2.5, where length is the total
  of same-named ways that connect (common names like "Le Foron" are several
  unrelated streams).  Geneva: Arve 3.0 mm, Fier 2.5, Rhône 2.2, small
  torrents 1.2.
- **Mesh pitch 0.25 mm (user decision 2026-09-30):** the user prefers a
  slower, finer print to a faceted one and has a 64 GB Xeon for slicing.
  SRTM's 30 m samples are 0.15 mm apart at 1:200k, so 0.5 mm was discarding
  real relief, and colour/river edges stepped in 0.5 mm stairs.  Cost for the
  Geneva 2 × 2 set on the 11 GB aarch64 dev box: 142 s, 4.0 GB peak RSS,
  5–6 M triangles per tile, Mont Blanc tile 3MF 67 MB / STLs 320 MB.  Now the
  default and used by both presets; the UI preview is unaffected (96 nodes
  per tile).
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
  auto exaggeration targets ~45 mm of relief per 200 mm of tile edge (55 mm on
  245 mm; before 2026-09-28 bigger tiles wrongly got *less* relief), clamped to
  1–8× and to `max_height_mm` (180).  Geneva 1:200k would auto-pick 2.5×
  (60 mm tall) but the preset fixes **2.0× (≈49 mm)** so it fits the user's
  6 cm deep frame under glass (decision 2026-09-28; the user may deepen the
  frame back if needed).  Liverpool 1:50k → 8× (22 mm).
- **Pruning/merging:** bands with no area above their threshold, or covering
  <0.3 % of the footprint, are dropped (Liverpool loses rock & snow).  With
  more colours than slots, `overflow: "merge"` merges the adjacent pair with
  the lowest (ΔE·√min-area) score, keeping the larger band's colour;
  `overflow: "swap"` keeps all bands and plans manual swaps where a finished
  colour frees a slot (Geneva preset, 2 × 2 at 1:200k: 5 colours, one
  lowland → snow swap at ~27–28 mm on each of the two eastern tiles at 2.0×).  Slots are
  only reused when all 4 are taken.
- **Geneva has no shore band (user decision 2026-09-28); Liverpool keeps it.**
  At 1:250,000 the shore was a 1–2 mm rim around the lake.  Measured on the
  6-tile set: manual swaps 9 → 3, automatic per-layer colour changes 950 → 894
  with rivers (530 → 472 lakes only).  At Liverpool's 1:50,000 the shore band
  shows real tidal flats and costs no swaps (4 colours).
- **Colour-change costs (measured 2026-09-28, 0.2 mm layers, Σ(colours−1)
  per layer):** rivers roughly double automatic colour changes versus lakes
  only (Geneva 530 → 950, Liverpool 129 → 282) because river blue climbs the
  valleys and appears on most layers, whereas lake blue is confined to the
  bottom layers.  Rivers add no manual swaps where the region already has a
  lake or sea.
- **No keying (2026-09-18):** the user glues tiles, so the base plate is a
  plain square and the earlier tab-and-slot keying was removed.
  `shrinkage_pct` scales each tile's XY about its centre so the cooled part
  measures `tile_mm` (0.3 % is typical for PLA; leave 0 if the slicer's
  filament profile already compensates — never both).  Z is not scaled.
  A 245 mm tile already fills the printable bed, so compensation must come
  out of the nominal size (e.g. `tile_mm` 244.2 with 0.3 %); `validate()`
  says so with the exact limits (2026-09-28).  **Decision 2026-09-30: stay
  at 245 mm with `shrinkage_pct` 0 and let the slicer's filament profile
  compensate shrinkage** (OrcaSlicer filament setting "Shrinkage (XY)").
- **Tile placement (2026-09-28):** `config.place_on_bed` centres the tile on
  the 256 mm bed when that clears the no-print zone (`BED_EXCLUDE`, from the
  CC2 profile) and otherwise shifts it the least distance that does, keeping
  0.5 mm from every edge; a 245 mm tile lands at (0.5, 5.5).  The offset is
  recorded per tile (`bed_offset_mm` in plan.json) and `validate()` rejects
  tiles that cannot be placed.  Files drop straight into the slicer.
- **Prime tower (2026-09-28):** no room beside a 245 mm tile, so
  `slicer.prime_tower: "auto"` slices with a copy of the process preset with
  `enable_prime_tower = "0"` unless one strip beside the *placed* tile is at
  least 35 mm wide (a centred 200 mm tile has only 28 mm per side).  From the
  CC2 profile: `purge_in_prime_tower = 0` and `change_filament_gcode` runs
  `M6211 … L[flush_length]`, i.e. the printer purges each change itself.
  OrcaSlicer's flush-into-infill / flush-into-object options only apply when
  a prime tower is enabled (PrintConfig tooltips; Print.cpp only calls
  `mark_wiping_extrusions` for wipe-tower layers), so without a tower all of
  the purge is the printer's.
- **Missing OSM layers fail the build (2026-09-28):** an Overpass error used
  to drop rivers silently (it happened on the first 245 mm Geneva build).
  `build_project` now raises when an enabled rivers/urban layer could not be
  fetched; the preview still degrades gracefully.  HTTP 502 is retried.
- **Slicer CLI (spike result, from the OrcaSlicer sources, not a live binary):**
  `--load-filament-ids` cannot be combined with a 3MF input, so the driver uses
  `--load-assemble-list` with per-band STLs (one filament per part, merged into
  one object, no re-arrangement) plus `--load-settings`, `--load-filaments`,
  `--slice 0`, `--export-3mf`, `--outputdir`; G-code appears as
  `plate_1.gcode`.  Manual-swap pauses are inserted into the G-code before the
  first layer at/above the planned height (`machine_pause_gcode` from the
  exported machine preset, else `M600`, the CC2 profile's pause command).  The 3MF also carries per-object
  `extruder` metadata and `custom_gcode_per_layer.xml` pauses for GUI use.
  **Still to verify on the real Elegoo fork:** run `my3dmaps slicer probe`, export
  machine/process/filament presets from the GUI, then `build --slice`.
- **Preview** is rendered client-side from the elevation + cell-class grid at
  96 nodes/tile, so the exaggeration slider is instant; tile walls show the slab
  colours.  Rivers appear in the preview because they are ordinary water-class
  cells (rasterised at ≥1 preview cell wide).
- **Liverpool framed print (user brief 2026-11-02):** frame window 30 × 40 cm
  (read as portrait, 300 wide × 400 tall), 11 mm deep; relief 35 × 45 cm so
  its edges hide 25 mm behind the frame on every side.  Implemented as
  2 × 2 **rectangular tiles** of 175 × 225 mm (`tile_mm_y`, new) at 1:50,000.
  The model is projected on the **OS National Grid** (`crs: "EPSG:27700"`,
  `center_xy` SJ 355 900, new) so the overlay's grid lines are straight and
  axis-aligned; the window is exactly E 328–343 km, N 380–400 km.  Height:
  `max_height_mm` 10.5 (0.5 mm under the frame depth), `base_mm` 2.0 (the
  minimum `validate()` allows) → auto exaggeration ≈ 3.3×.  Four filaments
  pinned to slots on every tile (`BandConfig.slot`, new): 1 glossy blue
  (water + base), 2 matte army dark green (lowland), 3 matte army beige
  (shore / docks below 8 m; slots 2/3 set by the user 2026-11-02 to match
  the loaded spools), 4 matte wood brown (above 45 m: the Everton–Woolton
  and Wirral sandstone ridges, ~20 % of land).  SRTM roof/tree noise made
  camouflage patterns at the 8 m threshold, so skin colours now come from a
  DEM smoothed by `palette.classify_smooth_m` (60 m) and islands under
  `palette.despeckle_mm2` (3 mm²) are repainted; slabs are unaffected.
- **River continuity (user report 2026-11-02, first Liverpool G-code):** the
  Wirral and Liverpool brooks printed as dashes and the Manchester Ship Canal
  merged into the Mersey.  Causes and fixes (`rivers.*`, Liverpool preset
  values in brackets): (1) culverts/tunnels were all skipped; they are now
  kept aside and drawn when a chain of them is ≤ `culvert_bridge_m` (300 m,
  road/rail crossings) or, with `culvert_join`, when every loose end meets a
  visible waterway, lake/sea or the model edge (the River Alt's 1.9 km
  culvert); a brook culverted all the way to its outfall stays hidden.
  (2) Holes in the OSM data (Clatter Brook stops 250 m short): a dangling
  *downstream* end (OSM draws waterways in flow direction) is joined to the
  nearest waterway within `gap_join_m` (300; default 0 = off).  (3)
  `min_gap_mm` 0.5 (≈ one extrusion line, instead of 1.2) so confluences and
  double-mapped streams no longer fill into blue wedges; `min_fragment_mm2`
  12 drops orphan dashes.  (4) `canal_bank_mm` (1.2; default 0): SRTM puts
  the Ship Canal's embankment at sea level, so canal cells near the shore are
  taken out of the sea mask and a land strip that wide is kept between canal
  and open water (painted by elevation band = beige); canal routes crossing
  open water (Leeds & Liverpool Canal out of Brunswick Dock) are left as
  water so they don't become piers.
- **Prime tower (2026-11-02, reverses the 245 mm "auto" outcome for this
  preset):** `slicer.prime_tower: "on"` makes `place_on_bed` choose a bed
  position that leaves a ≥ `prime_tower_room_mm` (35) strip — a 175 × 225 tile
  goes to x 0.5 and leaves a 78 mm strip on the right, clear of the no-print
  zone — and the process preset copy gets `wipe_tower_x/y` for it.
  `validate()` rejects "on" when the tile leaves no room.
- **Film overlay (2026-11-02):** `my3dmaps/overlay.py`, `overlay.enabled`.
  Black-only SVG, page = frame window, 1:1.  Contours from SRTM (public
  domain, 75 m smoothing, 10 m / 50 m index, labelled), names and symbols from
  OSM (ODbL; one Overpass query, cached), British National Grid 1 km lines
  numbered in the border + 100 km letters, seam triangles for aligning the film
  to the glued tile joints, title / scale bar / credits on the bottom edge.
  Masks (not white ink) keep the film clear under labels.  Long-distance ferry
  routes are dropped (they only trace the shipping channel).  Contour tracing
  is a small marching-squares in numpy (no skimage in the venv).
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
