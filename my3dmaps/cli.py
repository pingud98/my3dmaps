"""Command-line entry point: ``my3dmaps <command>``.

  my3dmaps build presets/geneva.json --out out/geneva [--slice] [--exaggeration 2]
  my3dmaps info  presets/geneva.json           # fetch data, report bands/relief/plan, no meshes
  my3dmaps grid  presets/geneva.json           # tile corner coordinates
  my3dmaps serve [--port 8000]                 # web UI
  my3dmaps slicer probe [--executable PATH]    # check the OrcaSlicer CLI surface
  my3dmaps slicer run out/geneva/tile_x0_y0    # (re)slice an already built tile
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import ProjectConfig


def _progress(stage: str, message: str, fraction):
    pct = f"{fraction * 100:5.1f}%" if fraction is not None else "      "
    print(f"{pct} [{stage}] {message}", flush=True)


def _load(path: str, args) -> ProjectConfig:
    cfg = ProjectConfig.load(path)
    if getattr(args, "exaggeration", None) is not None:
        cfg.exaggeration = args.exaggeration
    if getattr(args, "scale", None) is not None:
        cfg.scale = args.scale
    if getattr(args, "pitch", None) is not None:
        cfg.pitch_mm = args.pitch
    if getattr(args, "tiles", None):
        tx, ty = (int(v) for v in args.tiles.lower().split("x"))
        cfg.tiles_x, cfg.tiles_y = tx, ty
    if getattr(args, "no_rivers", False):
        cfg.rivers.enabled = False
    if getattr(args, "urban", None) is not None:
        cfg.urban.enabled = args.urban
    if getattr(args, "shrinkage", None) is not None:
        cfg.shrinkage_pct = args.shrinkage
    problems = cfg.validate()
    if problems:
        print("config problems: " + "; ".join(problems), file=sys.stderr)
        sys.exit(2)
    return cfg


def cmd_build(args):
    from .pipeline import build_project

    cfg = _load(args.project, args)
    summary = build_project(cfg, args.out, _progress, do_slice=args.slice)
    print()
    print(Path(args.out, "PRINT_PLAN.txt").read_text())
    if args.slice:
        for t in summary["tiles"]:
            s = t.get("slice", {})
            print(f"{t['name']}: slice {'ok' if s.get('ok') else 'FAILED'} - {s.get('message')} -> {s.get('gcode')}")


def cmd_info(args):
    from .pipeline import analyze, choose_exaggeration, prepare_terrain

    cfg = _load(args.project, args)
    terrain = prepare_terrain(cfg, _progress, nodes_per_tile=args.nodes)
    cls = analyze(cfg, terrain)
    ex, sug = choose_exaggeration(cfg, terrain)
    out = {"relief_m": terrain.relief_m, "elevation_min_m": float(terrain.elev.min()),
           "elevation_max_m": float(terrain.elev.max()), "exaggeration": ex, "suggested_exaggeration": sug,
           "dem": terrain.dem_info,
           "water": None if terrain.water is None else [b.__dict__ for b in terrain.water.bodies],
           "classification": cls.summary()}
    print(json.dumps(out, indent=2))


def cmd_grid(args):
    from .geo import tile_outlines_lonlat

    cfg = _load(args.project, args)
    print(json.dumps({"tile_km": cfg.tile_m / 1000, "tiles": tile_outlines_lonlat(cfg)}, indent=2))


def cmd_serve(args):
    import uvicorn

    from .server import create_app

    uvicorn.run(create_app(presets_dir=args.presets, out_dir=args.out), host=args.host, port=args.port, log_level="info")


def cmd_slicer_probe(args):
    from .slicer import probe

    print(json.dumps(probe(args.executable), indent=2))


def cmd_slicer_run(args):
    from .plan import Swap
    from .slicer import SliceInput, slice_tile

    tdir = Path(args.tile_dir)
    plan = json.loads((tdir / "plan.json").read_text())
    proj = ProjectConfig.load(args.project) if args.project else ProjectConfig.load(tdir.parent / "project.json")
    inputs = [SliceInput(tdir / f"{o['name']}.stl", o["extruder"]) for o in plan["objects"]]
    swaps = [Swap(**s) for s in plan["plan"]["swaps"]]
    res = slice_tile(proj.slicer, inputs, tdir / "sliced", plan["name"], swaps, dry_run=args.dry_run)
    print(json.dumps({"ok": res.ok, "message": res.message, "gcode": str(res.gcode_path) if res.gcode_path else None,
                      "command": res.command, "log": str(res.log_path)}, indent=2))


def main(argv=None):
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(prog="my3dmaps", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("project", help="project JSON (see presets/)")
        sp.add_argument("--exaggeration", type=float, help="override height multiplier")
        sp.add_argument("--scale", type=float, help="override scale denominator (e.g. 100000)")
        sp.add_argument("--tiles", help="override grid, e.g. 3x2")
        sp.add_argument("--pitch", type=float, help="override mesh pitch in mm")
        sp.add_argument("--no-rivers", action="store_true", help="skip OpenStreetMap rivers (keeps the model public-domain only)")
        sp.add_argument("--urban", dest="urban", action="store_true", default=None,
                        help="paint built-up areas and major roads (OpenStreetMap) dark grey")
        sp.add_argument("--no-urban", dest="urban", action="store_false", help="disable the urban layer")
        sp.add_argument("--shrinkage", type=float, help="override XY shrinkage compensation in percent (e.g. 0.3)")

    b = sub.add_parser("build", help="build 3MF/STL for every tile (optionally slice)")
    common(b)
    b.add_argument("--out", required=True)
    b.add_argument("--slice", action="store_true", help="also run OrcaSlicer headlessly")
    b.set_defaults(func=cmd_build)

    i = sub.add_parser("info", help="fetch data and report bands/relief without meshing")
    common(i)
    i.add_argument("--nodes", type=int, default=120, help="nodes per tile for the analysis grid")
    i.set_defaults(func=cmd_info)

    g = sub.add_parser("grid", help="print tile outlines (lat/lon)")
    common(g)
    g.set_defaults(func=cmd_grid)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--presets", default="presets")
    s.add_argument("--out", default="out")
    s.set_defaults(func=cmd_serve)

    sl = sub.add_parser("slicer", help="slicer utilities")
    slsub = sl.add_subparsers(dest="slcmd", required=True)
    pr = slsub.add_parser("probe", help="check the installed OrcaSlicer CLI")
    pr.add_argument("--executable")
    pr.set_defaults(func=cmd_slicer_probe)
    ru = slsub.add_parser("run", help="slice an already built tile directory")
    ru.add_argument("tile_dir")
    ru.add_argument("--project", help="project.json (default: <tile_dir>/../project.json)")
    ru.add_argument("--dry-run", action="store_true")
    ru.set_defaults(func=cmd_slicer_run)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
