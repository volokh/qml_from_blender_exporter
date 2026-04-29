# Qt Quick 3D Balsam Exporter — Blender Plugin

A Blender add-on that exports your scene in exactly the same structure as
Qt's **balsam** asset-import tool, ready to drop into a Qt Quick 3D project.

---

## What it exports

| Asset type        | Output                                      |
|-------------------|---------------------------------------------|
| Meshes            | `meshes/<name>.glb`  (glTF2 binary)         |
| Textures / Images | `images/<name>.png`                         |
| Materials         | Inline `PrincipledMaterial {}` in QML       |
| Cameras           | `PerspectiveCamera` / `OrthographicCamera`  |
| Lights            | `PointLight`, `DirectionalLight`, `SpotLight` |
| Animations        | `Timeline` + `KeyframeGroup` blocks         |
| Scene hierarchy   | Nested `Node {}` / `Model {}` tree          |
| Resource file     | `<scene>.qrc`                               |
| CMake snippet     | `CMakeLists_qt3d_snippet.txt`               |
| Manifest          | `export_manifest.json`                      |

---

## Output directory structure

```
MyScene/
├── MyScene.qml            ← main QML component (Node root)
├── MyScene.qrc            ← Qt resource file
├── CMakeLists_qt3d_snippet.txt
├── export_manifest.json
├── meshes/
│   ├── Cube.glb
│   └── Character.glb
└── images/
    ├── albedo.png
    ├── normal.png
    └── roughness.png
```

---

## Installation

1. Open Blender → **Edit → Preferences → Add-ons → Install**
2. Select `qt_balsam_exporter.zip`
3. Enable **Import-Export: Qt Quick 3D Balsam Exporter**

---

## Usage

**File → Export → Qt Quick 3D (.qml)**

Choose your output path (e.g. `MyProject/assets/MyScene.qml`).  
All sub-directories (`meshes/`, `images/`) are created automatically beside the `.qml`.

### Export Options

| Option              | Default | Description                                      |
|---------------------|---------|--------------------------------------------------|
| Cameras             | ✓       | Export cameras as QML camera nodes               |
| Lights              | ✓       | Export lights as QML light nodes                 |
| Animations          | ✓       | Export keyframe animations via Timeline          |
| Apply Modifiers     | ✓       | Apply mesh modifiers before exporting            |
| Selected Only       | ✗       | Export only currently selected objects           |

---

## Using the exported files in Qt Quick 3D

### 1. Add mesh conversion to CMakeLists.txt

Qt's balsam tool converts `.glb` → Qt's native `.mesh` at build time.
Add to your `CMakeLists.txt`:

```cmake
find_package(Qt6 REQUIRED COMPONENTS Quick3D)

# Auto-convert all exported .glb files
qt6_add_balsam(
    my_target
    FILES
        assets/meshes/Cube.glb
        assets/meshes/Character.glb
    OUTPUT_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/assets/meshes
)
```

Or if you use the `.qrc` directly:

```cmake
qt_add_resources(my_target "scene_assets"
    PREFIX "/"
    FILES
        assets/MyScene.qml
        assets/meshes/Cube.glb
        assets/images/albedo.png
)
```

### 2. Use in QML

```qml
import QtQuick
import QtQuick3D

View3D {
    anchors.fill: parent

    environment: SceneEnvironment {
        clearColor: "#222"
        backgroundMode: SceneEnvironment.Color
    }

    // Drop the exported component in directly
    MyScene {
        id: myScene
    }
}
```

### 3. Mesh sources

The exported QML references meshes as:
```qml
Model {
    source: "qrc:/meshes/Cube.glb"
    ...
}
```

After balsam conversion these become `"qrc:/meshes/Cube.mesh"` — 
update the `source` paths in your QML (or use a build step to do it automatically).

---

## Material mapping (Blender → Qt)

| Blender Principled BSDF input | Qt PrincipledMaterial property |
|-------------------------------|-------------------------------|
| Base Color                    | `baseColor` / `baseColorMap`  |
| Metallic                      | `metalness` / `metalnessMap`  |
| Roughness                     | `roughness` / `roughnessMap`  |
| Normal                        | `normalMap`                   |
| Emission Color                | `emissiveFactor` / `emissiveMap` |
| Alpha                         | `opacity` + `alphaMode`       |
| IOR                           | `indexOfRefraction`           |

---

## Coordinate system

Blender uses Z-up, right-hand.  Qt Quick 3D uses Y-up, left-hand.

The plugin automatically converts:

| Axis  | Blender | Qt Quick 3D |
|-------|---------|-------------|
| Right | +X      | +X          |
| Up    | +Z      | +Y          |
| Back  | +Y      | −Z          |

---

## Requirements

- Blender 3.0 or newer
- Qt 6.4 or newer (for full Quick 3D API coverage)
- The built-in Blender glTF2 exporter (enabled by default)

---

## License

MIT — free to use in commercial and open-source Qt projects.
