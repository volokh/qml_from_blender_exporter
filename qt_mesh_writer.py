"""
Qt Quick 3D native .mesh writer aimed at Qt 6.11 compatibility.

This writer keeps the v7 MeshDataHeader/container structure used by Qt Quick 3D,
but is stricter about subset offsets and payload layout than the older exporter.

Key choices:
- MeshDataHeader FILE_VERSION = 7
- MultiMeshInfo outer container with one mesh entry
- UTF-16LE string table without terminators
- subset.index_offset is written as INDEX ELEMENT OFFSET (not byte offset)
- lightmap width/height packed as two quint16 values
- LOD count = 0 for every subset
- target buffer descriptor present but empty

Binary format reverse-engineered from:
  qtquick3d/src/utils/qssgmesh_p.h   (v6.6, FILE_VERSION = 7)
  qtquick3d/src/utils/qssgmesh.cpp

File layout (all little-endian):
──────────────────────────────────────────────────────────────────
OUTER CONTAINER  (MultiMeshInfo)
  quint32  fileId       = 555777497   (0x2124_8959)
  quint32  fileVersion  = 1
  quint32  meshCount
  for each mesh:
    quint32  meshId
    quint64  byteOffset   (absolute, from start of file)

PER-MESH BLOCK  (MeshDataHeader + payload, 8-byte aligned)
  quint32  fileId       = 3365961549  (0xC884_094D)
  quint16  fileVersion  = 7
  quint16  flags        = 0
  quint32  sizeInBytes  (size of payload following this header)

  [payload – all offsets relative to start of payload]
  VertexBuffer:
    quint32  byteOffset    (into data section, 0 in v4+)
    quint32  byteSize
    quint32  stride
    quint32  entryCount
    for each entry:
      quint32  nameOffset   (into string table)
      quint32  nameLength
      quint32  componentType  (QSSGRenderComponentType enum)
      quint32  numComponents
      quint32  firstItemOffset  (byte offset inside one vertex)
      quint32  _pad

  IndexBuffer:
    quint32  componentType
    quint32  byteOffset    (0 in v4+)
    quint32  byteSize

  TargetBuffer (v7+):
    quint32  numTargets
    quint32  entryCount
    for each entry: same layout as VertexBufferEntry
    quint32  byteOffset
    quint32  byteSize

  SubsetCount: quint32
  for each subset:
    quint32  indexCount
    quint32  indexOffset
    float    boundsMin[3]
    float    boundsMax[3]
    quint16  lightmapWidth   (v5+)
    quint16  lightmapHeight  (v5+)
    quint32  lodCount        (v6+)
    for each lod (v6+):
      quint32  count; quint32  offset; float  distance

  Subset names (after all subset structs):
    for each subset: quint32 nameOffset, quint32 nameLength

  String table (UTF-16LE strings, no null terminator in qt6)

  TargetBuffer LOD data (v7+): not used here, 0 entries

  Actual binary vertex data   (padded to 4 bytes)
  Actual binary index data    (padded to 4 bytes)
  Actual target buffer data   (padded to 4 bytes)

NOTE: In practice the "offset" fields in the buffer descriptors are
all set to 0 in version 4+ (the loader ignores them), and the data
blobs are appended in order after the descriptor section.

This writer produces a single-mesh .mesh file (meshId = 1).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from mathutils import Vector, Euler, Matrix
from pathlib import Path
import re
import json
import struct
import math
import bmesh
import bpy
from bpy.types import Operator
from bpy.props import (
    StringProperty, BoolProperty, EnumProperty,
    FloatProperty, IntProperty
)

# QSSGRenderComponentType
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

'''
def _u16(v: int) -> bytes:
    return struct.pack(LE + 'H', int(v))


def _u32(v: int) -> bytes:
    return struct.pack(LE + 'I', int(v))


def _u64(v: int) -> bytes:
    return struct.pack(LE + 'Q', int(v))


def _f32(v: float) -> bytes:
    return struct.pack(LE + 'f', float(v))


def _utf16(s: str) -> bytes:
    return s.encode('utf-16-le')


def _pad4(buf: bytearray) -> None:
    buf.extend(b'\x00' * ((-len(buf)) % 4))


@dataclass(frozen=True)
class VertexAttribute:
    name: str
    component_type: int
    num_components: int
    byte_offset: int


@dataclass(frozen=True)
class MeshSubset:
    name: str
    index_count: int
    index_offset: int
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    lightmap_width: int = 0
    lightmap_height: int = 0


class MeshWriterError(RuntimeError):
    pass


def _normalize_attributes(attributes):
    out = []
    for a in attributes:
        if isinstance(a, VertexAttribute):
            out.append(a)
        elif isinstance(a, tuple) and len(a) == 4:
            out.append(VertexAttribute(a[0], a[1], a[2], a[3]))
        else:
            raise MeshWriterError(f'invalid attribute descriptor: {a!r}')
    return out


def _normalize_subsets(subsets):
    out = []
    for s in subsets:
        if isinstance(s, MeshSubset):
            out.append(s)
        elif isinstance(s, tuple) and len(s) >= 5:
            out.append(MeshSubset(s[0], s[1], s[2], tuple(s[3]), tuple(s[4])))
        else:
            raise MeshWriterError(f'invalid subset descriptor: {s!r}')
    return out
'''

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


def inverse(t):
    return tuple(-1 * elem for elem in t)


'''
# ─────────────────────────────────────────────────────────────────
#  Blender mesh → Qt .mesh
# ─────────────────────────────────────────────────────────────────

class VertexEntry:
    def __init__(self, name, component_type, component_count, offset):
        self.name = name.encode('ascii')
        self.component_type = component_type
        self.component_count = component_count
        self.offset = offset


def align4(n):
    return (n + 3) & ~3


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
'''
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


_PAD4 = b"\x00\x00\x00\x00"


def _pad(n: int) -> bytes:
    return _PAD4[:n]


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


