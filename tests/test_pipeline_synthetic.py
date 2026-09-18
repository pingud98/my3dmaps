import json

import numpy as np

from my3dmaps.classify import classify, suggest_exaggeration
from my3dmaps.config import PaletteConfig, ProjectConfig
from my3dmaps.mesh import edge_manifold_report, signed_volume
from my3dmaps.plan import ColorRange, plan_slots
from my3dmaps.tile import base_plate, build_tile


def test_classification_prunes_and_merges_to_four_colors(synth):
    cfg, grid, elev, water = synth
    cls = classify(cfg, grid, elev, water)
    s = cls.summary()
    assert len(s["distinct_colors"]) <= 4
    ids = [b["id"] for b in s["bands"]]
    assert ids[0] == "water"
    # elevation bands ascending, lowest is open-ended
    lowers = [b["lower_m"] for b in s["bands"][1:]]
    assert lowers[0] is None and lowers[1:] == sorted(lowers[1:])
    assert sum(b["fraction"] for b in s["bands"]) > 0.999


def test_water_cells_only_on_flat_water(synth):
    cfg, grid, elev, water = synth
    cls = classify(cfg, grid, elev, water)
    wi = cls.water_index
    ce = 0.25 * (elev[:-1, :-1] + elev[:-1, 1:] + elev[1:, :-1] + elev[1:, 1:])
    assert ce[cls.cells == wi].max() <= 372.0 + 1e-6


def test_no_water_drops_shore_band(synth):
    cfg, grid, elev, _ = synth
    cls = classify(cfg, grid, elev, None)
    assert "water" not in [b.id for b in cls.bands]
    assert any(p["id"] == "sand" for p in cls.pruned)


def test_swap_strategy_keeps_all_bands(synth):
    cfg, grid, elev, water = synth
    cfg.palette.overflow = "swap"
    cls = classify(cfg, grid, elev, water)
    assert len(cls.distinct_colors()) >= 5
    assert cls.merges == []


def test_tile_objects_are_closed_and_fit_height(synth):
    cfg, grid, elev, water = synth
    cls = classify(cfg, grid, elev, water)
    tb = build_tile(cfg, grid, cls, 1, 0, 2.0, float(elev.min()))
    assert tb.height_mm <= cfg.max_height_mm
    assert tb.plan.feasible
    total = 0.0
    for o in tb.objects:
        r = edge_manifold_report(o.mesh)
        assert r["boundary_edges"] == 0, (o.name, r)
        assert r["nonmanifold_edges"] == 0, (o.name, r)
        v = signed_volume(o.mesh)
        assert v > 0, o.name
        total += v
    # solid volume ~= integral of the model height over the footprint
    from my3dmaps.tile import z_of

    js, is_ = grid.tile_slices(1, 0)
    ez = z_of(elev[js, is_], cfg, float(elev.min()), 2.0)
    cell_h = 0.25 * (ez[:-1, :-1] + ez[:-1, 1:] + ez[1:, :-1] + ez[1:, 1:])
    expected = float(cell_h.sum() * cfg.pitch_mm ** 2)
    assert abs(total - expected) / expected < 0.02
    lo, hi = np.min([o.mesh.bounds()[0] for o in tb.objects], axis=0), np.max([o.mesh.bounds()[1] for o in tb.objects], axis=0)
    assert lo[2] == 0.0
    assert 20 < lo[0] < 40 and 216 < hi[0] < 236  # centred on the 256 mm bed


def test_shared_border_between_tiles(synth):
    cfg, grid, elev, water = synth
    cls = classify(cfg, grid, elev, water)
    a = build_tile(cfg, grid, cls, 0, 0, 2.0, float(elev.min()), center_on_bed=False)
    b = build_tile(cfg, grid, cls, 1, 0, 2.0, float(elev.min()), center_on_bed=False)
    va = np.concatenate([o.mesh.vertices for o in a.objects])
    vb = np.concatenate([o.mesh.vertices for o in b.objects])
    east = va[np.isclose(va[:, 0], cfg.tile_mm)]
    west = vb[np.isclose(vb[:, 0], 0.0)]
    key_a = {(round(y, 3), round(z, 3)) for _, y, z in east}
    key_b = {(round(y, 3), round(z, 3)) for _, y, z in west}
    # the terrain surface along the shared edge is identical on both tiles
    from my3dmaps.tile import z_of

    js, is_ = grid.tile_slices(0, 0)
    edge_elev = elev[js, is_.stop - 1]
    ys = np.linspace(0.0, cfg.tile_mm, len(edge_elev))
    surface = {(round(y, 3), round(float(z), 3)) for y, z in zip(ys, z_of(edge_elev, cfg, float(elev.min()), 2.0))}
    assert surface <= key_a and surface <= key_b


def test_base_plate_is_plain_square():
    cfg = ProjectConfig(tiles_x=2, tiles_y=2)
    m = base_plate(cfg)
    lo, hi = m.bounds()
    assert lo.tolist() == [0.0, 0.0, 0.0] and hi.tolist() == [cfg.tile_mm, cfg.tile_mm, cfg.base_mm]
    assert abs(signed_volume(m) - cfg.tile_mm ** 2 * cfg.base_mm) < 1e-6
    assert edge_manifold_report(m)["boundary_edges"] == 0


def test_shrinkage_compensation_scales_footprint(synth):
    cfg, grid, elev, water = synth
    cfg.shrinkage_pct = 0.3
    cls = classify(cfg, grid, elev, water)
    tb = build_tile(cfg, grid, cls, 0, 0, 2.0, float(elev.min()), center_on_bed=False)
    lo = np.min([o.mesh.bounds()[0] for o in tb.objects], axis=0)
    hi = np.max([o.mesh.bounds()[1] for o in tb.objects], axis=0)
    assert abs((hi[0] - lo[0]) - cfg.tile_mm * 1.003) < 1e-6
    assert abs((hi[1] - lo[1]) - cfg.tile_mm * 1.003) < 1e-6
    assert abs(tb.footprint_mm - 200.6) < 1e-9
    assert lo[2] == 0.0 and abs(hi[2] - tb.height_mm) < 1e-9  # Z untouched
    assert ProjectConfig(shrinkage_pct=40).validate()


def test_plan_slots_prefers_free_slots_then_swaps():
    ranges = [ColorRange("#1", ["base"], 0, 3), ColorRange("#2", ["a"], 3, 10), ColorRange("#3", ["b"], 3, 20),
              ColorRange("#4", ["c"], 15, 30), ColorRange("#5", ["d"], 25, 40)]
    p = plan_slots(ranges, 4)
    assert p.feasible and len(p.initial) == 4 and len(p.swaps) == 1
    assert p.swaps[0].slot == 1 and p.swaps[0].from_color == "#1" and p.swaps[0].to_color == "#5"
    p2 = plan_slots(ranges[:4], 4)
    assert p2.feasible and p2.swaps == []
    p3 = plan_slots([ColorRange(f"#{i}", [str(i)], 0, 10) for i in range(5)], 4)
    assert not p3.feasible


def test_suggest_exaggeration_bounds():
    cfg = ProjectConfig(scale=50000)
    assert suggest_exaggeration(cfg, 100.0) == 8.0  # flat -> max
    assert 1.0 <= suggest_exaggeration(cfg, 4000.0) <= 1.5  # mountains -> low


def test_config_roundtrip(tmp_path):
    cfg = ProjectConfig.load("presets/geneva.json")
    assert cfg.validate() == []
    cfg.save(tmp_path / "x.json")
    again = ProjectConfig.load(tmp_path / "x.json")
    assert again.to_dict() == cfg.to_dict()
    assert isinstance(again.palette, PaletteConfig)
    assert json.loads((tmp_path / "x.json").read_text())["palette"]["bands"][0]["id"] == "water"


def test_monochrome_draft_gives_one_object(synth):
    cfg, grid, elev, water = synth
    cfg.monochrome = True
    cls = classify(cfg, grid, elev, water)
    assert cls.distinct_colors() == ["#c8c8c8"]
    tb = build_tile(cfg, grid, cls, 0, 0, 2.0, float(elev.min()))
    assert len(tb.objects) == 1 and tb.plan.swaps == []
    assert edge_manifold_report(tb.objects[0].mesh)["boundary_edges"] == 0


def test_shore_rule_never_repaints_with_a_surface_band(synth):
    """Sand as the only elevation band above water: far-from-shore cells must not turn blue."""
    cfg, grid, elev, water = synth
    cfg.palette.bands = [b for b in cfg.palette.bands if b.id in ("water", "sand")]
    cls = classify(cfg, grid, elev, water)
    ce = 0.25 * (elev[:-1, :-1] + elev[:-1, 1:] + elev[1:, :-1] + elev[1:, 1:])
    assert (ce[cls.cells == cls.water_index] <= 372.0 + 1e-6).all()


def test_base_none_uses_lowest_elevation_band_not_overlay(synth):
    from shapely.geometry import Polygon
    from my3dmaps.urban import UrbanFeatures

    cfg, grid, elev, _ = synth
    cfg.palette.base_color = "none"
    cfg.urban.enabled = True
    cfg.palette.overflow = "swap"
    e0, e1, n0, n1 = grid.easting[0], grid.easting[-1], grid.northing[0], grid.northing[-1]
    town = Polygon([(e0, n0), (e0 + 0.3 * (e1 - e0), n0), (e0 + 0.3 * (e1 - e0), n0 + 0.3 * (n1 - n0)), (e0, n0 + 0.3 * (n1 - n0))])
    cls = classify(cfg, grid, elev, None, None, UrbanFeatures([], [("residential", town, None)]))
    assert cls.bands[0].kind == "urban"
    assert cls.base_color == cls.bands[cls.elev_band_indices[0]].color != cls.bands[0].color
