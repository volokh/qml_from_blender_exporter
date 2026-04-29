#!/usr/bin/env python3
import argparse
import json
import struct

MULTI_FILE_ID = 555777497
MESH_FILE_ID = 3365961549


def align4(n):
    return (n + 3) & ~3


def read_u32(buf, off):
    return struct.unpack_from('<I', buf, off)[0]


def read_u16(buf, off):
    return struct.unpack_from('<H', buf, off)[0]


def qt_mesh_validate(path):
    with open(path, 'rb') as f:
        data = f.read()

    if len(data) < 28:
        raise ValueError('file too small')

    file_id, file_version, entries_offset, mesh_count = struct.unpack_from('<4I', data, len(data) - 16)
    if file_id != MULTI_FILE_ID:
        raise ValueError(f'invalid multi-mesh file id: {file_id}')
    if mesh_count < 1:
        raise ValueError('no meshes recorded in footer')

    mesh_offset, mesh_id, _ = struct.unpack_from('<QII', data, len(data) - 32)
    mesh_file_id = read_u32(data, mesh_offset)
    mesh_file_version = read_u16(data, mesh_offset + 4)
    mesh_flags = read_u16(data, mesh_offset + 6)
    mesh_size = read_u32(data, mesh_offset + 8)
    if mesh_file_id != MESH_FILE_ID:
        raise ValueError(f'invalid mesh file id: {mesh_file_id}')

    off = mesh_offset + 12
    target_entries, vertex_entries, stride, target_data_size = struct.unpack_from('<4I', data, off)
    off += 16
    vertex_data_size = read_u32(data, off)
    off += 4
    index_component_type, _legacy_index_offset, index_data_size = struct.unpack_from('<3I', data, off)
    off += 12
    target_count, subset_count = struct.unpack_from('<2I', data, off)
    off += 8
    _joints_offset, _joints_count = struct.unpack_from('<2I', data, off)
    off += 8
    draw_mode, winding = struct.unpack_from('<2I', data, off)
    off += 8

    entries = []
    for _ in range(vertex_entries):
        _name_off, ctype, ccount, eoff = struct.unpack_from('<4I', data, off)
        entries.append({'componentType': ctype, 'componentCount': ccount, 'offset': eoff})
        off += 16
    off = mesh_offset + 12 + align4(off - (mesh_offset + 12))

    for i in range(vertex_entries):
        name_len = read_u32(data, off)
        off += 4
        namez = data[off:off + name_len]
        off += name_len
        off = mesh_offset + 12 + align4(off - (mesh_offset + 12))
        entries[i]['name'] = namez[:-1].decode('ascii', errors='replace') if name_len else ''

    vertex_data_offset = off
    off += vertex_data_size
    off = mesh_offset + 12 + align4(off - (mesh_offset + 12))
    index_data_offset = off
    off += index_data_size
    off = mesh_offset + 12 + align4(off - (mesh_offset + 12))

    subsets = []
    for _ in range(subset_count):
        count, idx_off, minx, miny, minz, maxx, maxy, maxz, _name_off, name_len, lm_w, lm_h, lod_count = struct.unpack_from('<II6fIIIII', data, off)
        subsets.append({'count': count, 'offset': idx_off, 'boundsMin': [minx, miny, minz], 'boundsMax': [maxx, maxy, maxz], 'nameLength': name_len, 'lightmap': [lm_w, lm_h], 'lodCount': lod_count})
        off += 52
    off = mesh_offset + 12 + align4(off - (mesh_offset + 12))

    for s in subsets:
        name_bytes = s['nameLength'] * 2
        raw = data[off:off + name_bytes]
        off += name_bytes
        off = mesh_offset + 12 + align4(off - (mesh_offset + 12))
        s['name'] = raw[:-2].decode('utf-16le', errors='replace') if name_bytes >= 2 else ''

    result = {
        'multiMesh': {'fileId': file_id, 'fileVersion': file_version, 'entriesOffset': entries_offset, 'meshCount': mesh_count, 'meshId': mesh_id, 'meshOffset': mesh_offset},
        'meshHeader': {'fileId': mesh_file_id, 'fileVersion': mesh_file_version, 'flags': mesh_flags, 'sizeInBytes': mesh_size},
        'mesh': {'vertexEntries': vertex_entries, 'targetEntries': target_entries, 'stride': stride, 'vertexDataSize': vertex_data_size, 'indexComponentType': index_component_type, 'indexDataSize': index_data_size, 'targetCount': target_count, 'subsetCount': subset_count, 'drawMode': draw_mode, 'winding': winding, 'vertexDataOffset': vertex_data_offset, 'indexDataOffset': index_data_offset},
        'attributes': entries,
        'subsets': subsets,
    }
    return result


def main():
    ap = argparse.ArgumentParser(description='Validate and inspect a Qt Quick 3D .mesh file')
    ap.add_argument('mesh_file')
    args = ap.parse_args()
    print(json.dumps(qt_mesh_validate(args.mesh_file), indent=2))


if __name__ == '__main__':
    main()
