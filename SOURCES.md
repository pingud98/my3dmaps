# Data sources and licences

"Public domain, genuinely reusable" was a deliberate requirement of this
project.  Every dataset the pipeline touches is listed here with its licence
and the attribution we carry.  **Do not add a CC-BY (or more restrictive)
dataset without updating this file and flagging it in the UI.**

| Use | Dataset | Licence | Where in code |
|-----|---------|---------|---------------|
| Elevation (default) | **NASA/USGS SRTM 1 Arc-Second Global, v3 (SRTMGL1)**, ~30 m | US Government work – public domain. NASA asks for the citation below; no legal attribution requirement. | `my3dmaps/dem.py` |
| Water bodies, coastline | **Natural Earth 1:10m** `ne_10m_lakes`, `ne_10m_land` | Public domain (Natural Earth places all data in the public domain). | `my3dmaps/water.py` |
| Rivers, canals, streams (**opt-out**) | **OpenStreetMap** `waterway=river/canal/stream` lines and `natural=water` river/canal areas, via the Overpass API | **ODbL 1.0 – NOT public domain.** A printed model that contains them is an ODbL *produced work*: it may be made, sold and shared, but must be credited "© OpenStreetMap contributors" (and the extracted data itself, if you redistribute it, stays ODbL share-alike). Set `rivers.enabled=false` / untick "rivers" / `--no-rivers` for a strictly public-domain model. | `my3dmaps/rivers.py` |
| Built-up areas, major roads (**opt-in**, off by default) | **OpenStreetMap** `landuse=residential/industrial/commercial/retail` areas and `highway=motorway/trunk/primary` lines, via Overpass | ODbL, same terms and credit as the rivers above. | `my3dmaps/urban.py` |
| Web-map background (UI only) | OpenStreetMap standard tiles | © OpenStreetMap contributors, ODbL. Used only for on-screen area picking. | `my3dmaps/web/app.js` |

## SRTM details

* Product: SRTMGL1 v003, 1 arc-second, void-filled.  Coverage 60°N–56°S (both
  target areas – Alps and Liverpool – are inside).
* Fetched from the ESA STEP public mirror
  (`https://step.esa.int/auxdata/dem/SRTMGL1/`) which needs no login.  NASA
  Earthdata (`e4ftl01.cr.usgs.gov`) is used as a fallback when
  `EARTHDATA_USER` / `EARTHDATA_PASS` are set.  Files are cached under
  `~/.cache/my3dmaps/dem/` (override with `MY3DMAPS_CACHE`).
* Suggested citation: *NASA JPL (2013). NASA Shuttle Radar Topography Mission
  Global 1 arc second. NASA EOSDIS Land Processes DAAC.
  https://doi.org/10.5067/MEaSUREs/SRTM/SRTMGL1.003*
* Tiles that don't exist at the source are open ocean and are treated as 0 m.

## Natural Earth details

* Downloaded from the `naciscdn.org` mirror of naturalearthdata.com and cached
  under `~/.cache/my3dmaps/naturalearth/`.
* 1:10m vectors are coarse (hundreds of metres).  They are used only to *seed*
  water bodies; the actual shoreline is snapped to the flat surfaces SRTM v3
  already has for lakes and sea (see `water.py`).  Natural Earth's river
  layer only holds continental rivers, so rivers come from OpenStreetMap instead.

## OpenStreetMap rivers details

* Why OSM: it is the only global source with brook-level detail and river
  widths.  Public-domain alternatives were checked and rejected: Natural Earth
  rivers (too coarse – Rhône and Mersey only), HydroRIVERS/HydroSHEDS (CC-BY,
  ~500 m resolution, only catchments >10 km²), OS Open Rivers (OGL, UK only),
  swisstopo (Switzerland only).
* Fetched with one Overpass query per project bbox
  (`overpass-api.de`, fallback `overpass.kumi.systems`, override with
  `MY3DMAPS_OVERPASS_URL`), cached under `~/.cache/my3dmaps/osm/`.
  Overpass is a shared volunteer service: results are cached forever and
  streams are only requested at scales of 1:60,000 or larger, because a
  150 km Alpine box with every stream is more than it will serve.
* Tagged tunnels/culverts are skipped.  Width comes from the OSM `width` tag
  when present (rare), else 30 m river / 15 m canal / 5 m stream, and is then
  widened to the printable minimum (`rivers.min_width_mm`, default 1.2 mm).
* Attribution to carry with any model or image that includes rivers or the
  urban layer: **"© OpenStreetMap contributors, ODbL (openstreetmap.org/copyright)"**.
  The build writes this line into `PRINT_PLAN.txt`.
* The optional urban layer (`urban.enabled`, `--urban`) uses the same
  service and cache.  Its landuse query is heavy (≈40 MB for the 150 km
  Geneva box) and is only sent when the layer is switched on.

## Opt-in alternates (NOT wired in by default)

* **swisstopo swissALTI3D** (0.5–2 m, Switzerland only).  Swiss open
  government data, free reuse *with source attribution* ("© swisstopo").
  Would give far better Alpine detail than SRTM but is not public domain and
  only covers Switzerland; the Mont Blanc side is in France.  If added, it must
  be an explicit `dem_source` value and the attribution must be shown.
* **ESA WorldCover** land cover – CC-BY 4.0.  Deliberately *not* used;
  terrain type is derived from elevation + slope instead.
