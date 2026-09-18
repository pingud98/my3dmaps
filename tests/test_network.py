import pytest

from my3dmaps.config import ProjectConfig
from my3dmaps.pipeline import analyze, prepare_terrain

pytestmark = pytest.mark.network


def test_geneva_lake_detected():
    cfg = ProjectConfig(name="g", center_lat=46.4, center_lon=6.5, scale=200000, tiles_x=1, tiles_y=1)
    t = prepare_terrain(cfg, nodes_per_tile=60)
    assert t.water is not None and any(b.kind == "lake" and 365 < b.level_m < 376 for b in t.water.bodies)
    cls = analyze(cfg, t)
    assert "water" in [b.id for b in cls.bands]


def test_liverpool_ocean_and_pruning():
    cfg = ProjectConfig.load("presets/liverpool.json")
    t = prepare_terrain(cfg, nodes_per_tile=40)
    assert any(b.kind == "ocean" for b in t.water.bodies)
    cls = analyze(cfg, t)
    assert {p["id"] for p in cls.pruned} >= {"rock", "snow"}
