"""Mesh construction and export.

Everything printable is built from one primitive, a *sheet*: a rectilinear
grid with a top height and a bottom height per node and a boolean mask of
cells that exist.  A sheet is meshed as a closed solid (top surface, bottom
surface, vertical walls where the footprint ends).  Where the top meets the
bottom (a slab cut by a horizontal plane tapering out along a contour) the
solid keeps a 10 µm minimum thickness instead of a knife edge, so every shell
is a genuine 2-manifold that slicers accept without repair.

Terrain slabs, surface skins and the keyed base plate are all sheets; a color
band's object is simply the concatenation of its sheets.
"""
from __future__ import annotations

import io
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

import numpy as np

MIN_THICKNESS = 0.01  # mm; below a layer height, invisible in the print


@dataclass
class Mesh:
    vertices: np.ndarray  # float64 [n, 3]
    faces: np.ndarray  # int64 [m, 3]

    @property
    def empty(self) -> bool:
        return len(self.faces) == 0

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if len(self.vertices) == 0:
            return np.zeros(3), np.zeros(3)
        return self.vertices.min(0), self.vertices.max(0)

    def translated(self, d) -> "Mesh":
        return Mesh(self.vertices + np.asarray(d, dtype=np.float64), self.faces)


def _flat_rects(flat: np.ndarray, zval: np.ndarray) -> list[tuple[int, int, int, int, float]]:
    """Greedy cover of the flat cells by axis-aligned rectangles of equal height.

    Returns (i0, i1, j0, j1, z) with inclusive cell ranges.
    """
    ny, nx = flat.shape
    todo = flat.copy()
    rects = []
    for j in range(ny):
        row = todo[j]
        i = 0
        while i < nx:
            if not row[i]:
                i += 1
                continue
            z = zval[j, i]
            i1 = i
            while i1 + 1 < nx and row[i1 + 1] and zval[j, i1 + 1] == z:
                i1 += 1
            j1 = j
            while j1 + 1 < ny and todo[j1 + 1, i:i1 + 1].all() and (zval[j1 + 1, i:i1 + 1] == z).all():
                j1 += 1
            todo[j:j1 + 1, i:i1 + 1] = False
            if i1 > i or j1 > j:  # a lone cell is already just two triangles
                rects.append((i, i1, j, j1, float(z)))
            i = i1 + 1
    return rects


def _fan_rects(rects, req: np.ndarray, nid: np.ndarray, xs: np.ndarray, ys: np.ndarray, base_id: int,
               reverse: bool) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Triangulate merged rectangles as fans around a centre vertex, keeping every
    *required* boundary node (nodes used by neighbouring faces or walls) so there
    are no T-junctions.  Returns (new centre vertices, faces)."""
    verts, faces = [], []
    for k, (i0, i1, j0, j1, z) in enumerate(rects):
        ring = []
        for i in range(i0, i1 + 2):  # south edge, west -> east
            if req[j0, i]:
                ring.append(nid[j0, i])
        for jj in range(j0 + 1, j1 + 2):  # east edge, south -> north
            if req[jj, i1 + 1]:
                ring.append(nid[jj, i1 + 1])
        for i in range(i1, i0 - 1, -1):  # north edge, east -> west
            if req[j1 + 1, i]:
                ring.append(nid[j1 + 1, i])
        for jj in range(j1, j0, -1):  # west edge, north -> south
            if req[jj, i0]:
                ring.append(nid[jj, i0])
        c = base_id + k
        verts.append((0.5 * (xs[i0] + xs[i1 + 1]), 0.5 * (ys[j0] + ys[j1 + 1]), z))
        r = np.asarray(ring, dtype=np.int64)
        nxt = np.roll(r, -1)
        f = np.column_stack([np.full(len(r), c, dtype=np.int64), r, nxt])
        faces.append(f[:, ::-1] if reverse else f)
    return verts, faces


def sheet_mesh(xs: np.ndarray, ys: np.ndarray, top: np.ndarray, bot: np.ndarray, cells: np.ndarray,
               merge_flat: bool = True) -> Mesh:
    """Closed solid between ``bot`` and ``top`` over the masked ``cells``.

    xs [nx], ys [ny] node coordinates (ys ascending), top/bot [ny, nx] heights,
    cells [ny-1, nx-1] bool.  Winding is counter-clockwise seen from outside.

    With ``merge_flat`` (lossless decimation) runs of cells whose top (or
    bottom) corners all sit at exactly the same height are covered by
    rectangles and each rectangle is triangulated as a fan around one centre
    vertex instead of two triangles per cell.  Slab bottoms, clipped slab tops
    and lake surfaces shrink by orders of magnitude; the geometry is identical.
    """
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    top = np.asarray(top, dtype=np.float64)
    bot = np.asarray(bot, dtype=np.float64)
    ny, nx = top.shape
    cells = np.asarray(cells, dtype=bool)
    if not cells.any():
        return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))

    top = np.maximum(top, bot + MIN_THICKNESS)
    N = ny * nx
    nid = np.arange(N, dtype=np.int64).reshape(ny, nx)
    X, Y = np.meshgrid(xs, ys)
    xy = np.column_stack([X.ravel(), Y.ravel()])
    # vertex blocks: 0 top, 1 bottom, 2 alt-top, 3 alt-bottom (alt copies split checkerboard corners)
    verts = np.concatenate([
        np.column_stack([xy, top.ravel()]), np.column_stack([xy, bot.ravel()]),
        np.column_stack([xy, top.ravel()]), np.column_stack([xy, bot.ravel()]),
    ])

    j, i = np.nonzero(cells)
    # corner nodes: a=(j,i) b=(j,i+1) c=(j+1,i+1) d=(j+1,i)
    A, B, C, D = nid[j, i], nid[j, i + 1], nid[j + 1, i + 1], nid[j + 1, i]

    # Checkerboard corners: two present cells meet only diagonally at a node.  Give the
    # south-western / south-eastern cell its own vertex copy there so the two solids do
    # not share a vertical edge (which would make the shell non-manifold).
    P = np.pad(cells, 1, constant_values=False)  # P[j+1, i+1] == cells[j, i]
    # node (j,i) has cells NE=cells[j,i], NW=cells[j,i-1], SW=cells[j-1,i-1], SE=cells[j-1,i]
    NE, NW, SW, SE = P[1:, 1:], P[1:, :-1], P[:-1, :-1], P[:-1, 1:]  # all [ny, nx]
    patA = NE & SW & ~NW & ~SE  # SW cell (j-1,i-1) uses alt id for its corner c = node (j,i)
    patB = NW & SE & ~NE & ~SW  # SE cell (j-1,i) uses alt id for its corner d = node (j,i)
    altC = patA[j + 1, i + 1]  # this cell is the SW cell of node (j+1,i+1)
    altD = patB[j + 1, i]      # this cell is the SE cell of node (j+1,i)
    C = np.where(altC, C + 2 * N, C)
    D = np.where(altD, D + 2 * N, D)
    At, Bt, Ct, Dt = A, B, C, D
    Ab, Bb, Cb, Db = A + N, B + N, C + N, D + N

    za, zb, zc, zd = top[j, i], top[j, i + 1], top[j + 1, i + 1], top[j + 1, i]
    diag_ac = (np.abs(za - zc) <= np.abs(zb - zd))[:, None]

    def tris(a, b, c, d, keep):
        t1 = np.where(diag_ac, np.column_stack([a, b, c]), np.column_stack([a, b, d]))
        t2 = np.where(diag_ac, np.column_stack([a, c, d]), np.column_stack([b, c, d]))
        return t1[keep], t2[keep]

    faces = []
    extra_verts: list[tuple[float, float, float]] = []
    keep_top = np.ones(len(j), dtype=bool)
    keep_bot = np.ones(len(j), dtype=bool)
    if merge_flat:
        alt_cell = altC | altD
        for surf, reverse in (("top", False), ("bot", True)):
            zz = top if surf == "top" else bot
            v0, v1, v2, v3 = zz[j, i], zz[j, i + 1], zz[j + 1, i + 1], zz[j + 1, i]
            flat_cells = (v0 == v1) & (v1 == v2) & (v2 == v3) & ~alt_cell
            if not flat_cells.any():
                continue
            flat = np.zeros(cells.shape, dtype=bool)
            flat[j[flat_cells], i[flat_cells]] = True
            zval = np.zeros(cells.shape)
            zval[j, i] = v0
            rects = _flat_rects(flat, zval)
            merged = np.zeros(cells.shape, dtype=bool)
            for i0, i1, j0, j1, _ in rects:
                merged[j0:j1 + 1, i0:i1 + 1] = True
            Pm = np.pad(merged, 1, constant_values=False)
            req = ~(Pm[1:, 1:] & Pm[1:, :-1] & Pm[:-1, :-1] & Pm[:-1, 1:])
            for i0, i1, j0, j1, _ in rects:
                req[j0, i0] = req[j0, i1 + 1] = req[j1 + 1, i1 + 1] = req[j1 + 1, i0] = True
            ids = nid if surf == "top" else nid + N
            nv, nf = _fan_rects(rects, req, ids, xs, ys, 4 * N + len(extra_verts), reverse)
            extra_verts.extend(nv)
            faces.extend(nf)
            keep = ~merged[j, i]
            if surf == "top":
                keep_top = keep
            else:
                keep_bot = keep

    t1, t2 = tris(At, Bt, Ct, Dt, keep_top)
    b1, b2 = tris(Ab, Bb, Cb, Db, keep_bot)
    faces += [t1, t2, b1[:, ::-1], b2[:, ::-1]]
    if extra_verts:
        verts = np.concatenate([verts, np.asarray(extra_verts, dtype=np.float64)])

    # walls where a masked cell borders an unmasked cell or the grid edge
    def wall(sel, ut, ub, vt, vb, order):
        q = {"ut": ut[sel], "ub": ub[sel], "vt": vt[sel], "vb": vb[sel]}
        if len(q["ut"]) == 0:
            return
        q0, q1, q2, q3 = (q[k] for k in order)
        faces.append(np.column_stack([q0, q1, q2]))
        faces.append(np.column_stack([q0, q2, q3]))

    south = ~P[:-2, 1:-1][j, i]
    north = ~P[2:, 1:-1][j, i]
    west = ~P[1:-1, :-2][j, i]
    east = ~P[1:-1, 2:][j, i]
    wall(south, At, Ab, Bt, Bb, ("ut", "ub", "vb", "vt"))  # -Y: u=a v=b
    wall(north, Dt, Db, Ct, Cb, ("ut", "vt", "vb", "ub"))  # +Y: u=d v=c
    wall(west, At, Ab, Dt, Db, ("ut", "vt", "vb", "ub"))   # -X: u=a v=d
    wall(east, Bt, Bb, Ct, Cb, ("ut", "ub", "vb", "vt"))   # +X: u=b v=c

    F = np.concatenate(faces).astype(np.int64)
    used, inv = np.unique(F.ravel(), return_inverse=True)
    return Mesh(verts[used], inv.reshape(-1, 3))


def concat(meshes: Iterable[Mesh]) -> Mesh:
    vs, fs, off = [], [], 0
    for m in meshes:
        if m.empty:
            continue
        vs.append(m.vertices)
        fs.append(m.faces + off)
        off += len(m.vertices)
    if not vs:
        return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    return Mesh(np.concatenate(vs), np.concatenate(fs))


def edge_manifold_report(m: Mesh) -> dict:
    """Count edges by how many faces use them (2 = closed manifold)."""
    f = m.faces
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    e_sorted = np.sort(e, axis=1)
    _, counts = np.unique(e_sorted, axis=0, return_counts=True)
    # orientation consistency: each undirected edge should appear once in each direction
    directed, dcounts = np.unique(e, axis=0, return_counts=True)
    return {
        "faces": int(len(f)),
        "edges": int(len(counts)),
        "boundary_edges": int((counts == 1).sum()),
        "nonmanifold_edges": int((counts > 2).sum()),
        "duplicate_directed_edges": int((dcounts > 1).sum()),
    }


def signed_volume(m: Mesh) -> float:
    v = m.vertices
    f = m.faces
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


# ---------------------------------------------------------------------------
# export

def write_stl(path: str | Path, m: Mesh, name: str = "my3dmaps") -> None:
    f = m.faces
    v = m.vertices.astype(np.float32)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1)
    n = n / np.where(ln > 0, ln, 1)[:, None]
    rec = np.zeros(len(f), dtype=np.dtype([("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3), ("c", "<f4", 3),
                                            ("attr", "<u2")]))
    rec["n"], rec["a"], rec["b"], rec["c"] = n, a, b, c
    header = name.encode()[:80].ljust(80, b"\0")
    with open(path, "wb") as fh:
        fh.write(header)
        fh.write(struct.pack("<I", len(f)))
        fh.write(rec.tobytes())


@dataclass
class MeshObject:
    name: str
    color: str  # hex
    mesh: Mesh
    extruder: int = 1  # 1-based slot (from the slot plan)
    band_ids: list[str] = field(default_factory=list)


@dataclass
class CustomGCodeItem:
    top_z: float
    type: int  # 0 ColorChange, 1 PausePrint, 2 ToolChange, 3 Template, 4 Custom
    extruder: int = 1
    color: str = ""
    extra: str = ""


def _vertices_xml(v: np.ndarray) -> str:
    buf = io.StringIO()
    np.savetxt(buf, v, fmt='<vertex x="%.3f" y="%.3f" z="%.3f"/>')
    return buf.getvalue()


def _triangles_xml(f: np.ndarray) -> str:
    buf = io.StringIO()
    np.savetxt(buf, f, fmt='<triangle v1="%d" v2="%d" v3="%d"/>')
    return buf.getvalue()


def write_3mf(path: str | Path, objects: list[MeshObject], title: str = "my3dmaps tile",
              custom_gcodes: list[CustomGCodeItem] | None = None, metadata: dict | None = None) -> None:
    """Write a multi-object 3MF (one object per color band).

    Standard 3MF core: each object references a base material carrying its
    display color.  In addition ``Metadata/model_settings.config`` records the
    per-object extruder the way OrcaSlicer/BambuStudio project files do, and
    ``Metadata/custom_gcode_per_layer.xml`` carries manual filament-swap
    pauses, so opening the file in OrcaSlicer restores the print plan.
    """
    objects = [o for o in objects if not o.mesh.empty]
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<model unit="millimeter" xml:lang="en-US" '
                 'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n')
    parts.append(f'<metadata name="Title">{escape(title)}</metadata>\n')
    parts.append('<metadata name="Application">my3dmaps</metadata>\n')
    for k, v in (metadata or {}).items():
        parts.append(f'<metadata name="{escape(str(k))}">{escape(str(v))}</metadata>\n')
    parts.append('<resources>\n<basematerials id="1">\n')
    for o in objects:
        parts.append(f'<base name="{escape(o.name)}" displaycolor="{o.color.upper()}FF"/>\n')
    parts.append('</basematerials>\n')
    for k, o in enumerate(objects):
        oid = k + 2
        parts.append(f'<object id="{oid}" name="{escape(o.name)}" type="model" pid="1" pindex="{k}">\n<mesh>\n<vertices>\n')
        parts.append(_vertices_xml(o.mesh.vertices))
        parts.append('</vertices>\n<triangles>\n')
        parts.append(_triangles_xml(o.mesh.faces))
        parts.append('</triangles>\n</mesh>\n</object>\n')
    parts.append('</resources>\n<build>\n')
    for k, o in enumerate(objects):
        parts.append(f'<item objectid="{k + 2}" printable="1"/>\n')
    parts.append('</build>\n</model>\n')
    model_xml = "".join(parts)

    # OrcaSlicer / BambuStudio per-object settings
    ms = ['<?xml version="1.0" encoding="UTF-8"?>\n<config>\n']
    for k, o in enumerate(objects):
        oid = k + 2
        ms.append(f'  <object id="{oid}">\n'
                  f'    <metadata key="name" value="{escape(o.name)}"/>\n'
                  f'    <metadata key="extruder" value="{o.extruder}"/>\n'
                  f'    <part id="{oid}" subtype="normal_part">\n'
                  f'      <metadata key="name" value="{escape(o.name)}"/>\n'
                  f'      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
                  f'      <metadata key="extruder" value="{o.extruder}"/>\n'
                  f'    </part>\n  </object>\n')
    ms.append('  <plate>\n    <metadata key="plater_id" value="1"/>\n    <metadata key="plater_name" value=""/>\n'
              '    <metadata key="locked" value="false"/>\n')
    for k, _ in enumerate(objects):
        ms.append(f'    <model_instance>\n      <metadata key="object_id" value="{k + 2}"/>\n'
                  f'      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="{k + 100}"/>\n'
                  '    </model_instance>\n')
    ms.append('  </plate>\n</config>\n')
    model_settings = "".join(ms)

    content_types = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
                     '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
                     '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
                     '<Default Extension="config" ContentType="text/xml"/>\n'
                     '<Default Extension="xml" ContentType="text/xml"/>\n'
                     '</Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
            '</Relationships>\n')

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("3D/3dmodel.model", model_xml)
        zf.writestr("Metadata/model_settings.config", model_settings)
        if custom_gcodes:
            zf.writestr("Metadata/custom_gcode_per_layer.xml", custom_gcode_xml(custom_gcodes))


def custom_gcode_xml(items: list[CustomGCodeItem]) -> str:
    out = ['<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer>\n<plate>\n<plate_info id="1"/>\n']
    for it in items:
        out.append(f'<layer top_z="{it.top_z:.3f}" type="{it.type}" extruder="{it.extruder}" '
                   f'color="{escape(it.color)}" extra="{escape(it.extra)}" gcode=""/>\n')
    out.append('<mode value="MultiAsSingle"/>\n</plate>\n</custom_gcodes_per_layer>\n')
    return "".join(out)


def write_glb(path: str | Path, objects: list[MeshObject]) -> None:
    """Minimal glTF binary with one flat-colored primitive per object (for previews)."""
    import json

    bin_parts: list[bytes] = []
    buffer_views, accessors, meshes, nodes, materials = [], [], [], [], []
    offset = 0

    def add_view(data: bytes, target: int):
        nonlocal offset
        pad = (-len(data)) % 4
        bin_parts.append(data + b"\0" * pad)
        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data), "target": target})
        offset += len(data) + pad
        return len(buffer_views) - 1

    for k, o in enumerate(objects):
        if o.mesh.empty:
            continue
        v = o.mesh.vertices.astype(np.float32)
        f = o.mesh.faces.astype(np.uint32)
        bv = add_view(v.tobytes(), 34962)
        accessors.append({"bufferView": bv, "componentType": 5126, "count": len(v), "type": "VEC3",
                          "min": v.min(0).tolist(), "max": v.max(0).tolist()})
        bi = add_view(f.tobytes(), 34963)
        accessors.append({"bufferView": bi, "componentType": 5125, "count": f.size, "type": "SCALAR"})
        r, g, b = (int(o.color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
        materials.append({"name": o.name, "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, 1.0],
                                                                    "metallicFactor": 0.0, "roughnessFactor": 0.9}})
        meshes.append({"name": o.name, "primitives": [{"attributes": {"POSITION": len(accessors) - 2},
                                                       "indices": len(accessors) - 1, "material": len(materials) - 1}]})
        nodes.append({"mesh": len(meshes) - 1, "name": o.name})

    gltf = {"asset": {"version": "2.0", "generator": "my3dmaps"}, "scene": 0,
            "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes,
            "materials": materials, "accessors": accessors, "bufferViews": buffer_views,
            "buffers": [{"byteLength": offset}]}
    js = json.dumps(gltf).encode()
    js += b" " * ((-len(js)) % 4)
    bin_blob = b"".join(bin_parts)
    total = 12 + 8 + len(js) + 8 + len(bin_blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A))
        fh.write(js)
        fh.write(struct.pack("<II", len(bin_blob), 0x004E4942))
        fh.write(bin_blob)
