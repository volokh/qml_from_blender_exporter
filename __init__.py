from bpy.types import Operator
from bpy.props import (
    StringProperty, BoolProperty, EnumProperty,
    FloatProperty, IntProperty
)
from bpy_extras.io_utils import ExportHelper
from .qt_mesh_writer import write_mesh_file, extract_mesh_data
from .qt_mesh_validate import validate_qt_mesh
from .qt_bsdf_mat_importer import mat_to_quick3d
from .qt_hatch import qml_hatch_register, qml_hatch_unregister, is_qml_hatch, export_qml_hatch
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
    "name": "Qt Quick 3D Balsam Exporter Plugin",
    "author": "konvol",
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

# _PAD4 = b"\x00\x00\x00\x00"
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
'''
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


# ─────────────────────────────────────────────────────────────────
#  PrincipledMaterial QML block
# ─────────────────────────────────────────────────────────────────

def mat_qml(mat, img_dir, exported_images, indent=1):
    return "\n".join(mat_to_quick3d(mat, img_dir, exported_images, indent))


# ─────────────────────────────────────────────────────────────────
#  Light / Camera QML
# ─────────────────────────────────────────────────────────────────

_LIGHT_MAP = {'POINT': 'PointLight', 'SUN': 'DirectionalLight',
              'SPOT': 'SpotLight',   'AREA': 'AreaLight'}


def light_qml(obj, d=2, convert_coords=False):
    l = obj.data
    t = _LIGHT_MAP.get(l.type, 'PointLight')
    ind = "    " * d
    ind1 = "    " * (d+1)
    pos, rot, sc = qt_local_trs(obj, convert_coords)
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


def camera_qml(obj, d=2, convert_coords=False):
    cam = obj.data
    ind = "    " * d
    ind1 = "    " * (d+1)
    pos, rot, sc = qt_local_trs(obj, convert_coords)
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

    def _obj_qml(self, obj, d=2, offset=tuple((0, 0, 0))):
        hide_render_ = hide_render(obj)

        self.s.report({"INFO"}, f"processing object: {obj.name}, type: {obj.type}, instance_type: {obj.instance_type}, has_collection: {obj.instance_collection is not None}, hide_render: {hide_render_}, offset: {offset}")

        if hide_render_:
            return []

        blocks = []
        safe = sanitize(obj.name)
        nid = f"node_{safe}"
        self.node_ids[obj.name] = nid
        def I(n): return "    " * n

        pos, rot, sc = qt_local_trs(obj, self.s.convert_coords)
        pos = tuple(map(sum, zip(pos, inverse(offset))))
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
            blocks.append(light_qml(obj, d, self.s.convert_coords))
        elif obj.type == 'CAMERA' and self.s.export_cameras:
            blocks.append(camera_qml(obj, d, self.s.convert_coords))
        elif obj.type == 'EMPTY':
            lines = []
            if is_qml_hatch(obj):
                lines = export_qml_hatch(obj, nid, d)
                #blocks.append("\n".join(lines))
            else:
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
                    col_offs_ = qt_pos(obj.instance_collection.instance_offset)
                    for cobj in [o for o in obj.instance_collection.objects if o.parent is None]:
                        lines.extend(ln for ln in
                                     "\n".join(self._obj_qml(cobj, d + 1, col_offs_)).split("\n"))

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
        imports = ["import QtQuick", "import QtQuick3D", "", 'import LogicModule as LM']
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
        qml += f"Node {{\n    id: root\n    objectName: '{stem}'"
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
    qml_hatch_register()


def unregister():
    bpy.utils.unregister_class(EXPORT_OT_qt_balsam)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func)
    qml_hatch_unregister()


if __name__ == "__main__":
    register()
