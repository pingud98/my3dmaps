"""Rectangular tiles, OS grid projection, prime-tower placement, pinned slots,
skin despeckling and the film overlay (offline parts)."""
import json

import numpy as np
from shapely.geometry import LineString

from my3dmaps.classify import classify, despeckle_cells
from my3dmaps.config import BED_MM, ProjectConfig, place_on_bed, tower_strip
from my3dmaps.geo import make_grid, tile_outlines_lonlat
from my3dmaps.mesh import edge_manifold_report
from my3dmaps.overlay import contour_lines, os_square_letters, text_width
from my3dmaps.slicer import process_with_prime_tower, wants_prime_tower
from my3dmaps.tile import build_tile
from my3dmaps.water import WaterResult


def rect_cfg(**kw):
    d = dict(name="rect", crs="EPSG:27700", center_xy=[335500, 390000], scale=50000, tiles_x=2, tiles_y=2,
             tile_mm=175.0, tile_mm_y=225.0, pitch_mm=2.5, exaggeration=3.0, base_mm=2.0, max_height_mm=10.5)
    d.update(kw)
    return ProjectConfig.from_dict(d)


def test_rect_grid_on_os_grid():
    cfg = rect_cfg()
    g = make_grid(cfg)
    assert g.nodes_per_tile == 70 and g.nodes_per_tile_y == 90
    assert g.shape == (2 * 90 + 1, 2 * 70 + 1)
    assert "27700" in g.crs.to_string() or "British National Grid" in g.crs.name
    # model 350 x 450 mm at 1:50k = 17.5 x 22.5 km centred on the given grid point
    assert np.isclose(g.easting[0], 335500 - 8750) and np.isclose(g.easting[-1], 335500 + 8750)
    assert np.isclose(g.northing[0], 390000 - 11250) and np.isclose(g.northing[-1], 390000 + 11250)
    js, is_ = g.tile_slices(1, 1)
    assert (js.stop - js.start, is_.stop - is_.start) == (91, 71)
    outl = tile_outlines_lonlat(cfg)
    assert len(outl) == 4


def test_tower_placement_leaves_room():
    pos = place_on_bed(175, 225, 35)
    room, strip = tower_strip(*pos, 175, 225)
    assert room >= 35
    x0, y0, x1, y1 = strip
    assert x0 >= pos[0] + 175 and x1 <= BED_MM
    # the strip never enters the CC2 no-print zone (x 246-256, y 0-20)
    assert y0 >= 20 or x1 <= 246
    assert place_on_bed(245, 245, 35) == place_on_bed(245)  # no room: falls back to the plain placement
    s = rect_cfg().slicer
    s.prime_tower = "auto"
    assert wants_prime_tower(s, 175, 225) and not wants_prime_tower(s, 245, 245)


def test_validate_prime_tower_on():
    cfg = rect_cfg()
    cfg.slicer.prime_tower = "on"
    assert cfg.validate() == []
    cfg.tile_mm, cfg.tile_mm_y = 245, 245
    assert any("prime_tower" in p for p in cfg.validate())


def test_process_preset_gets_tower_position(tmp_path):
    src = tmp_path / "p.json"
    src.write_text(json.dumps({"name": "x", "enable_prime_tower": "0", "wipe_tower_x": ["165"], "wipe_tower_y": ["220"]}))
    out = process_with_prime_tower(str(src), True, tmp_path, (199.0, 98.0))
    d = json.loads(open(out).read())
    assert d["enable_prime_tower"] == "1" and d["wipe_tower_x"] == ["199.0"] and d["wipe_tower_y"] == ["98.0"]


def _rect_synth():
    cfg = rect_cfg()
    cfg.slicer.prime_tower = "on"
    cfg.palette.band("water").slot = 1
    cfg.palette.band("sand").slot = 2
    cfg.palette.band("lowland").slot = 3
    cfg.palette.band("upland").slot = 4
    cfg.palette.band("upland").from_m = 40
    grid = make_grid(cfg)
    ny, nx = grid.shape
    X, Y = np.meshgrid(np.linspace(0, 1, nx), np.linspace(0, 1, ny))
    elev = 80 * np.exp(-((X - 0.7) ** 2 + (Y - 0.6) ** 2) / 0.05) + 15 * X
    sea = X < 0.2
    elev[sea] = 0.0
    water = WaterResult(sea, np.where(sea, 0.0, np.nan).astype(np.float32), [], 0.0)
    return cfg, grid, elev, water


def test_rect_tile_build_closed_placed_and_pinned():
    cfg, grid, elev, water = _rect_synth()
    cls = classify(cfg, grid, elev, water)
    tb = build_tile(cfg, grid, cls, 1, 1, 3.0, 0.0)
    assert tb.height_mm <= cfg.max_height_mm + 1e-6
    for o in tb.objects:
        r = edge_manifold_report(o.mesh)
        assert r["boundary_edges"] == 0 and r["nonmanifold_edges"] == 0
    lo = np.min([o.mesh.vertices.min(axis=0) for o in tb.objects], axis=0)
    hi = np.max([o.mesh.vertices.max(axis=0) for o in tb.objects], axis=0)
    assert np.allclose(hi[:2] - lo[:2], [175, 225], atol=1e-6)
    assert np.allclose(lo[:2], tb.bed_offset_mm, atol=1e-6)
    assert tb.prime_tower_xy is not None and tb.prime_tower_xy[0] >= lo[0] + 175
    slots = {cls.bands[[b.id for b in cls.bands].index(bid)].color: s
             for bid, s in (("water", 1), ("lowland", 3), ("upland", 4)) if bid in [b.id for b in cls.bands]}
    for c, s in slots.items():
        if c in tb.plan.slots:
            assert tb.plan.slots[c] == s


def test_despeckle_repaints_small_islands():
    cells = np.full((40, 40), 2, dtype=np.int16)
    cells[:, 20:] = 3
    cells[10, 5] = 3  # single-cell island
    cells[30:33, 30:33] = 2  # 9-cell island
    cells[0:5, 0:5] = 0  # water: not editable, never touched
    out = despeckle_cells(cells, [2, 3], 4)
    assert out[10, 5] == 2
    assert (out[30:33, 30:33] == 2).all()  # 9 >= 4 cells, kept
    assert (out[0:5, 0:5] == 0).all()
    out2 = despeckle_cells(cells, [2, 3], 10)
    assert (out2[30:33, 30:33] == 3).all()


def test_contour_lines_circle():
    xs = np.linspace(-10, 10, 81)
    ys = np.linspace(-10, 10, 81)
    X, Y = np.meshgrid(xs, ys)
    z = 100 - (X ** 2 + Y ** 2)
    lines = contour_lines(z, xs, ys, 75.0)  # radius 5
    assert len(lines) == 1 and lines[0].is_ring
    r = np.hypot(*np.array(lines[0].coords).T)
    assert np.allclose(r, 5.0, atol=0.05)
    assert isinstance(lines[0], LineString)


def test_os_letters():
    assert os_square_letters(335500, 390000) == "SJ"
    assert os_square_letters(335500, 405000) == "SD"
    assert os_square_letters(651000, 313000) == "TG"
    assert os_square_letters(216000, 771000) == "NN"
    assert text_width("ab", 3) > text_width("a", 3)


def test_overlay_svg_offline(tmp_path, monkeypatch):
    import xml.etree.ElementTree as ET

    import my3dmaps.overlay as ov
    from pyproj import Transformer

    cfg, grid, elev, water = _rect_synth()
    cfg.overlay.enabled = True
    cfg.overlay.title = "TEST"
    to_ll = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform(335500, 390000)
    lon2, lat2 = to_ll.transform(337500, 392000)
    fake = {"elements": [
        {"type": "node", "lat": lat, "lon": lon, "tags": {"place": "city", "name": "Testville"}},
        {"type": "node", "lat": lat2, "lon": lon2, "tags": {"railway": "station", "name": "Halt"}},
        {"type": "way", "tags": {"railway": "rail"}, "geometry": [{"lat": lat, "lon": lon}, {"lat": lat2, "lon": lon2}]},
        {"type": "way", "tags": {"highway": "motorway"}, "geometry": [{"lat": lat2, "lon": lon}, {"lat": lat, "lon": lon2}]},
    ]}
    monkeypatch.setattr(ov, "fetch_overpass", lambda *a, **k: fake)
    res = ov.build_overlay(cfg, grid, elev, tmp_path / "o.svg", 3.0)
    root = ET.parse(tmp_path / "o.svg").getroot()
    assert root.get("width") == "300mm" and root.get("height") == "400mm"
    txt = (tmp_path / "o.svg").read_text()
    assert "TESTVILLE" in txt and "OpenStreetMap contributors" in txt and 'id="motorways"' in txt
    assert res.stats["window_grid_extent"] == [328000, 380000, 343000, 400000]
    assert res.stats["contour_lines"] > 0 and res.stats["grid_squares"] == ["SJ"]
    # black ink only
    import re
    colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,6})"', txt))
    assert colours <= {"#000", "#fff"}  # #fff only inside masks
    cls = classify(cfg, grid, elev, water)
    p = ov.write_topview_png(cfg, grid, cls, 3.0, tmp_path / "t.png")
    assert p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
