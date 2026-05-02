from bpy.types import Operator
from bpy.props import (
    StringProperty, BoolProperty, EnumProperty,
    FloatProperty, IntProperty
)
from bpy_extras.io_utils import ExportHelper
from .qt_mesh_writer import write_qt_quick3d_mesh
from .qt_mesh_validate import validate_qt_mesh
from .qt_bsdf_mat_importer import principled_bsdf_to_quick3d
from mathutils import Vector, Euler
from pathlib import Path
import re
import json
import struct
import math
import bmesh
import bpy
from mathutils import Matrix

bl_info = {
    "name": "Qt Quick 3D Balsam Exporter",
    "author": "Qt Balsam Exporter Plugin",
    "version": (2, 1, 0),
    "blender": (4, 4, 0),
    "location": "File > Export > Qt Quick 3D (.qml)",
    "description": "Export scene as Qt Quick 3D QML + native .mesh assets (no balsam needed)",
    "category": "Import-Export",
}


# ─────────────────────────────────────────────────────────────────
#  Qt .mesh binary format constants
#  Reverse-engineered from:
#    qtquick3d/src/utils/qssgmesh_p.h  (Qt 6.6, FILE_VERSION = 7)
#  @sa /opt/Qt/6.11.0/Src/qtquick3d/src/utils/qssgmesh.cpp
# ─────────────────────────────────────────────────────────────────

MESH_FILE_ID = 3365961549
MESH_FILE_VERSION = 7
MESH_FLAGS = 0
MULTI_MESH_FILE_ID = 555777497    # MultiMeshInfo::FILE_ID
MULTI_MESH_FILE_VERSION = 1
MESH_DATA_FILE_ID = 3365961549   # MeshDataHeader::FILE_ID
# latest (supports morph split, LODs, lightmaps)
MESH_DATA_FILE_VERSION = 7

COMPONENT_TYPE_UNSIGNED_INT16 = 4
COMPONENT_TYPE_UNSIGNED_INT32 = 6
COMPONENT_TYPE_FLOAT32 = 10

DRAW_MODE_TRIANGLES = 7
DRAW_MODE_LINES = 4

WINDING_CLOCKWISE = 2
WINDING_COUNTER_CLOCKWISE = 2

MESH_HEADER_SIZE = 12

##########


# QSSGRenderComponentType enum values
COMP_UINT8 = 1
COMP_UINT16 = 3
COMP_UINT32 = 5
COMP_UINT64 = 7
COMP_FLOAT32 = 10

LE = '<'   # all little-endian (x86/ARM default)

# Standard Qt Quick 3D vertex attribute names (must match exactly)
ATTR_POSITION = b"attr_pos"
ATTR_NORMAL = b"attr_norm"
ATTR_UV0 = b"attr_uv0"
ATTR_UV1 = b"attr_uv1"
ATTR_TANGENT = b"attr_textan"
ATTR_BINORMAL = b"attr_binormal"
ATTR_COLOR = b"attr_color"
ATTR_JOINTS = b"attr_joints"
ATTR_WEIGHTS = b"attr_weights"


# ══════════════════════════════════════════════════════════════════════════════
#  Qt .mesh format constants  (mirrors qssgmesh_p.h / qssgmesh.cpp)
# ══════════════════════════════════════════════════════════════════════════════

MULTI_HEADER_STRUCT_SIZE = 16
MULTI_ENTRY_STRUCT_SIZE = 16
MESH_HEADER_STRUCT_SIZE = 12
MESH_STRUCT_SIZE = 56
VERTEX_BUFFER_ENTRY_STRUCT_SIZE = 16
SUBSET_STRUCT_SIZE_V6 = 52

_PAD4 = b"\x00\x00\x00\x00"
# ─────────────────────────────────────────────────────────────────
#  Low-level pack helpers
# ─────────────────────────────────────────────────────────────────


def _u32(v): return struct.pack(LE + 'I', int(v))
def _u16(v): return struct.pack(LE + 'H', int(v))
def _u64(v): return struct.pack(LE + 'Q', int(v))
def _f32(v): return struct.pack(LE + 'f', float(v))
def _utf16(s): return s.encode('utf-16le')


AXIS_FIX = Matrix((
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
))


def blender_local_matrix(obj):
    if obj.parent:
        return obj.parent.matrix_world.inverted() @ obj.matrix_world
    return obj.matrix_world.copy()


def qt_local_trs(obj, convert_coords=True):
    m = blender_local_matrix(obj)
    if convert_coords:
        m = AXIS_FIX @ m @ AXIS_FIX.inverted()

    loc, rot, scale = m.decompose()
    eul = rot.to_euler('XYZ')
    return (
        (loc.x, loc.y, loc.z),
        (math.degrees(eul.x), math.degrees(eul.y), math.degrees(eul.z)),
        (scale.x, scale.y, scale.z),
    )


#####
# ══════════════════════════════════════════════════════════════════════════════
#  OffsetTracker  (mirrors MeshInternal::MeshOffsetTracker)
# ══════════════════════════════════════════════════════════════════════════════

class _OffsetTracker:
    def __init__(self):
        self.counter = 0

    def advance(self, n: int):
        self.counter += n

    def aligned_advance(self, n: int) -> int:
        self.counter += n
        pad = 4 - (self.counter % 4)
        self.counter += pad
        return pad


def _pad(n: int) -> bytes:
    return _PAD4[:n]


# ─────────────────────────────────────────────────────────────────
#  Common helpers
# ─────────────────────────────────────────────────────────────────


def sanitize(name):
    s = re.sub(r'[^A-Za-z0-9_]', '_', name)
    return ('_' + s if s and s[0].isdigit() else s) or '_'


def qt_pos(value):
    """Blender Z-up → Qt Y-up coordinate conversion."""
    """Blender (X right, Y fwd, Z up)  →  Qt Quick 3D (X right, Y up, -Z fwd)"""

    value_ = tuple(value)
    return (value_[0], value_[2], -value_[1])


def qt_scale(value):
    value_ = tuple(value)
    return (value_[0], value_[2], -value_[1])


def qt_rot(e): return (math.degrees(e.x),
                       math.degrees(e.z),
                       math.degrees(-e.y))


def rgba3(c): return f"Qt.rgba({c[0]:.4f}, {c[1]:.4f}, {c[2]:.4f}, 1.0)"


def rgba4(c):
    a = c[3] if len(c) > 3 else 1.0
    return f"Qt.rgba({c[0]:.4f}, {c[1]:.4f}, {c[2]:.4f}, {a:.4f})"


def align4(n):
    return (n + 3) & ~3


# ══════════════════════════════════════════════════════════════════════════════
#  Mesh geometry extraction from Blender
# ══════════════════════════════════════════════════════════════════════════════


def collect_mesh(obj, convert_coords):  # , depsgraph, apply_modifiers=True):
    # eval_obj = obj.evaluated_get(depsgraph) if apply_modifiers else obj
    mesh = obj.to_mesh()
    mesh.calc_loop_triangles()

    color_attr = None
    if hasattr(mesh, "color_attributes") and mesh.color_attributes:
        preferred = [a for a in mesh.color_attributes if getattr(
            a, "domain", None) == 'CORNER']
        color_attr = preferred[0] if preferred else mesh.color_attributes.active_color
        if color_attr and getattr(color_attr, "domain", None) != 'CORNER':
            color_attr = None
    elif hasattr(mesh, "vertex_colors") and mesh.vertex_colors:
        color_attr = mesh.vertex_colors[0]

    has_color = color_attr is not None
    uv_layer = mesh.uv_layers.active.data if mesh.uv_layers.active else None
    has_uv1 = len(mesh.uv_layers) > 1
    uv1_layer = mesh.uv_layers[1].data if has_uv1 else None
    col_layer = color_attr.data if color_attr else None
    has_tangent = False  # uv_layer != None

    vbuf = bytearray()
    vmap = {}
    vertices = []
    indices = []
    bounds_min = [math.inf, math.inf, math.inf]
    bounds_max = [-math.inf, -math.inf, -math.inf]

    # ── Group faces by material index ─────────────────────────────
    mat_face_groups = {}
    for tris in mesh.loop_triangles:
        mat_face_groups.setdefault(tris.material_index, []).append(tris)

    subsets_data = []

    for mat_idx in sorted(mat_face_groups.keys()):
        polys = mat_face_groups[mat_idx]
        subset_start = len(indices)
        bmin = [math.inf, math.inf, math.inf]
        bmax = [-math.inf, -math.inf, -math.inf]

        for tri in polys:
            tri_split_normals = getattr(tri, "split_normals", None)
            for corner, loop_index in enumerate(tri.loops):
                loop = mesh.loops[loop_index]
                vert = mesh.vertices[loop.vertex_index]
                pos = tuple(vert.co)
                norm = tuple(loop.normal)

                if tri_split_normals:
                    norm = tuple(tri_split_normals[corner])
                elif hasattr(loop, "normal"):
                    norm = tuple(loop.normal)
                else:
                    norm = tuple(vert.normal)

                uv = tuple(uv_layer[loop_index].uv) if uv_layer else (0.0, 0.0)

                if convert_coords:
                    pos = qt_pos(pos)
                    norm = qt_pos(norm)

                vdata = pos + norm + uv

                if has_uv1:
                    uv1 = tuple(uv1_layer[loop_index].uv)
                    uv1 = (uv1[0], 1.0 - uv1[1])  # flip V
                    vdata += uv1

                if has_tangent:
                    tan = tuple(loop.tangent)
                    tan = qt_pos(tan)
                    vdata += tan

                if has_color:
                    col_ = tuple(col_layer[loop_index].color)
                    if len(col_) >= 4:
                        col_ = (col_[0], col_[1], col_[2], col_[3])
                    else:
                        col_ = (col_[0], col_[1], col_[2], 1.0)

                    vdata += col_

                key = tuple(round(x, 6) for x in vdata)
                idx = vmap.get(key)
                if idx is None:
                    idx = len(vertices)
                    vmap[key] = idx

                    vbuf.extend(struct.pack("<3f", *pos))
                    vbuf.extend(struct.pack("<3f", *norm))

                    if uv_layer:
                        vbuf.extend(struct.pack("<2f", *uv))

                    if has_uv1:
                        vbuf.extend(struct.pack("<2f", *uv1))

                    if has_tangent:
                        vbuf.extend(struct.pack("<3f", *tan))

                    if has_color:
                        vbuf.extend(struct.pack("<4f", *col_))

                    vertices.append({
                        "pos": pos,
                        "norm": norm,
                        "uv": uv,
                    })

                    for i in range(3):
                        bounds_min[i] = min(bounds_min[i], pos[i])
                        bounds_max[i] = max(bounds_max[i], pos[i])

                indices.append(idx)
                for i in range(3):
                    bmin[i] = min(bmin[i], pos[i])
                    bmax[i] = max(bmax[i], pos[i])

        icount = len(indices) - subset_start
        mat = (obj.material_slots[mat_idx].material if mat_idx < len(
            obj.material_slots) else None)
        sname = mat.name if mat else f"subset_{mat_idx}"
        subsets_data.append({'sname': sname, 'icount': icount,
                            'subset_start': subset_start, 'bmin': tuple(bmin), 'bmax': tuple(bmax)})

    material_name = mesh.materials[0].name if mesh.materials and mesh.materials[0] else ""
    has_uv0 = mesh.uv_layers.active is not None
    has_normals = len(vertices) > 0

    return {'vertices': bytes(vbuf),
            'vertex_count': len(vertices),
            'indices': indices,
            'has_normals': has_normals,
            'has_uv0': has_uv0,
            'has_tangent': has_tangent,
            # 'bounds_min': tuple(bounds_min),
            # 'bounds_max': tuple(bounds_max),
            'material_name': material_name,
            'has_uv1': has_uv1,
            'has_color': has_color,
            'subsets_data': subsets_data
            }


def extract_mesh_data(obj, apply_modifiers: bool, convert_coords: bool) -> dict:
    """
    Triangulate the mesh and build vertex / index buffers.

    Returns a dict with keys:
        entries, stride, vbuf, ibuf, index_type, index_count, vertex_count
    """
    # ── Get evaluated (modifier-applied) or raw mesh ──────────────────────
    depsgraph = bpy.context.evaluated_depsgraph_get()
    eval_obj = obj.evaluated_get(depsgraph) if apply_modifiers else obj

    mesh_ = collect_mesh(eval_obj, convert_coords)
    '''
    if apply_modifiers:
        eval_obj = obj.evaluated_get(depsgraph)
        me = eval_obj.to_mesh()
    else:
        me = obj.to_mesh()
    '''
    '''
    me = eval_obj.to_mesh()

    # ── Triangulate via bmesh ─────────────────────────────────────────────
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])

    uv_layer = bm.loops.layers.uv.active
    has_uvs = uv_layer is not None
    has_norms = True  # Blender always has normals after calc_normals_split()

    # me.calc_normals_split()
    # Build a map from loop to split normal
    split_normals = {}
    for poly in me.polygons:
        for li in poly.loop_indices:
            loop = me.loops[li]
            split_normals[loop.index] = tuple(loop.normal)
    '''
    # ── Compute attribute offsets ─────────────────────────────────────────
    entries = []
    offset = 0

    def add_attr(name, ncomp):
        nonlocal offset
        entries.append({"name": name, "type": COMP_FLOAT32,
                       "count": ncomp, "offset": offset})
        # entries.append((name, COMP_FLOAT32, ncomp, offset))
        offset += ncomp * 4

    add_attr(ATTR_POSITION, 3)
    add_attr(ATTR_NORMAL,   3)

    has_uv = mesh_['has_uv0']
    if has_uv:
        add_attr(ATTR_UV0, 2)

    has_uv1 = mesh_['has_uv1']
    if has_uv1:
        add_attr(ATTR_UV1, 2)

    has_tangent = mesh_['has_tangent']
    if has_tangent:
        add_attr(ATTR_TANGENT, 3)

    has_color = mesh_['has_color']
    if has_color:
        add_attr(ATTR_COLOR, 3)

    indices_ = mesh_['indices']
    idx_count_ = len(indices_)
    # idx_pref_, idx_comp_type_ = ('B', COMP_UINT8)
    idx_pref_, idx_comp_type_ = (
        'H', COMP_UINT16)  # if idx_count_ > 0xff else (
    #    idx_pref_, idx_comp_type_)
    idx_pref_, idx_comp_type_ = ('I', COMP_UINT32) if idx_count_ > 0xffff else (
        idx_pref_, idx_comp_type_)
    idx_pref_, idx_comp_type_ = (
        'Q', COMP_UINT64) if idx_count_ > 0xffffffff else (idx_pref_, idx_comp_type_)

    ibuf = bytearray()
    for idx_ in indices_:
        ibuf.extend(struct.pack(f'<{idx_pref_}', idx_))

    # ── Cleanup ───────────────────────────────────────────────────────────
    if apply_modifiers:
        eval_obj.to_mesh_clear()
    else:
        obj.to_mesh_clear()

    return {
        "entries":      entries,
        "stride":       offset,
        "vbuf":         mesh_['vertices'],
        "ibuf":         bytes(ibuf),
        "index_type":   idx_comp_type_,
        "index_count":  idx_count_,
        "vertex_count": mesh_['vertex_count'],
        'subsets_data': mesh_['subsets_data'],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  .mesh file writer  (mirrors MeshInternal::writeMeshData + Mesh::save)
# ══════════════════════════════════════════════════════════════════════════════

def _write_mesh_body(mesh: dict) -> bytes:
    # buf = bytearray()
    tracker = _OffsetTracker()

    entries = mesh["entries"]
    vbuf = mesh["vbuf"]
    ibuf = mesh["ibuf"]
    stride = mesh["stride"]
    index_type = mesh["index_type"]
    index_count = mesh["index_count"]
    subsetsData_ = mesh['subsets_data']

    # n_vb = len(entries)
    vsize = len(vbuf)
    isize = len(ibuf)

    # MESH_STRUCT (56 bytes)
    body = bytearray()
    targetEntries_ = []
    targetData_ = []
    targetCount_ = 0
    subsetsCount_ = len(subsetsData_)
    body.extend(struct.pack('<4I', len(targetEntries_),
                len(entries), stride, len(targetData_)))
    body.extend(struct.pack('<4I', vsize, index_type, 0, isize))
    # targetCount, subsetsCount, legacy joints
    body.extend(struct.pack('<4I', targetCount_, subsetsCount_, 0, 0))
    body.extend(struct.pack('<2I', DRAW_MODE_TRIANGLES,
                WINDING_COUNTER_CLOCKWISE))

    # def wu32(v): body.extend(struct.pack("<I", v))
    # def wf32(v): body.extend(struct.pack("<f", v))

    tracker.advance(MESH_STRUCT_SIZE)

    # VB entry structs
    eb_size = 0
    for e in entries:
        body.extend(struct.pack("<4I", 0, e["type"], e["count"], e["offset"]))
        eb_size += VERTEX_BUFFER_ENTRY_STRUCT_SIZE

    body.extend(_pad(tracker.aligned_advance(eb_size)))

    # VB entry names
    for e in entries:
        entryName_ = e["name"] + b"\x00"
        body.extend(struct.pack("<I", len(entryName_)))
        body.extend(entryName_)
        body.extend(_pad(tracker.aligned_advance(4 + len(entryName_))))

    # Vertex buffer
    body.extend(vbuf)
    body.extend(_pad(tracker.aligned_advance(vsize)))

    # Index buffer
    body.extend(ibuf)
    body.extend(_pad(tracker.aligned_advance(isize)))

    # Subset struct V6 (52 bytes)
    subsetByteSize_ = 0
    for item in subsetsData_:
        subsetCount_ = item['icount']
        subsetOffset_ = item['subset_start']
        subsetName_ = item['sname']
        lightmapSizeHintWidth_ = 0
        lightmapSizeHintHeight_ = 0
        lodCount_ = 0
        body.extend(struct.pack('<2I', subsetCount_, subsetOffset_))
        body.extend(struct.pack('<3f', *item['bmin']))
        body.extend(struct.pack('<3f', *item['bmax']))
        body.extend(struct.pack('<5I', 0, len(subsetName_) + 1,
                                lightmapSizeHintWidth_, lightmapSizeHintHeight_, lodCount_))
        subsetByteSize_ += SUBSET_STRUCT_SIZE_V6

    body.extend(_pad(tracker.aligned_advance(subsetByteSize_)))

    # Subset name (UTF-16-LE)
    for item in subsetsData_:
        subsetName_ = item['sname']
        name_utf16_ = (subsetName_ + "\x00").encode("utf-16le")
        body.extend(name_utf16_)
        body.extend(_pad(tracker.aligned_advance(len(name_utf16_))))

    # LOD data

    # Data for morphTargets

    return bytes(body)


def write_mesh_file(mesh: dict, out_path: str):
    """Write a complete Qt .mesh file."""
    body = _write_mesh_body(mesh)
    file_buf = bytearray()

    # Mesh data header (12 bytes)
    file_buf.extend(struct.pack("<IHHI", MESH_FILE_ID,
                    MESH_FILE_VERSION, 0, len(body)))
    file_buf.extend(body)

    # Multi-mesh entry (16 bytes)
    multi_offset = len(file_buf)
    # mesh data at offset 0, mesh id = 1, padding
    file_buf.extend(struct.pack("<QII", 0, 1, 0))

    # Multi-mesh footer (16 bytes)
    file_buf.extend(struct.pack("<4I", MULTI_MESH_FILE_ID,
                    MULTI_MESH_FILE_VERSION, multi_offset, 1))   # meshCount

    with open(out_path, "wb") as fh:
        fh.write(file_buf)


# ─────────────────────────────────────────────────────────────────
#  Blender mesh → Qt .mesh
# ─────────────────────────────────────────────────────────────────

class VertexEntry:
    def __init__(self, name, component_type, component_count, offset):
        self.name = name.encode('ascii')
        self.component_type = component_type
        self.component_count = component_count
        self.offset = offset


def pack_vertex_entries(entries):
    out = bytearray()
    for e in entries:
        out += struct.pack('<4I', 0, e.component_type,
                           e.component_count, e.offset)
    out += b'\x00' * (align4(len(out)) - len(out))
    return out


def pack_names(entries):
    out = bytearray()
    for e in entries:
        n = e.name + b'\x00'
        out += struct.pack('<I', len(n))
        out += n
        out += b'\x00' * (align4(4 + len(n)) - (4 + len(n)))
    return out


def write_mesh_file_(path, vertices, indices, has_normals, has_uv0, bounds_min, bounds_max):
    offset = 0
    entries = []
    for name, comps in [("attr_pos", 3), ("attr_norm", 3 if has_normals else 0), ("attr_uv0", 2 if has_uv0 else 0)]:
        if not comps:
            continue
        offset = align4(offset)
        entries.append(VertexEntry(
            name, COMPONENT_TYPE_FLOAT32, comps, offset))
        offset += comps * 4
    stride = align4(offset)

    vb = bytearray()
    for v in vertices:
        rec = bytearray(stride)
        struct.pack_into('<3f', rec, next(
            e.offset for e in entries if e.name == b'attr_pos'), *v['pos'])
        if has_normals:
            struct.pack_into('<3f', rec, next(
                e.offset for e in entries if e.name == b'attr_norm'), *v['norm'])
        if has_uv0:
            struct.pack_into('<2f', rec, next(
                e.offset for e in entries if e.name == b'attr_uv0'), *v['uv'])
        vb += rec

    index_type = COMPONENT_TYPE_UNSIGNED_INT16 if max(
        indices) <= 65535 else COMPONENT_TYPE_UNSIGNED_INT32
    ib = struct.pack('<' + ('H' if index_type ==
                     COMPONENT_TYPE_UNSIGNED_INT16 else 'I') * len(indices), *indices)

    body = bytearray()
    body += struct.pack('<4I', 0, len(entries), stride, 0)
    body += struct.pack('<I', len(vb))
    body += struct.pack('<3I', index_type, 0, len(ib))
    body += struct.pack('<2I', 0, 1)
    body += struct.pack('<2I', 0, 0)
    body += struct.pack('<2I', DRAW_MODE_TRIANGLES, WINDING_COUNTER_CLOCKWISE)
    body += pack_vertex_entries(entries)
    body += pack_names(entries)
    body += vb
    body += b'\x00' * (align4(len(vb)) - len(vb))
    body += ib
    body += b'\x00' * (align4(len(ib)) - len(ib))

    subset_name = "default"
    subset = struct.pack(
        '<II6fIIII',
        len(indices), 0,
        bounds_min[0], bounds_min[1], bounds_min[2],
        bounds_max[0], bounds_max[1], bounds_max[2],
        0, len(subset_name) + 1, 0, 0
    ) + struct.pack('<I', 0)
    subset += b'\x00' * (align4(len(subset)) - len(subset))
    body += subset
    n16 = (subset_name + '\x00').encode('utf-16le')
    body += n16
    body += b'\x00' * (align4(len(n16)) - len(n16))

    header = struct.pack('<IHHI', MESH_FILE_ID, MESH_FILE_VERSION,
                         MESH_FLAGS, MESH_HEADER_SIZE + len(body))
    entry = struct.pack('<QII', 0, 1, 0)
    file_header = struct.pack('<4I', MULTI_MESH_FILE_ID,
                              MULTI_MESH_FILE_VERSION, len(entry), 1)

    with open(path, 'wb') as f:
        f.write(header)
        f.write(body)
        f.write(entry)
        f.write(file_header)


# ─────────────────────────────────────────────────────────────────
#  Texture export
# ─────────────────────────────────────────────────────────────────

def save_image(image, img_dir):
    safe = sanitize(image.name.replace('.', '_'))
    dest = img_dir / f"{safe}.png"
    old_p = getattr(image, "filepath_raw", "")
    old_f = getattr(image, "file_format", None)

    image.filepath_raw = str(dest)
    image.file_format = 'PNG'
    try:
        if getattr(image, "packed_file", None) is not None:
            image.save()
        else:
            image.save_render(filepath=str(dest))
    except Exception:
        try:
            image.save()
        except Exception:
            pass
    finally:
        image.filepath_raw = old_p
        try:
            enum_items = image.bl_rna.properties["file_format"].enum_items.keys(
            )
            if old_f in enum_items:
                image.file_format = old_f
        except Exception:
            pass

    return f"images/{safe}.png"


# ─────────────────────────────────────────────────────────────────
#  PrincipledMaterial QML block
# ─────────────────────────────────────────────────────────────────

def mat_qml(mat, img_dir, exported_images, indent=1):
    result_ = principled_bsdf_to_quick3d(mat, img_dir, exported_images, indent)
    if len(result_) != 0:
        return result_

    ind = "    " * indent
    ind1 = "    " * (indent + 1)
    out = [f"{ind}PrincipledMaterial {{",
           f'{ind1}id: mat_{sanitize(mat.name)}',
           f'{ind1}objectName: "{mat.name}"']

    if not mat.use_nodes:
        out += [f"{ind1}baseColor: {rgba3(mat.diffuse_color)}",
                f"{ind1}metalness: {mat.metallic:.4f}",
                f"{ind1}roughness: {mat.roughness:.4f}",
                f"{ind}}}"]
        return "\n".join(out)

    bsdf = next((n for n in mat.node_tree.nodes
                 if n.type == 'BSDF_PRINCIPLED'), None)
    if not bsdf:
        out += [f"{ind1}baseColor: {rgba3(mat.diffuse_color)}", f"{ind}}}"]
        return "\n".join(out)

    def val(name):
        return bsdf.inputs[name].default_value

    def tex(name):
        inp = bsdf.inputs.get(name)
        if inp and inp.links:
            n = inp.links[0].from_node
            if n.type == 'TEX_IMAGE' and n.image:
                return n.image
        return None

    def tex_src(img):
        rel = exported_images.get(img.name) or save_image(img, img_dir)
        exported_images[img.name] = rel
        # return f'Texture {{ source: "qrc:/{rel}" }}'
        return f'Texture {{ source: "{rel}" }}'

    def try_map(socket, prop):
        t = tex(socket)
        if t:
            out.append(f"{ind1}{prop}: {tex_src(t)}")
            return True
        return False

    if not try_map('Base Color', 'baseColorMap'):
        out.append(f"{ind1}baseColor: {rgba4(val('Base Color'))}")
    if not try_map('Metallic', 'metalnessMap'):
        out.append(f"{ind1}metalness: {val('Metallic'):.4f}")
    if not try_map('Roughness', 'roughnessMap'):
        out.append(f"{ind1}roughness: {val('Roughness'):.4f}")

    # Normal map (direct or via Normal Map node)
    nm = tex('Normal')
    normal_strength_ = 1.
    if not nm:
        ni = bsdf.inputs.get('Normal')
        if ni and ni.links:
            nn = ni.links[0].from_node
            if nn.type == 'NORMAL_MAP':
                ci = nn.inputs.get('Color')
                if ci and ci.links:
                    cand = ci.links[0].from_node
                    nm = getattr(cand, 'image', None)

                normal_strength_ = nn.inputs["Strength"].default_value

    if nm:
        out.append(f"{ind1}normalMap: {tex_src(nm)}")
        out.append(f'{ind1}normalStrength: {normal_strength_}')

    try_map('Occlusion', 'occlusionMap')

    em = tex('Emission Color') or tex('Emission')
    if em:
        out.append(f"{ind1}emissiveMap: {tex_src(em)}")
    else:
        ec_key = 'Emission Color' if 'Emission Color' in bsdf.inputs else 'Emission'
        ec = val(ec_key)
        if any(v > 0.001 for v in list(ec)[:3]):
            out.append(f"{ind1}emissiveFactor: {rgba3(ec)}")

    alpha = val('Alpha')
    if alpha < 1.:
        out += [f"{ind1}opacity: {alpha:.4f}",
                f"{ind1}alphaMode: PrincipledMaterial.Blend"]
    else:
        out += [f"{ind1}alphaMode: PrincipledMaterial.Opaque"]

    out.append(f"{ind1}cullMode: PrincipledMaterial.NoCulling")

    if 'IOR' in bsdf.inputs:
        ior = val('IOR')
        if abs(ior - 1.5) > 0.01:
            out.append(f"{ind1}indexOfRefraction: {ior:.4f}")

    out.append(f"{ind}}}")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────
#  Light / Camera QML
# ─────────────────────────────────────────────────────────────────

_LIGHT_MAP = {'POINT': 'PointLight', 'SUN': 'DirectionalLight',
              'SPOT': 'SpotLight',   'AREA': 'AreaLight'}


def light_qml(obj, d=2):
    l = obj.data
    t = _LIGHT_MAP.get(l.type, 'PointLight')
    ind = "    " * d
    ind1 = "    " * (d+1)
    pos, rot, sc = qt_local_trs(obj, self.s.convert_coords)
    # pos = qt_pos(obj.location)
    # rot = qt_rot(obj.rotation_euler)
    col = l.color
    lines = [f"{ind}{t} {{",
             f'{ind1}objectName: "{obj.name}"',
             f"{ind1}position: Qt.vector3d{pos}",
             f"{ind1}scale: {sc}",
             f"{ind1}eulerRotation: Qt.vector3d{rot}",
             f"{ind1}color: Qt.rgba({col.r:.4f},{col.g:.4f},{col.b:.4f},1.0)",
             f"{ind1}brightness: {l.energy:.4f}"]
    if l.type == 'SPOT':
        lines += [f"{ind1}coneAngle: {math.degrees(l.spot_size):.4f}",
                  f"{ind1}innerConeAngle: {math.degrees(l.spot_size*(1-l.spot_blend)):.4f}"]
    if l.use_shadow:
        lines.append(f"{ind1}castsShadow: true")
    lines.append(f"{ind}}}")
    return "\n".join(lines)


def camera_qml(obj, d=2):
    cam = obj.data
    ind = "    " * d
    ind1 = "    " * (d+1)
    pos, rot, sc = qt_local_trs(obj, self.s.convert_coords)
    # pos = qt_pos(obj.location)
    # rot = qt_rot(obj.rotation_euler)
    if cam.type == 'ORTHO':
        lines = [f"{ind}OrthographicCamera {{",
                 f"{ind1}horizontalMagnification: {cam.ortho_scale:.4f}"]
    else:
        lines = [f"{ind}PerspectiveCamera {{",
                 f"{ind1}fieldOfView: {math.degrees(cam.angle):.4f}",
                 f"{ind1}fieldOfViewOrientation: PerspectiveCamera.Vertical"]
    lines += [f'{ind1}objectName: "{obj.name}"',
              f"{ind1}position: Qt.vector3d{pos}",
              f"{ind1}scale: {sc}",
              f"{ind1}eulerRotation: Qt.vector3d{rot}",
              f"{ind1}clipNear: {cam.clip_start:.4f}",
              f"{ind1}clipFar: {cam.clip_end:.4f}",
              f"{ind}}}"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
#  Animation → Timeline QML
# ─────────────────────────────────────────────────────────────────

def anim_qml(scene, node_ids, d=2):
    if not any(o.animation_data and o.animation_data.action
               for o in scene.objects):
        return ""

    fps = scene.render.fps
    s, e = scene.frame_start, scene.frame_end
    def I(n): return "    " * n
    lines = [f"{I(d)}Timeline {{",
             f"{I(d+1)}id: timeline",
             f"{I(d+1)}startFrame: {s}", f"{I(d+1)}endFrame: {e}",
             f"{I(d+1)}enabled: true",
             f"{I(d+1)}animations: [",
             f"{I(d+2)}TimelineAnimation {{",
             f"{I(d+3)}duration: {int((e-s)/fps*1000)}",
             f"{I(d+3)}from: {s}", f"{I(d+3)}to: {e}",
             f"{I(d+3)}running: true", f"{I(d+3)}loops: Animation.Infinite",
             f"{I(d+2)}}}", f"{I(d+1)}]", ""]

    prop_map = {
        'location':       ('position', lambda v: qt_pos(Vector(v))),
        'rotation_euler': ('eulerRotation', lambda v: qt_rot(Euler(v, 'XYZ'))),
        'scale':          ('scale', lambda v: qt_scale(Vector(v))),
    }

    for obj in scene.objects:
        if not (obj.animation_data and obj.animation_data.action):
            continue
        nid = node_ids.get(obj.name)
        if not nid:
            continue
        groups = {}
        for fc in obj.animation_data.action.fcurves:
            groups.setdefault(fc.data_path, {})[fc.array_index] = fc
        for dp, idx_map in groups.items():
            if dp not in prop_map:
                continue
            qt_prop, conv = prop_map[dp]
            frames = sorted(set(int(kp.co[0]) for fc in idx_map.values()
                                for kp in fc.keyframe_points))
            if not frames:
                continue
            lines += [f"{I(d+1)}KeyframeGroup {{",
                      f"{I(d+2)}target: {nid}",
                      f'{I(d+2)}property: "{qt_prop}"']
            for fr in frames:
                vals = [idx_map[ax].evaluate(fr) if ax in idx_map else 0.0
                        for ax in range(3)]
                qv = conv(vals)
                lines.append(f"{I(d+3)}Keyframe {{ frame: {fr}; "
                             f"value: Qt.vector3d({qv[0]:.4f},{qv[1]:.4f},{qv[2]:.4f}) }}")
            lines += [f"{I(d+1)}}}", ""]

    lines.append(f"{I(d)}}}")
    return "\n".join(lines)


def is_linked(obj):
    if obj is None:
        return False

    if obj.type == 'MESH':
        return obj.library is not None or (obj.data and obj.data.library is not None)

    if obj.type == 'EMPTY' and obj.instance_type == 'COLLECTION' and obj.instance_collection:
        return obj.library is not None or obj.instance_collection.library is not None

    return obj.library is not None


def hide_render(obj):
    has_renderable_collection = any(
        not col.hide_render
        for col in obj.users_collection
    )

    return (obj.hide_render and not is_linked(obj)) or not has_renderable_collection


# ─────────────────────────────────────────────────────────────────
#  Main exporter
# ─────────────────────────────────────────────────────────────────

class BalsamExporter:
    def __init__(self, filepath, settings):
        self.root = Path(filepath).parent
        self.qml_path = Path(filepath)
        self.s = settings

        self.mesh_dir = self.root / "meshes"
        self.img_dir = self.root / "images"
        for d in (self.mesh_dir, self.img_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.exp_images = {}   # blender_name → "images/x.png"
        self.exp_materials = {}   # blender_name → (id_str, qml_str)
        self.exp_meshes = {}   # blender_name → "meshes/x.mesh"
        self.node_ids = {}   # blender_name → qml id

    def _ensure_mat(self, mat):
        if mat and mat.name not in self.exp_materials:
            q = mat_qml(mat, self.img_dir, self.exp_images, indent=0)
            self.exp_materials[mat.name] = (f"mat_{sanitize(mat.name)}", q)

    def extract_and_write_mesh(self, obj, filepath):
        self.s.report({"INFO"}, f"Exporting '{obj.name}' → '{filepath}' …")

        try:
            mesh_data = extract_mesh_data(
                obj,
                apply_modifiers=self.s.apply_modifiers,
                convert_coords=self.s.convert_coords,
            )
        except Exception as exc:
            self.s.report({"ERROR"}, f"Failed to extract '{obj.name}': {exc}")
            return False

        if mesh_data["vertex_count"] == 0:
            self.s.report(
                {"WARNING"}, f"'{obj.name}' has no geometry; skipping.")
            return False

        mesh_name = bpy.path.clean_name(obj.name)
        write_mesh_file(mesh_data, filepath)  # , subset_name=mesh_name)
        self.s.report({"INFO"}, f"{mesh_data['vertex_count']} verts, "
                      f"{mesh_data['index_count'] // 3} tris, entries: {len(mesh_data['entries'])} → {filepath}"
                      )

        # validate_report_ = validate_qt_mesh(filepath)
        # self.s.report({"INFO"}, f'  Mesh validated: {validate_report_}')

        return True

    def _obj_qml(self, obj, d=2):
        hide_render_ = hide_render(obj)

        self.s.report({"INFO"}, f"processing object: {obj.name}, type: {obj.type}, instance_type: {obj.instance_type}, has_collection: {obj.instance_collection is not None}, hide_render: {hide_render_}")

        if hide_render_:
            return []

        blocks = []
        safe = sanitize(obj.name)
        nid = f"node_{safe}"
        self.node_ids[obj.name] = nid
        def I(n): return "    " * n

        pos, rot, sc = qt_local_trs(obj, self.s.convert_coords)
        # pos = qt_pos(obj.location)
        # rot = qt_rot(obj.rotation_euler)
        # sc = qt_scale(obj.scale)

        if obj.type == 'MESH':
            if obj.name not in self.exp_meshes:
                mp = self.mesh_dir / f"{safe}.mesh"
                if not self.extract_and_write_mesh(obj, str(mp)):
                    return blocks

                self.exp_meshes[obj.name] = f"meshes/{safe}.mesh"

            rel = self.exp_meshes[obj.name]

            for slot in obj.material_slots:
                if slot.material:
                    self._ensure_mat(slot.material)
            mat_ids = [f"mat_{sanitize(sl.material.name)}"
                       for sl in obj.material_slots if sl.material]

            lines = [f"{I(d)}Model {{",
                     f"{I(d+1)}id: {nid}",
                     f'{I(d+1)}objectName: "{obj.name}"',
                     f'{I(d+1)}source: "{rel}"',  # qrc:/{rel}
                     f"{I(d+1)}position: Qt.vector3d{pos}",
                     f"{I(d+1)}eulerRotation: Qt.vector3d{rot}",
                     f"{I(d+1)}scale: Qt.vector3d{sc}"]
            if obj.hide_render:
                lines.append(f"{I(d+1)}visible: false")
            if mat_ids:
                lines.append(f"{I(d+1)}materials: [ {', '.join(mat_ids)} ]")
            for child in obj.children:
                lines.extend(ln for ln in "\n".join(
                    self._obj_qml(child, d + 1)).split("\n"))
            lines.append(f"{I(d)}}}")
            blocks.append("\n".join(lines))

        elif obj.type == 'LIGHT' and self.s.export_lights:
            blocks.append(light_qml(obj, d))
        elif obj.type == 'CAMERA' and self.s.export_cameras:
            blocks.append(camera_qml(obj, d))
        elif obj.type == 'EMPTY':
            lines = [f"{I(d)}Node {{",
                     f"{I(d+1)}id: {nid}",
                     f'{I(d+1)}objectName: "{obj.name}"',
                     f"{I(d+1)}position: Qt.vector3d{pos}",
                     f"{I(d+1)}eulerRotation: Qt.vector3d{rot}",
                     f"{I(d+1)}scale: Qt.vector3d{sc}"]
            for child in obj.children:
                lines.extend(ln for ln in
                             "\n".join(self._obj_qml(child, d + 1)).split("\n"))

            if obj.instance_type == "COLLECTION" and obj.instance_collection != None:
                for cobj in [o for o in obj.instance_collection.objects if o.parent is None]:
                    lines.extend(ln for ln in
                                 "\n".join(self._obj_qml(cobj, d + 1)).split("\n"))

            lines.append(f"{I(d)}}}")
            blocks.append("\n".join(lines))

        return blocks

    def export(self):
        scene = bpy.context.scene
        stem = sanitize(self.qml_path.stem)

        # Process all top-level objects
        node_blocks = []
        for obj in [o for o in scene.objects if o.parent is None]:
            node_blocks.extend(self._obj_qml(obj, d=2))

        # Animation
        anim = anim_qml(scene, self.node_ids,
                        d=2) if self.s.export_animations else ""

        # ── Assemble QML ──────────────────────────────────────────
        imports = ["import QtQuick", "import QtQuick3D"]
        if self.s.export_animations:
            imports.append("import QtQuick.Timeline")

        mat_section = ""
        for name, (mid, mq) in self.exp_materials.items():
            reindented = "\n".join(("    " + l if l.strip() else l)
                                   for l in mq.split("\n"))
            mat_section += reindented + "\n\n"

        qml = f'// {stem}.qml\n' + '\n'.join(imports)
        qml += f"\n\n// Qt Quick 3D — exported by Blender Qt Balsam Exporter\n"
        qml += f"// Native .mesh files — no balsam conversion step required\n\n"
        qml += f"Node {{\n    id: root\n    objectName: '{scene.name}'"
        if self.s.convert_coords:
            qml += f"\n    scale: Qt.vector3d(100., 100., 100.)"

        qml += "\n\n"
        if mat_section:
            qml += "    // ── Materials ─────────────────────────────────────────\n"
            qml += mat_section

        qml += "    // ── Scene Nodes ───────────────────────────────────────\n"
        qml += "    Node {\n"
        qml += "\n".join(node_blocks)
        qml += "\n    }\n"

        if anim:
            qml += "\n\n    // ── Animations ────────────────────────────────────\n"
            qml += anim

        qml += "\n}\n"

        self.qml_path.write_text(qml, encoding='utf-8')

        # ── .qrc ──────────────────────────────────────────────────
        all_files = ([self.qml_path.name] +
                     list(self.exp_meshes.values()) +
                     list(self.exp_images.values()))
        qrc = ['<RCC>', '    <qresource prefix="/">']
        for f in sorted(set(all_files)):
            qrc.append(f'        <file>{f}</file>')
        qrc += ['    </qresource>', '</RCC>']
        (self.root / f"{self.qml_path.stem}.qrc").write_text(
            "\n".join(qrc), encoding='utf-8')

        # ── CMake snippet ─────────────────────────────────────────
        cmake = [f"# No balsam step needed — .mesh files are already in Qt native format",
                 f"qt_add_resources(${{TARGET}} \"{stem}_assets\"",
                 f"    PREFIX \"/\"", f"    FILES"]
        for f in sorted(set(all_files)):
            cmake.append(f"        {f}")
        cmake.append(")")
        (self.root / "CMakeLists_qt3d_snippet.txt").write_text(
            "\n".join(cmake), encoding='utf-8')

        # ── Manifest ──────────────────────────────────────────────
        (self.root / "export_manifest.json").write_text(json.dumps({
            "scene": scene.name, "qml": self.qml_path.name,
            "meshes": self.exp_meshes, "images": self.exp_images,
            "materials": list(self.exp_materials.keys()),
        }, indent=2), encoding='utf-8')

        # {"CANCELLED"}
        return {'FINISHED'}


# ─────────────────────────────────────────────────────────────────
#  Blender Operator & registration
# ─────────────────────────────────────────────────────────────────

class EXPORT_OT_qt_balsam(bpy.types.Operator):
    """Export Blender scene to Qt Quick 3D QML and native .mesh assets."""
    bl_idname = "export_scene.qt_balsam"
    bl_label = "Qt Quick 3D (.qml)"
    bl_options = {'PRESET', 'UNDO'}

    filepath:    bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.qml", options={'HIDDEN'})

    export_cameras: bpy.props.BoolProperty(
        name="Cameras", default=False,
        description="Export cameras as Qt camera nodes")
    export_lights: bpy.props.BoolProperty(
        name="Lights", default=False,
        description="Export lights as Qt light nodes")
    export_animations: bpy.props.BoolProperty(
        name="Animations", default=False,
        description="Export object animations via Timeline/KeyframeGroup")
    apply_modifiers: bpy.props.BoolProperty(
        name="Apply Modifiers", default=True,
        description="Apply mesh modifiers before exporting geometry")
    selected_only: bpy.props.BoolProperty(
        name="Selected Only", default=False,
        description="Only export currently selected objects")
    convert_coords: bpy.props.BoolProperty(
        name="Convert Coordinates (Z-up → Y-up)",
        description="Convert Blender's Z-up to Qt Quick 3D's Y-up coordinate system",
        default=True,
    )

    def execute(self, context):
        if not self.filepath.endswith(".qml"):
            self.filepath += ".qml"
        return BalsamExporter(self.filepath, self).export()

    def invoke(self, context, event):
        self.filepath = sanitize(context.scene.name) + ".qml"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context):
        lo = self.layout
        lo.use_property_split = True
        lo.use_property_decorate = False
        b = lo.box()
        b.label(text="Include", icon='SCENE_DATA')
        b.prop(self, "export_cameras")
        b.prop(self, "export_lights")
        b.prop(self, "export_animations")
        b2 = lo.box()
        b2.label(text="Mesh", icon='MESH_DATA')
        b2.prop(self, "apply_modifiers")
        b2.prop(self, "selected_only")
        b2.prop(self, "convert_coords")


def menu_func(self, context):
    self.layout.operator(EXPORT_OT_qt_balsam.bl_idname,
                         text="Qt Quick 3D (.qml)")


def register():
    bpy.utils.register_class(EXPORT_OT_qt_balsam)
    bpy.types.TOPBAR_MT_file_export.append(menu_func)


def unregister():
    bpy.utils.unregister_class(EXPORT_OT_qt_balsam)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func)


if __name__ == "__main__":
    register()
