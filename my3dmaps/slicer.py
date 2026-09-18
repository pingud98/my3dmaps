"""Headless slicing with OrcaSlicer (Elegoo fork).

Verified against the OrcaSlicer sources (src/OrcaSlicer.cpp, PrintConfig.cpp
CLI*ConfigDef) rather than a live install - **run ``my3dmaps slicer probe``
against the real binary before trusting this**.  Relevant facts from the code:

* ``--load-filament-ids`` maps one filament id per *input file* and refuses
  3MF inputs, so the multi-object 3MF cannot carry slot assignment on the CLI.
* ``--load-assemble-list plates.json`` (BambuStudio lineage) loads several
  STLs into one plate, assigns a filament per STL and merges STLs that share
  an ``assemble_index`` into one multi-part object *without* rearranging them.
  That is exactly our per-band STL set, so it is the primary route.
* With ``--slice 0 --outputdir DIR`` the G-code lands in ``DIR/plate_1.gcode``
  and ``--export-3mf name.3mf`` writes the sliced project next to it.
* Machine/process/filament settings come from exported preset JSON files
  (``--load-settings "machine.json;process.json"`` and
  ``--load-filaments "f1.json;f2.json;..."``).  Export them from the slicer GUI
  once (printer: Elegoo Centauri Carbon 2, four loaded filaments) and point
  ``ProjectConfig.slicer`` at them.

Manual filament swaps (5th/6th color) are realised by inserting a pause
before the first layer at or above the planned height; the pause command is
taken from the machine preset's ``machine_pause_gcode`` when present.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import SlicerConfig
from .plan import Swap

CANDIDATE_NAMES = ["orca-slicer", "OrcaSlicer", "orcaslicer", "elegoo-slicer", "ElegooSlicer", "bambu-studio"]
REQUIRED_FLAGS = ["--load-settings", "--load-filaments", "--load-assemble-list", "--slice", "--export-3mf",
                  "--outputdir", "--debug"]
OPTIONAL_FLAGS = ["--load-filament-ids", "--load-custom-gcodes", "--export-stls", "--arrange", "--allow-newer-file"]


def find_executable(cfg: SlicerConfig | None = None) -> str | None:
    if cfg and cfg.executable:
        p = Path(cfg.executable).expanduser()
        return str(p) if p.exists() else shutil.which(cfg.executable)
    env = os.environ.get("ORCA_SLICER")
    if env and Path(env).expanduser().exists():
        return str(Path(env).expanduser())
    for name in CANDIDATE_NAMES:
        w = shutil.which(name)
        if w:
            return w
    for p in ["/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
              "/Applications/ElegooSlicer.app/Contents/MacOS/ElegooSlicer",
              "C:/Program Files/OrcaSlicer/orca-slicer.exe", "C:/Program Files/ELEGOO Slicer/elegoo-slicer.exe"]:
        if Path(p).exists():
            return p
    return None


def probe(executable: str | None = None, timeout: int = 60) -> dict:
    """Run ``--help`` and report which of the flags we rely on exist."""
    exe = executable or find_executable()
    if not exe:
        return {"found": False, "message": "No OrcaSlicer/ElegooSlicer executable found. Set ORCA_SLICER or "
                                           "ProjectConfig.slicer.executable."}
    try:
        r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=timeout)
        text = r.stdout + r.stderr
    except Exception as e:  # pragma: no cover - depends on the machine
        return {"found": True, "executable": exe, "error": str(e)}
    present = {f: (f in text) for f in REQUIRED_FLAGS + OPTIONAL_FLAGS}
    return {"found": True, "executable": exe, "flags": present,
            "ready": all(present[f] for f in REQUIRED_FLAGS),
            "missing_required": [f for f in REQUIRED_FLAGS if not present[f]],
            "help_excerpt": text[:4000]}


@dataclass
class SliceInput:
    stl_path: Path
    filament: int  # 1-based slot




@dataclass
class SliceResult:
    ok: bool
    command: list[str]
    returncode: int
    gcode_path: Path | None
    project_3mf: Path | None
    log_path: Path
    message: str = ""
    swaps_inserted: int = 0


def write_assemble_list(path: Path, inputs: list[SliceInput], plate_name: str) -> Path:
    plate = {
        "plate_name": plate_name,
        "need_arrange": False,
        "objects": [
            {"path": str(i.stl_path.resolve()), "count": 1, "filaments": [i.filament], "assemble_index": [1],
             "pos_x": [0.0], "pos_y": [0.0], "pos_z": [0.0]}
            for i in inputs
        ],
    }
    path.write_text(json.dumps({"plates": [plate]}, indent=2))
    return path


def machine_pause_gcode(cfg: SlicerConfig, default: str = "M601") -> str:
    if cfg.machine_preset and Path(cfg.machine_preset).exists():
        try:
            d = json.loads(Path(cfg.machine_preset).read_text())
            g = d.get("machine_pause_gcode")
            if isinstance(g, list):
                g = "\n".join(g)
            if g and g.strip():
                return g.strip()
        except Exception:
            pass
    return default


_Z_PATTERNS = [re.compile(r"^;\s*Z_HEIGHT:\s*([0-9.]+)"), re.compile(r"^;\s*Z:\s*([0-9.]+)")]
_LAYER_MARKERS = ("; CHANGE_LAYER", ";LAYER_CHANGE", "; LAYER_CHANGE")


def insert_pauses(gcode_in: Path, gcode_out: Path, swaps: list[Swap], pause_gcode: str) -> int:
    """Insert a pause before the first layer whose height >= each swap height."""
    pending = sorted(swaps, key=lambda s: s.z_mm)
    lines = gcode_in.read_text(errors="replace").splitlines(keepends=True)
    out: list[str] = []
    inserted = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        if pending and line.strip().startswith(_LAYER_MARKERS):
            # look ahead a few lines for the Z of this layer
            z = None
            for k in range(i, min(i + 6, len(lines))):
                for pat in _Z_PATTERNS:
                    m = pat.match(lines[k].strip())
                    if m:
                        z = float(m.group(1))
                        break
                if z is not None:
                    break
            if z is not None and z >= pending[0].z_mm - 1e-6:
                sw = pending.pop(0)
                out.append(f"; my3dmaps: manual filament swap in slot {sw.slot}: "
                           f"{'+'.join(sw.from_labels)} ({sw.from_color}) -> {'+'.join(sw.to_labels)} ({sw.to_color})\n")
                out.append(pause_gcode.rstrip("\n") + "\n")
                inserted += 1
        out.append(line)
        i += 1
    gcode_out.write_text("".join(out))
    return inserted


def slice_tile(cfg: SlicerConfig, inputs: list[SliceInput], out_dir: Path, plate_name: str,
               swaps: list[Swap] | None = None, dry_run: bool = False) -> SliceResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "slicer.log"
    exe = find_executable(cfg)
    assemble = write_assemble_list(out_dir / "assemble.json", inputs, plate_name)
    n_slots = max([i.filament for i in inputs] + [1])
    filaments = list(cfg.filament_presets)
    if filaments and len(filaments) < n_slots:
        filaments = filaments + [filaments[-1]] * (n_slots - len(filaments))
    cmd = [exe or "orca-slicer"]
    if cfg.machine_preset or cfg.process_preset:
        cmd += ["--load-settings", ";".join(p for p in (cfg.machine_preset, cfg.process_preset) if p)]
    if filaments:
        cmd += ["--load-filaments", ";".join(filaments)]
    cmd += ["--load-assemble-list", str(assemble), "--slice", "0", "--debug", "2",
            "--export-3mf", f"{plate_name}.3mf", "--outputdir", str(out_dir)]
    cmd += list(cfg.extra_args)
    if dry_run or not exe:
        msg = "dry run" if dry_run else "slicer executable not found (set ORCA_SLICER); command written to slicer.log"
        log_path.write_text(" ".join(cmd) + "\n")
        return SliceResult(False, cmd, -1, None, None, log_path, msg)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=cfg.timeout_s, cwd=str(out_dir))
    except subprocess.TimeoutExpired:
        log_path.write_text(" ".join(cmd) + "\n\nTIMEOUT\n")
        return SliceResult(False, cmd, -1, None, None, log_path, "slicer timed out")
    log_path.write_text(" ".join(cmd) + "\n\n" + r.stdout + "\n" + r.stderr)
    gcode = out_dir / "plate_1.gcode"
    project = out_dir / f"{plate_name}.3mf"
    if r.returncode != 0 or not gcode.exists():
        return SliceResult(False, cmd, r.returncode, gcode if gcode.exists() else None,
                           project if project.exists() else None, log_path,
                           f"slicer exited with {r.returncode}; see slicer.log")
    inserted = 0
    if swaps:
        final = out_dir / f"{plate_name}.gcode"
        inserted = insert_pauses(gcode, final, swaps, machine_pause_gcode(cfg))
        gcode = final
    else:
        final = out_dir / f"{plate_name}.gcode"
        shutil.copyfile(gcode, final)
        gcode = final
    return SliceResult(True, cmd, r.returncode, gcode, project if project.exists() else None, log_path,
                       "ok", inserted)
