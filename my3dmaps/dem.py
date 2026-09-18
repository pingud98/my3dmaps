"""Elevation data: NASA/USGS SRTM 1 Arc-Second Global (SRTMGL1, ~30 m).

SRTM is a US government work and is in the public domain (see SOURCES.md).
Tiles are fetched as ``.hgt`` files, cached locally and sampled bilinearly
onto the project's UTM node grid.  No GDAL/rasterio needed: an HGT tile is a
raw big-endian int16 array of 3601 x 3601 samples with the first row at the
tile's northern edge.

Sources (``ProjectConfig.dem_source``):
  * ``srtmgl1``       - ESA STEP public mirror of SRTMGL1 v3 (no login), with
                        NASA Earthdata as a fallback when EARTHDATA_USER /
                        EARTHDATA_PASS are set.
  * ``local:<dir>``   - a directory of ``NxxEyyy.hgt`` (or ``.hgt.zip``) files.

swisstopo swissALTI3D is deliberately *not* wired in by default; it is an
opt-in alternate with its own licence terms (see SOURCES.md).
"""
from __future__ import annotations

import io
import logging
import math
import os
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import requests
from scipy import ndimage

log = logging.getLogger(__name__)

HGT_N = 3601
VOID = -32768

ESA_STEP_URL = "https://step.esa.int/auxdata/dem/SRTMGL1/{name}.SRTMGL1.hgt.zip"
EARTHDATA_URL = "https://e4ftl01.cr.usgs.gov/MEASURES/SRTMGL1.003/2000.02.11/{name}.SRTMGL1.hgt.zip"


def cache_dir() -> Path:
    root = os.environ.get("MY3DMAPS_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "my3dmaps")
    p = Path(root) / "dem"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tile_name(lat: float, lon: float) -> str:
    """SRTM tile containing (lat, lon): named after its south-west corner."""
    la = int(math.floor(lat))
    lo = int(math.floor(lon))
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def tile_origin(name: str) -> tuple[int, int]:
    lat = int(name[1:3]) * (1 if name[0] == "N" else -1)
    lon = int(name[4:7]) * (1 if name[3] == "E" else -1)
    return lat, lon


def tiles_for_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[str]:
    names = []
    for la in range(int(math.floor(min_lat)), int(math.floor(max_lat)) + 1):
        for lo in range(int(math.floor(min_lon)), int(math.floor(max_lon)) + 1):
            names.append(tile_name(la + 0.5, lo + 0.5))
    return names


class TileMissing(Exception):
    """Tile does not exist at the source (open ocean, or outside 56S-60N)."""


def _download(url: str, auth=None, timeout=120) -> Optional[bytes]:
    sess = requests.Session()
    if auth:
        sess.auth = auth
    r = sess.get(url, timeout=timeout, allow_redirects=True)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def _unzip_hgt(blob: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for n in zf.namelist():
            if n.lower().endswith(".hgt"):
                return zf.read(n)
    raise ValueError("no .hgt inside zip")


def fetch_hgt(name: str, source: str = "srtmgl1", progress: Callable[[str], None] | None = None) -> Optional[Path]:
    """Return a local ``.hgt`` path for the tile, downloading on demand.

    Returns None when the tile does not exist at the source (all-ocean tiles
    are simply absent from SRTM; they are treated as elevation 0).
    """
    if source.startswith("local:"):
        d = Path(source[len("local:"):]).expanduser()
        for cand in (d / f"{name}.hgt", d / f"{name}.SRTMGL1.hgt", d / f"{name}.hgt.zip", d / f"{name}.SRTMGL1.hgt.zip"):
            if cand.exists():
                return cand
        raise FileNotFoundError(f"{name}: not found in {d}")

    cache = cache_dir()
    dst = cache / f"{name}.hgt"
    missing_marker = cache / f"{name}.missing"
    if dst.exists():
        return dst
    if missing_marker.exists():
        return None

    if progress:
        progress(f"downloading SRTM tile {name}")
    blob = None
    err: Exception | None = None
    try:
        blob = _download(ESA_STEP_URL.format(name=name))
    except Exception as e:  # network hiccup; try the fallback
        err = e
        log.warning("ESA STEP download failed for %s: %s", name, e)
    if blob is None and os.environ.get("EARTHDATA_USER"):
        try:
            blob = _download(EARTHDATA_URL.format(name=name),
                             auth=(os.environ["EARTHDATA_USER"], os.environ.get("EARTHDATA_PASS", "")))
        except Exception as e:
            err = e
            log.warning("Earthdata download failed for %s: %s", name, e)
    if blob is None:
        if err is not None:
            raise err
        missing_marker.write_text("404 at source; assumed ocean\n")
        return None
    dst.write_bytes(_unzip_hgt(blob))
    return dst


def load_hgt(path: Path) -> np.ndarray:
    if str(path).lower().endswith(".zip"):
        data = _unzip_hgt(path.read_bytes())
    else:
        data = path.read_bytes()
    n = int(round(math.sqrt(len(data) / 2)))
    arr = np.frombuffer(data, dtype=">i2").reshape(n, n)
    return arr


def sample_srtm(lon: np.ndarray, lat: np.ndarray, source: str = "srtmgl1",
                progress: Callable[[str], None] | None = None) -> tuple[np.ndarray, dict]:
    """Bilinearly sample SRTM at the given lon/lat arrays (same shape).

    Voids and missing tiles are filled (nearest valid sample / sea level).
    Returns (elevation_m float32, info dict).
    """
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    out = np.full(lon.shape, np.nan, dtype=np.float32)
    names = tiles_for_bbox(float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))
    used, missing = [], []
    for name in names:
        la0, lo0 = tile_origin(name)
        sel = (lat >= la0) & (lat < la0 + 1) & (lon >= lo0) & (lon < lo0 + 1)
        # include the exact northern/eastern edge for the outermost tiles
        sel |= (lat == la0 + 1) & (lon >= lo0) & (lon <= lo0 + 1) & (lat > lat.min())
        sel |= (lon == lo0 + 1) & (lat >= la0) & (lat <= la0 + 1) & (lon > lon.min())
        if not sel.any():
            continue
        path = fetch_hgt(name, source, progress)
        if path is None:
            out[sel] = 0.0  # open ocean
            missing.append(name)
            continue
        used.append(name)
        arr = load_hgt(path)
        n = arr.shape[0]
        res = n - 1  # samples per degree
        rows = (la0 + 1 - lat[sel]) * res
        cols = (lon[sel] - lo0) * res
        rows = np.clip(rows, 0, n - 1)
        cols = np.clip(cols, 0, n - 1)
        valid = (arr != VOID).astype(np.float32)
        arrf = np.where(arr == VOID, 0, arr).astype(np.float32)
        coords = np.vstack([rows, cols])
        z = ndimage.map_coordinates(arrf, coords, order=1, mode="nearest")
        v = ndimage.map_coordinates(valid, coords, order=1, mode="nearest")
        z = np.where(v > 0.999, z / np.maximum(v, 1e-6), np.nan)
        out[sel] = z.astype(np.float32)

    void = ~np.isfinite(out)
    n_void = int(void.sum())
    if n_void and n_void < out.size:
        idx = ndimage.distance_transform_edt(void, return_distances=False, return_indices=True)
        out = out[tuple(idx)]
    elif n_void:
        out[:] = 0.0
    info = {"tiles": used, "missing_tiles": missing, "void_samples_filled": n_void, "source": source,
            "dataset": "SRTMGL1 v3 (NASA/USGS, public domain)"}
    return out, info
