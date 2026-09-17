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
- Tile edges get interlocking keying geometry (peg-and-socket or dovetail) so tiles physically register together without glue.
- Batch mode: given a region larger than one tile, auto-split into an N×M grid at the chosen scale and generate each tile as a separate job.
- Keep a safe max model height budget (e.g. ~180–200 mm) that leaves margin under the 256 mm ceiling once base thickness is accounted for.

## Data sources (must be genuinely reusable — prefer true public domain over merely "free")

- **Elevation (DEM):** NASA/USGS SRTM 1 Arc-Second Global (~30 m) as the default — it's a US government work and is public domain, and it covers both target areas (Alps and UK). Note as an optional higher-accuracy alternate for the Alps: swisstopo swissALTI3D (Swiss open data, free reuse) — flag clearly in code/docs that this is an *opt-in* alternate source with its own license terms, not the default.
- **Water bodies / coastline:** Natural Earth vector data (lakes, rivers, ocean/coastline) — explicitly public domain, good match for the licensing bar here. Use this to mask/flatten water rather than deriving it purely from DEM noise.
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
4. Fetch Natural Earth water vectors for the bbox; rasterize/clip to flatten water areas to a fixed level.
5. Classify terrain per-pixel into elevation/slope bands per the active palette; auto-prune inapplicable bands.
6. Generate heightmap → 3D mesh, scaled to the tile's physical footprint and Z-scaled by (real elevation × exaggeration multiplier), clamped to the safe height budget.
7. Segment the mesh into per-band sub-solids (with slight overlap at boundaries to avoid gaps/z-fighting), preserving elevation-ordering for the "load each color once" property.
8. Add tile edge keying geometry; ensure border elevations match neighboring tiles in a multi-tile job.
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

## Open questions / spikes for Fable to resolve early

- Exact OrcaSlicer (Elegoo fork) CLI surface for headless slicing + multi-material slot assignment — verify against the real installed version before building automation around assumed flags.
- Whether elevation+slope-only terrain classification is good enough for a dense urban area like Liverpool, or needs a cheap supplementary heuristic (e.g. flagging obviously built-up flat areas differently from natural lowland).
- Concrete mesh-segmentation approach for per-band solids that (a) avoids gaps/z-fighting at band boundaries and (b) keeps print time reasonable at tile scale.

## Stretch goals (not required for v1, worth keeping in mind)

- Printed base-plate legend (location name, scale, elevation range) as embossed/engraved text.
- Single-color "preview print" mode for quick physical drafts before committing filament to a full multi-color run.
- Saved project presets (bbox/scale/exaggeration/palette) so a named project like "Geneva" can be reloaded and regenerated after a tweak.
- Auto-suggested exaggeration multiplier based on measured relief of the selected area.

## Non-goals

- Not building a general-purpose GIS tool — scope is DEM-in, printable-landscape-out.
- Not targeting printers other than the Centauri Carbon 2 Combo for v1; keep the slicing-automation layer isolated enough that another printer/slicer could be swapped in later, but don't generalize prematurely.
