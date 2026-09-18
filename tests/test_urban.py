import numpy as np
import pytest
from shapely.geometry import LineString, Polygon

from my3dmaps.classify import classify
from my3dmaps.config import ProjectConfig
from my3dmaps.mesh import edge_manifold_report, signed_volume
from my3dmaps.rivers import RiverFeatures
from my3dmaps.tile import build_tile
from my3dmaps.urban import UrbanFeatures, overpass_query, parse_elements, rasterize_urban, road_width


def test_overpass_query_landuse_and_roads():
    q = overpass_query((5.5, 45.6, 7.5, 46.6), ["residential", "industrial"], ["motorway", "trunk"])
    assert '"landuse"~"^(residential|industrial)$"' in q and '"highway"~"^(motorway|trunk)$"' in q
    q2 = overpass_query((5.5, 45.6, 7.5, 46.6), ["residential"], [])
    assert "highway" not in q2


def test_road_width_from_tags():
    assert road_width({"width": "14 m"}) == 14.0
    assert road_width({"lanes": "4"}) == 14.0
    assert road_width({}) is None


def test_parse_elements_landuse_relation_and_roads():
    sq = [{"lon": 0, "lat": 0}, {"lon": 0.01, "lat": 0}, {"lon": 0.01, "lat": 0.01}, {"lon": 0, "lat": 0.01}, {"lon": 0, "lat": 0}]
    els = [
        {"type": "way", "id": 1, "tags": {"landuse": "residential"}, "geometry": sq},
        {"type": "way", "id": 2, "tags": {"landuse": "farmland"}, "geometry": sq},
        {"type": "way", "id": 3, "tags": {"highway": "motorway", "lanes": "3"}, "geometry": sq[:3]},
        {"type": "way", "id": 4, "tags": {"highway": "primary", "tunnel": "yes"}, "geometry": sq[:3]},
        {"type": "way", "id": 5, "tags": {"highway": "residential"}, "geometry": sq[:3]},
        {"type": "relation", "id": 6, "tags": {"landuse": "industrial", "type": "multipolygon"},
         "members": [{"type": "way", "role": "outer", "geometry": sq}]},
    ]
    feats = parse_elements(els, ["residential", "industrial"], ["motorway", "primary"])
    assert [(f.kind, f.is_area, f.osm_id) for f in feats] == [("residential", True, 1), ("motorway", False, 3), ("industrial", True, 6)]
    assert feats[1].width_m == 10.5


def _urban_synth(grid):
    """A town square in the east tile plus a motorway across the whole grid."""
    e0, e1 = grid.easting[0], grid.easting[-1]
    n0, n1 = grid.northing[0], grid.northing[-1]
    town = Polygon([(e0 + 0.6 * (e1 - e0), n0 + 0.2 * (n1 - n0)), (e0 + 0.8 * (e1 - e0), n0 + 0.2 * (n1 - n0)),
                    (e0 + 0.8 * (e1 - e0), n0 + 0.4 * (n1 - n0)), (e0 + 0.6 * (e1 - e0), n0 + 0.4 * (n1 - n0))])
    road = LineString([(e0 - 10, n0 + 0.7 * (n1 - n0)), (e1 + 10, n0 + 0.7 * (n1 - n0))])
    return UrbanFeatures([], [("residential", town, None), ("motorway", road, None)])


def test_urban_off_by_default_and_paints_when_enabled(synth):
    cfg, grid, elev, water = synth
    urban = _urban_synth(grid)
    cls0 = classify(cfg, grid, elev, water, None, urban)
    assert cls0.urban_index is None and cls0.urban_cells is None  # disabled -> ignored entirely
    cfg.urban.enabled = True
    cfg.palette.overflow = "swap"
    cls = classify(cfg, grid, elev, water, None, urban)
    ui = cls.urban_index
    assert ui is not None and cls.bands[ui].kind == "urban" and cls.bands[ui].color == "#4a4a4a"
    assert ui not in cls.elev_band_indices
    assert (cls.cells[cls.urban_cells] == ui).all()
    # the town square covers 0.2 x 0.2 of the grid, the road at least one cell row
    assert 0.03 < cls.summary()["urban_fraction"] < 0.08
    # never on the lake
    lake_cells = water.mask[:-1, :-1] & water.mask[1:, 1:] & water.mask[:-1, 1:] & water.mask[1:, :-1]
    assert not (cls.urban_cells & lake_cells).any()


def test_rivers_cross_towns(synth):
    cfg, grid, elev, water = synth
    cfg.urban.enabled = True
    cfg.palette.overflow = "swap"
    urban = _urban_synth(grid)
    y = grid.northing[0] + 0.3 * (grid.northing[-1] - grid.northing[0])
    river = RiverFeatures([], [("river", LineString([(grid.easting[0] - 10, y), (grid.easting[-1] + 10, y)]), None)])
    cls = classify(cfg, grid, elev, water, river, urban)
    assert (cls.cells[cls.river_cells] == cls.water_index).all()
    assert not (cls.urban_cells & cls.river_cells).any()


def test_dropping_urban_restores_terrain_classes(synth):
    cfg, grid, elev, water = synth
    urban = _urban_synth(grid)
    cfg.urban.enabled = True
    cfg.palette.overflow = "swap"
    cls = classify(cfg, grid, elev, water, None, urban)
    before = cls.cells.copy()
    ui = cls.urban_index
    ref = classify(cfg, grid, elev, water, None, None)  # what the terrain looks like without the overlay
    cls.merge_into(ui, cls.elev_band_indices[0], "test")
    assert cls.urban_index is None and cls.urban_cells is None
    assert cls.merges[-1]["from"] == "urban" and cls.merges[-1]["into"] == "terrain"
    # every formerly-urban cell is now the elevation band underneath; other cells only shifted index
    sel = before == ui
    assert set(np.unique(cls.cells[sel])) <= set(cls.elev_band_indices)
    assert [b.id for b in cls.bands] == [b.id for b in ref.bands]
    assert (cls.cells[~sel] == ref.cells[~sel]).mean() > 0.99


def test_urban_is_first_to_go_when_merging(synth):
    cfg, grid, elev, water = synth  # 6 colours + urban with overflow=merge -> urban merged away first
    cfg.urban.enabled = True
    urban = _urban_synth(grid)
    cls = classify(cfg, grid, elev, water, None, urban)
    assert len(cls.distinct_colors()) <= 4
    assert any(m["from"] == "urban" for m in cls.merges)


def test_urban_tile_is_closed_with_thin_skin(synth):
    cfg, grid, elev, water = synth
    cfg.urban.enabled = True
    cfg.palette.overflow = "swap"
    cls = classify(cfg, grid, elev, water, None, _urban_synth(grid))
    tb = build_tile(cfg, grid, cls, 1, 0, 2.0, float(elev.min()))
    names = [o.name for o in tb.objects]
    assert any("urban" in n for n in names)
    total = 0.0
    for o in tb.objects:
        r = edge_manifold_report(o.mesh)
        assert r["boundary_edges"] == 0 and r["nonmanifold_edges"] == 0, (o.name, r)
        total += signed_volume(o.mesh)
    from my3dmaps.tile import z_of

    js, is_ = grid.tile_slices(1, 0)
    ez = z_of(elev[js, is_], cfg, float(elev.min()), 2.0)
    cell_h = 0.25 * (ez[:-1, :-1] + ez[:-1, 1:] + ez[1:, :-1] + ez[1:, 1:])
    assert abs(total - float(cell_h.sum() * cfg.pitch_mm ** 2)) / total < 0.02
    urban_obj = next(o for o in tb.objects if "urban" in o.band_ids)
    # a thin sheet draped on the terrain: volume ~ area x depth (0.6 mm), not a slab down to the base
    ncells = cls.urban_cells[js.start:js.stop - 1, is_.start:is_.stop - 1].sum()
    per_cell = signed_volume(urban_obj.mesh) / (ncells * cfg.pitch_mm ** 2)
    assert 0.4 < per_cell < 1.0


def test_rasterize_urban_empty():
    cfg = ProjectConfig()
    from my3dmaps.geo import make_grid

    grid = make_grid(cfg, nodes_per_tile=10)
    assert not rasterize_urban(None, grid, cfg).any()


@pytest.mark.network
def test_liverpool_urban_from_osm():
    from my3dmaps.geo import make_grid
    from my3dmaps.urban import load_urban

    cfg = ProjectConfig.load("presets/liverpool.json")
    cfg.urban.enabled = True
    grid = make_grid(cfg, nodes_per_tile=40)
    u = load_urban(cfg, grid)
    assert u.error == "" and u.count > 100
    s = u.summary()
    assert s["built_up_km2"] > 50 and s["road_km"] > 20
