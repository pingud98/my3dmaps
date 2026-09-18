import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import LineString

from my3dmaps.classify import classify
from my3dmaps.config import ProjectConfig
from my3dmaps.mesh import edge_manifold_report, signed_volume
from my3dmaps.rivers import (RiverFeatures, overpass_query, parse_elements, parse_width, project_features,
                             rasterize_rivers)
from my3dmaps.tile import build_tile, skin_depth_nodes


def _lonlat_line(grid, y_frac=0.5):
    """A west-east river across the grid at a given row fraction, in lon/lat."""
    inv = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    y = grid.northing[0] + y_frac * (grid.northing[-1] - grid.northing[0])
    xs = np.linspace(grid.easting[0] - 500, grid.easting[-1] + 500, 12)
    lon, lat = inv.transform(xs, np.full_like(xs, y))
    return [{"lon": float(a), "lat": float(b)} for a, b in zip(lon, lat)]


def test_parse_width():
    assert parse_width("12") == 12.0
    assert parse_width("12.5 m") == 12.5
    assert parse_width("3-5") == 3.0
    assert parse_width("wide") is None and parse_width(None) is None and parse_width("0") is None


def test_overpass_query_classes_and_bbox():
    q = overpass_query((5.5, 45.6, 7.5, 46.6), ["river", "canal"])
    assert "[bbox:45.60000,5.50000,46.60000,7.50000]" in q
    assert '"waterway"~"^(river|canal)$"' in q and "stream" not in q
    assert 'relation["natural"="water"]' in q


def test_active_classes_gates_streams_by_scale():
    cfg = ProjectConfig()
    assert cfg.rivers.active_classes(50000) == ["river", "canal", "stream"]
    assert cfg.rivers.active_classes(250000) == ["river", "canal"]


def test_parse_elements_ways_areas_relations_and_tunnels():
    sq = [{"lon": 0, "lat": 0}, {"lon": 0.01, "lat": 0}, {"lon": 0.01, "lat": 0.01}, {"lon": 0, "lat": 0.01}, {"lon": 0, "lat": 0}]
    hole = [{"lon": 0.004, "lat": 0.004}, {"lon": 0.006, "lat": 0.004}, {"lon": 0.006, "lat": 0.006},
            {"lon": 0.004, "lat": 0.006}, {"lon": 0.004, "lat": 0.004}]
    els = [
        {"type": "way", "id": 1, "tags": {"waterway": "river", "name": "R", "width": "20"}, "geometry": sq[:3]},
        {"type": "way", "id": 2, "tags": {"waterway": "stream", "tunnel": "culvert"}, "geometry": sq[:3]},
        {"type": "way", "id": 3, "tags": {"natural": "water", "water": "river"}, "geometry": sq},
        {"type": "way", "id": 4, "tags": {"waterway": "riverbank"}, "geometry": sq},
        {"type": "way", "id": 5, "tags": {"waterway": "drain"}, "geometry": sq[:3]},
        {"type": "relation", "id": 6, "tags": {"natural": "water", "water": "canal", "type": "multipolygon"},
         "members": [{"type": "way", "role": "outer", "geometry": sq}, {"type": "way", "role": "inner", "geometry": hole}]},
    ]
    feats = parse_elements(els)
    kinds = [(f.kind, f.is_area, f.osm_id) for f in feats]
    assert kinds == [("river", False, 1), ("river", True, 3), ("river", True, 4), ("canal", True, 6)]
    assert feats[0].width_m == 20.0 and feats[0].name == "R"
    rel = feats[-1].geom
    assert rel.area == pytest.approx(0.01 * 0.01 - 0.002 * 0.002, rel=1e-6)


def test_rasterize_respects_minimum_printed_width(synth):
    cfg, grid, elev, water = synth  # pitch 2 mm on the print = 100 m on the ground at 1:50k
    els = [{"type": "way", "id": 1, "tags": {"waterway": "stream"}, "geometry": _lonlat_line(grid, 0.503)}]
    feats = parse_elements(els)
    proj = project_features(feats, grid, grid.bbox_lonlat())
    assert len(proj) == 1 and proj[0][0] == "stream"
    rivers = RiverFeatures(feats, proj)
    cells = rasterize_rivers(rivers, grid, cfg)
    rows = np.unique(np.nonzero(cells)[0])
    assert len(rows) >= 1  # a 5 m stream still gets a whole cell (>= 1.05 x pitch)
    assert cells.sum() >= cells.shape[1] * len(rows) * 0.95  # continuous across the grid
    # a wider minimum width widens the rasterised river accordingly
    cfg.rivers.min_width_mm = 6.0  # 300 m on the ground = 3 cells
    wide = rasterize_rivers(rivers, grid, cfg)
    assert len(np.unique(np.nonzero(wide)[0])) == 3
    assert rivers.length_km() > 0


def test_classify_paints_rivers_water_and_keeps_water_band(synth):
    cfg, grid, elev, water = synth
    line = LineString([(grid.easting[0] - 10, grid.northing[len(grid.northing) // 2] + 30),
                       (grid.easting[-1] + 10, grid.northing[len(grid.northing) // 2] + 30)])
    rivers = RiverFeatures([], [("river", line, None)])
    cls = classify(cfg, grid, elev, None, rivers)  # no lake at all
    assert cls.bands[0].is_water
    assert cls.river_cells is not None and cls.river_cells.any()
    assert (cls.cells[cls.river_cells] == cls.water_index).all()
    assert not any(p["id"] == "water" for p in cls.pruned)  # thin rivers never get pruned for small area
    assert cls.summary()["river_fraction"] < 0.05
    # the shore rule keys off lakes/sea only: no lake -> shore dropped even with rivers
    assert any(p["id"] == "sand" for p in cls.pruned)
    # with a lake, river cells inside the lake stay ordinary water (mask excludes them)
    cls2 = classify(cfg, grid, elev, water, rivers)
    assert not (cls2.river_cells & (water.mask[:-1, :-1] & water.mask[1:, 1:] & water.mask[:-1, 1:] & water.mask[1:, :-1])).any()
    cfg.rivers.enabled = False
    cls3 = classify(cfg, grid, elev, water, rivers)
    assert cls3.river_cells is None


def test_river_skin_is_thin_and_tile_stays_closed(synth):
    cfg, grid, elev, water = synth
    j = len(grid.northing) // 2
    line = LineString([(grid.easting[0] - 10, grid.northing[j] + 30), (grid.easting[-1] + 10, grid.northing[j] + 30)])
    rivers = RiverFeatures([], [("river", line, None)])
    cls = classify(cfg, grid, elev, water, rivers)
    js, is_ = grid.tile_slices(1, 0)
    rc = cls.river_cells[js.start:js.stop - 1, is_.start:is_.stop - 1]
    d = skin_depth_nodes(cfg.palette.skin_depth_mm, [(rc, cfg.rivers.depth_mm)], (js.stop - js.start, is_.stop - is_.start))
    assert d.min() == cfg.rivers.depth_mm and d.max() == cfg.palette.skin_depth_mm
    tb = build_tile(cfg, grid, cls, 1, 0, 2.0, float(elev.min()))
    total = 0.0
    for o in tb.objects:
        r = edge_manifold_report(o.mesh)
        assert r["boundary_edges"] == 0 and r["nonmanifold_edges"] == 0, (o.name, r)
        total += signed_volume(o.mesh)
    from my3dmaps.tile import z_of

    ez = z_of(elev[js, is_], cfg, float(elev.min()), 2.0)
    cell_h = 0.25 * (ez[:-1, :-1] + ez[:-1, 1:] + ez[1:, :-1] + ez[1:, 1:])
    assert abs(total - float(cell_h.sum() * cfg.pitch_mm ** 2)) / total < 0.02
    water_obj = next(o for o in tb.objects if "water" in o.band_ids)
    assert "base" in water_obj.band_ids  # blue base + lake + river = one object


@pytest.mark.network
def test_liverpool_rivers_from_osm():
    from my3dmaps.geo import make_grid
    from my3dmaps.rivers import load_rivers

    cfg = ProjectConfig.load("presets/liverpool.json")
    grid = make_grid(cfg, nodes_per_tile=40)
    r = load_rivers(cfg, grid)
    assert r.error == "" and r.count > 50
    names = {f.name for f in r.features}
    assert "River Alt" in names
    cells = rasterize_rivers(r, grid, cfg)
    assert cells.any()
