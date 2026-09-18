"""Local web app: Leaflet area/scale picker + Three.js colored preview + build jobs."""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .config import ProjectConfig
from .geo import tile_outlines_lonlat
from .pipeline import Terrain, analyze, build_project, prepare_terrain, preview_payload
from .slicer import probe

WEB_DIR = Path(__file__).parent / "web"
PREVIEW_NODES = 96


class PreviewRequest(BaseModel):
    config: dict[str, Any]
    nodes_per_tile: int = PREVIEW_NODES


class JobRequest(BaseModel):
    config: dict[str, Any]
    slice: bool = False
    pitch_mm: Optional[float] = None


class Job:
    def __init__(self, cfg: ProjectConfig, out_dir: Path, do_slice: bool):
        self.id = uuid.uuid4().hex[:10]
        self.cfg = cfg
        self.out_dir = out_dir
        self.do_slice = do_slice
        self.status = "queued"
        self.fraction = 0.0
        self.log: list[dict] = []
        self.summary: dict | None = None
        self.error: str | None = None
        self.created = time.time()

    def progress(self, stage: str, message: str, fraction):
        if fraction is not None:
            self.fraction = float(fraction)
        self.log.append({"t": round(time.time() - self.created, 1), "stage": stage, "message": message})

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "fraction": self.fraction, "log": self.log[-60:],
                "summary": self.summary, "error": self.error, "out_dir": str(self.out_dir),
                "name": self.cfg.name, "slice": self.do_slice}


def create_app(presets_dir: str | Path = "presets", out_dir: str | Path = "out") -> FastAPI:
    presets_path = Path(presets_dir)
    out_root = Path(out_dir)
    app = FastAPI(title="my3dmaps", version=__version__)
    jobs: dict[str, Job] = {}
    pool = ThreadPoolExecutor(max_workers=1)
    terrain_cache: dict[str, Terrain] = {}
    cache_lock = threading.Lock()

    def terrain_key(cfg: ProjectConfig, nodes: int) -> str:
        return json.dumps([round(cfg.center_lat, 6), round(cfg.center_lon, 6), cfg.scale, cfg.tiles_x, cfg.tiles_y,
                           cfg.tile_mm, cfg.dem_source, cfg.water, nodes,
                           cfg.rivers.enabled and sorted(cfg.rivers.active_classes(cfg.scale)),
                           cfg.urban.enabled and [sorted(cfg.urban.landuse), sorted(cfg.urban.roads)]])

    def get_terrain(cfg: ProjectConfig, nodes: int) -> Terrain:
        key = terrain_key(cfg, nodes)
        with cache_lock:
            t = terrain_cache.get(key)
        if t is None:
            t = prepare_terrain(cfg, nodes_per_tile=nodes)
            with cache_lock:
                if len(terrain_cache) > 8:
                    terrain_cache.pop(next(iter(terrain_cache)))
                terrain_cache[key] = t
        return t

    # ---- presets -----------------------------------------------------------
    @app.get("/api/presets")
    def list_presets():
        presets_path.mkdir(parents=True, exist_ok=True)
        out = []
        for p in sorted(presets_path.glob("*.json")):
            try:
                out.append({"file": p.stem, "config": ProjectConfig.load(p).to_dict()})
            except Exception as e:  # noqa: BLE001
                out.append({"file": p.stem, "error": str(e)})
        return out

    @app.get("/api/presets/{name}")
    def get_preset(name: str):
        p = presets_path / f"{_safe(name)}.json"
        if not p.exists():
            raise HTTPException(404, "no such preset")
        return ProjectConfig.load(p).to_dict()

    @app.post("/api/presets/{name}")
    def save_preset(name: str, config: dict[str, Any]):
        cfg = ProjectConfig.from_dict(config)
        problems = cfg.validate()
        if problems:
            raise HTTPException(400, "; ".join(problems))
        presets_path.mkdir(parents=True, exist_ok=True)
        cfg.save(presets_path / f"{_safe(name)}.json")
        return {"saved": name}

    @app.get("/api/default_config")
    def default_config():
        return ProjectConfig().to_dict()

    # ---- geometry / preview -------------------------------------------------
    @app.post("/api/grid")
    def grid(config: dict[str, Any]):
        cfg = ProjectConfig.from_dict(config)
        return {"tile_km": cfg.tile_m / 1000, "tiles": tile_outlines_lonlat(cfg), "pitch_m": cfg.pitch_m,
                "nodes_per_tile": cfg.nodes_per_tile}

    @app.post("/api/preview")
    def preview(req: PreviewRequest):
        cfg = ProjectConfig.from_dict(req.config)
        problems = cfg.validate()
        if problems:
            raise HTTPException(400, "; ".join(problems))
        nodes = max(24, min(240, req.nodes_per_tile))
        try:
            terrain = get_terrain(cfg, nodes)
            cls = analyze(cfg, terrain)
            return JSONResponse(preview_payload(cfg, terrain, cls))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"{type(e).__name__}: {e}")

    # ---- build jobs ---------------------------------------------------------
    @app.post("/api/jobs")
    def start_job(req: JobRequest):
        cfg = ProjectConfig.from_dict(req.config)
        if req.pitch_mm:
            cfg.pitch_mm = req.pitch_mm
        problems = cfg.validate()
        if problems:
            raise HTTPException(400, "; ".join(problems))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        job = Job(cfg, out_root / f"{_safe(cfg.name)}_{stamp}", req.slice)
        jobs[job.id] = job

        def run():
            job.status = "running"
            try:
                job.summary = build_project(cfg, job.out_dir, job.progress, do_slice=req.slice)
                job.status = "done"
            except Exception as e:  # noqa: BLE001
                job.error = f"{type(e).__name__}: {e}"
                job.status = "error"

        pool.submit(run)
        return job.to_dict()

    @app.get("/api/jobs")
    def list_jobs():
        return [j.to_dict() | {"log": []} for j in sorted(jobs.values(), key=lambda j: -j.created)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j.to_dict()

    @app.get("/api/jobs/{job_id}/files/{rel:path}")
    def job_file(job_id: str, rel: str):
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        root = j.out_dir.resolve()
        p = (root / rel).resolve()
        if root not in p.parents and p != root:
            raise HTTPException(400, "bad path")
        if not p.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(p, filename=p.name)

    @app.get("/api/slicer/probe")
    def slicer_probe(executable: Optional[str] = None):
        return probe(executable)

    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "-_.")[:64] or "project"


app = None  # populated lazily by ``uvicorn my3dmaps.server:get_app --factory``


def get_app() -> FastAPI:
    return create_app()
