"""
qt_mesh_writer_qt611.py

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

LE = '<'

# QSSGRenderComponentType
COMP_UINT8 = 1
COMP_INT8 = 2
COMP_UINT16 = 3
COMP_INT16 = 4
COMP_UINT32 = 5
COMP_INT32 = 6
COMP_UINT64 = 7
COMP_INT64 = 8
COMP_FLOAT16 = 9
COMP_FLOAT32 = 10
COMP_FLOAT64 = 11

MULTI_MESH_FILE_ID = 555777497
MULTI_MESH_FILE_VERSION = 1
MESH_DATA_FILE_ID = 3365961549
MESH_DATA_FILE_VERSION = 7

ATTR_POSITION = 'attr_pos'
ATTR_NORMAL = 'attr_norm'
ATTR_UV0 = 'attr_uv0'
ATTR_UV1 = 'attr_uv1'
ATTR_TANGENT = 'attr_textan'
ATTR_BINORMAL = 'attr_binormal'
ATTR_COLOR = 'attr_color'
ATTR_JOINTS = 'attr_joints'
ATTR_WEIGHTS = 'attr_weights'


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


def _validate(vertex_data: bytes,
              vertex_stride: int,
              attributes: Sequence[VertexAttribute],
              index_data: bytes,
              index_component_type: int,
              subsets: Sequence[MeshSubset]) -> None:
    if not vertex_data:
        raise MeshWriterError('vertex_data is empty')
    if vertex_stride <= 0:
        raise MeshWriterError('vertex_stride must be > 0')
    if len(vertex_data) % vertex_stride != 0:
        raise MeshWriterError('vertex_data size is not divisible by vertex_stride')
    if not attributes:
        raise MeshWriterError('at least one vertex attribute is required')
    if index_component_type not in (COMP_UINT16, COMP_UINT32):
        raise MeshWriterError('index_component_type must be COMP_UINT16 or COMP_UINT32')
    index_size = 2 if index_component_type == COMP_UINT16 else 4
    if len(index_data) % index_size != 0:
        raise MeshWriterError('index_data size is not divisible by index component size')
    total_indices = len(index_data) // index_size
    for a in attributes:
        if a.byte_offset < 0:
            raise MeshWriterError(f'negative attribute offset for {a.name}')
        end = a.byte_offset + a.num_components * {COMP_UINT8:1, COMP_INT8:1, COMP_UINT16:2, COMP_INT16:2, COMP_UINT32:4, COMP_INT32:4, COMP_UINT64:8, COMP_INT64:8, COMP_FLOAT16:2, COMP_FLOAT32:4, COMP_FLOAT64:8}[a.component_type]
        if end > vertex_stride:
            raise MeshWriterError(f'attribute {a.name} exceeds vertex stride')
    covered = 0
    for s in subsets:
        if s.index_count < 0 or s.index_offset < 0:
            raise MeshWriterError(f'invalid subset range for {s.name}')
        if s.index_offset + s.index_count > total_indices:
            raise MeshWriterError(f'subset {s.name} exceeds index buffer size')
        covered += s.index_count
    #if covered > total_indices:
    #    raise MeshWriterError('subset coverage exceeds index buffer size')


def write_qt_quick3d_mesh(filepath: str | Path,
                          vertex_data: bytes,
                          vertex_stride: int,
                          attributes: Sequence[VertexAttribute],
                          index_data: bytes,
                          index_component_type: int,
                          subsets: Sequence[MeshSubset]) -> None:
    """
    Write a Qt Quick 3D .mesh file.

    Parameters
    ----------
    filepath            : output path ending in .mesh
    vertex_data         : raw interleaved vertex bytes
    vertex_stride       : bytes per vertex
    vb_entries          : list of VBEntry (attributes)
    index_data          : raw index bytes
    index_component_type: COMP_UINT32 (4 bytes) or COMP_UINT16 (2 bytes)
    subsets             : list of MeshSubset
    """

    attributes = _normalize_attributes(attributes)
    subsets = _normalize_subsets(subsets)
    _validate(vertex_data, vertex_stride, attributes, index_data, index_component_type, subsets)

    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. Build string table for vertex entry names ───────────────
    # String table is UTF-16LE blob; entries reference it by offset+length.
    string_table = bytearray()
    attr_name_refs: list[tuple[int, int]] = []
    for a in attributes:
        off = len(string_table)
        string_table.extend(_utf16(a.name))
        attr_name_refs.append((off, len(a.name)))

    subset_name_refs: list[tuple[int, int]] = []
    for s in subsets:
        off = len(string_table)
        string_table.extend(_utf16(s.name))
        subset_name_refs.append((off, len(s.name)))

    str_bytes = bytes(string_table)

    # ── 2. Build descriptor payload ────────────────────────────────
    payload = bytearray()
    w = payload.extend

    w(_u32(0))
    w(_u32(len(vertex_data)))
    w(_u32(vertex_stride))
    w(_u32(len(attributes)))
    for i, a in enumerate(attributes):
        name_off, name_len = attr_name_refs[i]
        w(_u32(name_off))
        w(_u32(name_len))
        w(_u32(a.component_type))
        w(_u32(a.num_components))
        w(_u32(a.byte_offset))
        w(_u32(0))

    w(_u32(index_component_type))
    w(_u32(0))
    w(_u32(len(index_data)))

    w(_u32(0))
    w(_u32(0))
    w(_u32(0))
    w(_u32(0))

    w(_u32(len(subsets)))
    for s in subsets:
        w(_u32(s.index_count))
        w(_u32(s.index_offset))
        for v in s.bounds_min:
            w(_f32(v))
        for v in s.bounds_max:
            w(_f32(v))
        w(_u16(s.lightmap_width))
        w(_u16(s.lightmap_height))
        w(_u32(0))

    for name_off, name_len in subset_name_refs:
        w(_u32(name_off))
        w(_u32(name_len))

    w(_u32(len(str_bytes)))
    w(str_bytes)
    _pad4(payload)

    w(vertex_data)
    _pad4(payload)
    w(index_data)
    _pad4(payload)

    payload_bytes = bytes(payload)

    mesh_block = bytearray()
    mesh_block.extend(_u32(MESH_DATA_FILE_ID))
    mesh_block.extend(_u16(MESH_DATA_FILE_VERSION))
    mesh_block.extend(_u16(0))
    mesh_block.extend(_u32(len(payload_bytes)))
    mesh_block.extend(payload_bytes)
    mesh_block.extend(b'\x00' * ((-len(mesh_block)) % 8))

    outer = bytearray()
    outer.extend(_u32(MULTI_MESH_FILE_ID))
    outer.extend(_u32(MULTI_MESH_FILE_VERSION))
    outer.extend(_u32(1))
    outer.extend(_u32(1))
    outer.extend(_u64(24))

    with filepath.open('wb') as f:
        f.write(outer)
        f.write(mesh_block)


__all__ = [
    'COMP_UINT16', 'COMP_UINT32', 'COMP_FLOAT32',
    'ATTR_POSITION', 'ATTR_NORMAL', 'ATTR_UV0', 'ATTR_UV1',
    'ATTR_TANGENT', 'ATTR_BINORMAL', 'ATTR_COLOR', 'ATTR_JOINTS', 'ATTR_WEIGHTS',
    'VertexAttribute', 'MeshSubset', 'MeshWriterError', 'write_qt_quick3d_mesh'
]
