#!/usr/bin/env python3
import argparse
import json
import struct

MULTI_FILE_ID = 555777497
MESH_FILE_ID = 3365961549
FILE_VERSION = 1

VERTEX_BUFFER_ENTRY_STRUCT_SIZE = 16
MULTI_HEADER_STRUCT_SIZE = 16
MULTI_ENTRY_STRUCT_SIZE = 16
MESH_HEADER_STRUCT_SIZE = 12
MESH_STRUCT_SIZE = 56
SUBSET_STRUCT_SIZE_V6 = 52


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

    def offset(self):
        return self.counter


_PAD4 = b"\x00\x00\x00\x00"


def _pad(n: int) -> bytes:
    return _PAD4[:n]


def align4(n):
    return (n + 3) & ~3


def read_u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def read_u16(buf, off):
    return struct.unpack_from('<H', buf, off)[0]


def read_mesh(data, mesh_id, mesh_offset):
    # mesh_offset, mesh_id, _ = struct.unpack_from('<QII', data, len(data) - 32)
    mesh_file_id, mesh_file_version, mesh_flags, mesh_size = struct.unpack_from(
        '<IHHI', data, mesh_offset)
    if mesh_file_id != MESH_FILE_ID:
        raise ValueError(f'invalid mesh file id: {mesh_file_id}')

    tracker = _OffsetTracker()
    tracker.advance(mesh_offset + MESH_HEADER_STRUCT_SIZE)

    target_entries, vertex_entries, stride, target_data_size, vertex_data_size, \
        index_component_type, _legacy_index_offset, index_data_size, target_count, \
        subset_count, _joints_offset, _joints_count, draw_mode, winding \
        = struct.unpack_from('<14I', data, tracker.offset())
    tracker.advance(MESH_STRUCT_SIZE)

    entries = []
    entriesByteSize_ = 0
    for _ in range(vertex_entries):
        _name_offs, ctype, ccount, eoffs = struct.unpack_from(
            '<4I', data, entriesByteSize_ + tracker.offset())
        entries.append(
            {'componentType': ctype, 'componentCount': ccount, 'offset': eoffs})
        entriesByteSize_ += VERTEX_BUFFER_ENTRY_STRUCT_SIZE

    tracker.aligned_advance(entriesByteSize_)
    # print(f'tracker: {tracker.offset()} , off: {off}')

    for i in range(vertex_entries):
        name_len = read_u32(data, tracker.offset())
        tracker.advance(4)  # sizeof(quint32)
        off_ = tracker.offset()
        namez = data[off_:off_ + name_len]
        tracker.aligned_advance(name_len)
        entries[i]['name'] = namez[:-
                                   1].decode('ascii', errors='replace') if name_len else ''
        print(f'idx: {i} len: {name_len}, name: {entries[i]["name"]}')

    vertex_data_offset = tracker.offset()
    tracker.aligned_advance(vertex_data_size)
    index_data_offset = tracker.offset()
    tracker.aligned_advance(index_data_size)

    subsets = []
    subsetByteSize_ = 0
    for _ in range(subset_count):
        count, idx_off, minx, miny, minz, maxx, maxy, maxz, _name_off, name_len, lm_w, lm_h, lod_count = struct.unpack_from(
            '<II6fIIIII', data, tracker.offset())
        subsets.append({'count': count, 'offset': idx_off, 'boundsMin': [minx, miny, minz], 'boundsMax': [
                       maxx, maxy, maxz], 'nameLength': name_len, 'lightmap': [lm_w, lm_h], 'lodCount': lod_count})
        subsetByteSize_ += SUBSET_STRUCT_SIZE_V6

    tracker.aligned_advance(subsetByteSize_)

    for s in subsets:
        name_bytes = s['nameLength'] * 2
        off_ = tracker.offset()
        raw = data[off_:off_ + name_bytes]
        tracker.aligned_advance(name_bytes)
        # off = mesh_offset + 12 + align4(off - (mesh_offset + 12))
        s['name'] = raw[:-2].decode('utf-16le',
                                    errors='replace') if name_bytes >= 2 else ''

    return {
        'meshId': mesh_id, 'meshOffset': mesh_offset,
        'meshHeader': {'fileId': mesh_file_id, 'fileVersion': mesh_file_version, 'flags': mesh_flags, 'sizeInBytes': mesh_size},
        'mesh': {'vertexEntries': vertex_entries, 'targetEntries': target_entries, 'stride': stride, 'vertexDataSize': vertex_data_size,
                 'indexComponentType': index_component_type, 'indexDataSize': index_data_size, 'targetCount': target_count, 'subsetCount': subset_count,
                 'drawMode': draw_mode, 'winding': winding, 'vertexDataOffset': vertex_data_offset, 'indexDataOffset': index_data_offset},
        'attributes': entries,
        'subsets': subsets,
    }


def validate_qt_mesh(path):
    with open(path, 'rb') as f:
        data = f.read()

    if len(data) < 28:
        raise ValueError('file too small')

    # footer header
    multiHeaderStartOffset_ = len(data) - MULTI_HEADER_STRUCT_SIZE
    file_id, file_version, entries_offset, mesh_count = struct.unpack_from(
        '<4I', data, multiHeaderStartOffset_)
    print(f'mesh count: {mesh_count}')

    if file_id != MULTI_FILE_ID:
        raise ValueError(f'invalid multi-mesh file id: {file_id}')

    if file_version != FILE_VERSION:
        raise ValueError(f'invalid file_version file id: {file_version}')

    if mesh_count < 1:
        raise ValueError('no meshes recorded in footer')

    meshOffsets = {}
    meshes = []
    for idx in range(mesh_count):
        fileOffset_ = multiHeaderStartOffset_ - MULTI_ENTRY_STRUCT_SIZE * \
            mesh_count + MULTI_ENTRY_STRUCT_SIZE * idx
        offset_, id_, padding_ = struct.unpack_from('<QII', data, fileOffset_)
        meshOffsets[id_] = offset_
        print(f'meshes: {id_} {offset_}')
        meshes.append(read_mesh(data, id_, offset_))

    return {
        'multiMesh': {'fileId': file_id, 'fileVersion': file_version, 'entriesOffset': entries_offset, 'meshCount': mesh_count},
        'meshes': meshes
    }


def main():
    ap = argparse.ArgumentParser(
        description='Validate and inspect a Qt Quick 3D .mesh file')
    ap.add_argument('mesh_file')
    args = ap.parse_args()
    print(json.dumps(validate_qt_mesh(args.mesh_file), indent=2))


if __name__ == '__main__':
    main()
