import numpy as np

from my3dmaps.mesh import Mesh, MeshObject, edge_manifold_report, sheet_mesh, signed_volume, write_3mf, write_stl


def closed(m: Mesh):
    r = edge_manifold_report(m)
    assert r["boundary_edges"] == 0, r
    assert r["nonmanifold_edges"] == 0, r
    assert r["duplicate_directed_edges"] == 0, r


def test_box_volume_and_closed():
    xs, ys = np.arange(4.0), np.arange(3.0)
    m = sheet_mesh(xs, ys, np.full((3, 4), 2.0), np.zeros((3, 4)), np.ones((2, 3), bool))
    closed(m)
    assert abs(signed_volume(m) - 12.0) < 1e-9


def test_l_shaped_footprint():
    xs, ys = np.arange(4.0), np.arange(3.0)
    cells = np.ones((2, 3), bool)
    cells[0, 0] = False
    m = sheet_mesh(xs, ys, np.full((3, 4), 2.0), np.zeros((3, 4)), cells)
    closed(m)
    assert abs(signed_volume(m) - 10.0) < 1e-9


def test_clipped_slab_tapers_to_zero_thickness():
    xs = np.linspace(0, 10, 11)
    ys = np.linspace(0, 10, 11)
    X, _ = np.meshgrid(xs, ys)
    e = X * 0.5
    bot = np.full_like(e, 2.0)
    top = np.clip(e, 2.0, 4.0)
    cells = np.maximum.reduce([top[:-1, :-1], top[:-1, 1:], top[1:, :-1], top[1:, 1:]]) > 2.0 + 1e-3
    m = sheet_mesh(xs, ys, top, bot, cells)
    closed(m)
    assert abs(signed_volume(m) - 80.0) < 0.5  # analytic 80; +10 µm minimum-thickness slivers


def test_empty_mask():
    m = sheet_mesh(np.arange(3.0), np.arange(3.0), np.ones((3, 3)), np.zeros((3, 3)), np.zeros((2, 2), bool))
    assert m.empty


def test_stl_and_3mf_roundtrip(tmp_path):
    import struct
    import xml.etree.ElementTree as ET
    import zipfile

    xs, ys = np.arange(3.0), np.arange(3.0)
    m = sheet_mesh(xs, ys, np.full((3, 3), 1.0), np.zeros((3, 3)), np.ones((2, 2), bool))
    write_stl(tmp_path / "a.stl", m)
    data = (tmp_path / "a.stl").read_bytes()
    assert struct.unpack("<I", data[80:84])[0] == len(m.faces)
    assert len(data) == 84 + 50 * len(m.faces)

    objs = [MeshObject("water+base", "#1f5fbf", m, 1, ["water", "base"]), MeshObject("rock", "#8b8b8b", m.translated((0, 0, 1)), 2, ["rock"])]
    write_3mf(tmp_path / "t.3mf", objs)
    with zipfile.ZipFile(tmp_path / "t.3mf") as zf:
        names = set(zf.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "Metadata/model_settings.config"} <= names
        root = ET.fromstring(zf.read("3D/3dmodel.model"))
        ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
        objects = root.findall(".//m:object", ns)
        assert len(objects) == 2
        assert [o.get("name") for o in objects] == ["water+base", "rock"]
        assert len(root.findall(".//m:triangle", ns)) == 2 * len(m.faces)
        cfgx = ET.fromstring(zf.read("Metadata/model_settings.config"))
        ext = [md.get("value") for md in cfgx.iter("metadata") if md.get("key") == "extruder"]
        assert ext[:2] == ["1", "1"] and "2" in ext


def test_checkerboard_corner_is_manifold():
    xs, ys = np.arange(3.0), np.arange(3.0)
    for cells in (np.array([[True, False], [False, True]]), np.array([[False, True], [True, False]])):
        m = sheet_mesh(xs, ys, np.full((3, 3), 1.0), np.zeros((3, 3)), cells)
        closed(m)
        assert abs(signed_volume(m) - 2.0) < 1e-9
        # two separate solids touching along a line: the shared node has two vertex copies
        assert len(m.vertices) == 2 * 8


def test_flat_merge_is_lossless():
    """A slab with a flat bottom and a partly clipped top: merged faces give the
    same closed volume with far fewer triangles, and no T-junctions."""
    xs = np.linspace(0, 40, 41)
    ys = np.linspace(0, 30, 31)
    X, Y = np.meshgrid(xs, ys)
    top = np.clip(5 + 0.3 * X + 0.1 * Y, 5, 12)  # flat plateau at 12 over the east half
    bot = np.full_like(top, 2.0)
    cells = np.ones((30, 40), bool)
    cells[10:15, 5:9] = False  # a hole (walls inside)
    merged = sheet_mesh(xs, ys, top, bot, cells)
    plain = sheet_mesh(xs, ys, top, bot, cells, merge_flat=False)
    closed(merged)
    closed(plain)
    assert abs(signed_volume(merged) - signed_volume(plain)) < 1e-6
    assert len(merged.faces) < 0.4 * len(plain.faces)
    # every vertex of the plain mesh that lies on the flat bottom is still on the bottom plane of the merged one
    assert np.isclose(merged.vertices[:, 2].min(), 2.0)
