import json
from pathlib import Path

from my3dmaps.config import SlicerConfig
from my3dmaps.plan import Swap
from my3dmaps.slicer import SliceInput, insert_pauses, slice_tile, write_assemble_list


def test_assemble_list_shape(tmp_path):
    p = write_assemble_list(tmp_path / "a.json", [SliceInput(tmp_path / "w.stl", 1), SliceInput(tmp_path / "r.stl", 3)], "t")
    d = json.loads(p.read_text())
    plate = d["plates"][0]
    assert plate["need_arrange"] is False
    assert [o["filaments"] for o in plate["objects"]] == [[1], [3]]
    assert {o["assemble_index"][0] for o in plate["objects"]} == {1}


def test_insert_pauses_orca_and_prusa_markers(tmp_path):
    g = tmp_path / "in.gcode"
    g.write_text("; header\n; CHANGE_LAYER\n; Z_HEIGHT: 0.2\nG1 Z0.2\n; CHANGE_LAYER\n; Z_HEIGHT: 12.0\nG1 Z12\n"
                 ";LAYER_CHANGE\n;Z:12.2\nG1 Z12.2\n; CHANGE_LAYER\n; Z_HEIGHT: 30.0\nG1 Z30\n")
    out = tmp_path / "out.gcode"
    n = insert_pauses(g, out, [Swap(11.9, 1, "#1", "#2", ["a"], ["b"]), Swap(29.95, 2, "#3", "#4", ["c"], ["d"])], "M601")
    txt = out.read_text()
    assert n == 2
    assert txt.count("M601") == 2
    assert txt.index("M601") < txt.index("; Z_HEIGHT: 12.0")
    assert txt.index("; Z_HEIGHT: 12.0") < txt.index("M601", txt.index("M601") + 1) < txt.index("; Z_HEIGHT: 30.0")


def test_slice_tile_dry_run_builds_expected_command(tmp_path):
    cfg = SlicerConfig(executable="orca-slicer", machine_preset="m.json", process_preset="p.json",
                       filament_presets=["f1.json", "f2.json"])
    res = slice_tile(cfg, [SliceInput(tmp_path / "a.stl", 1), SliceInput(tmp_path / "b.stl", 2)], tmp_path / "s", "tile", dry_run=True)
    cmd = " ".join(res.command)
    assert "--load-settings m.json;p.json" in cmd
    assert "--load-filaments f1.json;f2.json" in cmd
    assert "--load-assemble-list" in cmd and "--slice 0" in cmd and "--export-3mf tile.3mf" in cmd
    assert Path(res.log_path).exists()
