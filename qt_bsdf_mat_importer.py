import bpy
import math

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


def sanitize(name: str) -> str:
    import re
    s = re.sub(r'[^A-Za-z0-9_]', '_', name or "")
    return ('_' + s if s and s[0].isdigit() else s) or '_'


def rgba3(c):
    return f"Qt.rgba({c[0]:.6f}, {c[1]:.6f}, {c[2]:.6f}, 1.0)"


def rgba4(v):
    a = v[3] if len(v) > 3 else 1.0
    return f"Qt.rgba({v[0]:.6f}, {v[1]:.6f}, {v[2]:.6f}, {a:.6f})"
    # return f"Qt.rgba({v[0]:.6f}, {v[1]:.6f}, {v[2]:.6f}, 1.0)"


def rgb(v):
    return f"Qt.vector3d({v[0]:.6f}, {v[1]:.6f}, {v[2]:.6f})"


def clamp01(x):
    return max(0.0, min(1.0, float(x)))


def first_linked_image(input_socket):
    if not input_socket or not input_socket.links:
        return None
    node = input_socket.links[0].from_node
    if node.type == 'TEX_IMAGE' and node.image:
        return node.image
    return None


def find_upstream_node(socket, node_type, visited=None):
    if visited is None:
        visited = set()
    if not socket or not socket.links:
        return None
    for link in socket.links:
        node = link.from_node
        key = id(node)
        if key in visited:
            continue
        visited.add(key)
        if node.type == node_type:
            return node
        for inp in node.inputs:
            found = find_upstream_node(inp, node_type, visited)
            if found:
                return found
    return None


def image_from_normal_input(normal_input):
    nm = find_upstream_node(normal_input, 'NORMAL_MAP')
    if not nm:
        return None, None
    strength = nm.inputs["Strength"].default_value if "Strength" in nm.inputs else getattr(
        nm, "strength", 1.0)
    color_in = nm.inputs.get("Color")
    img = first_linked_image(color_in)
    if not img and color_in and color_in.links:
        src = color_in.links[0].from_node
        if getattr(src, "image", None):
            img = src.image
    return img, strength


def image_from_socket_or_normal_chain(sock):
    img = first_linked_image(sock)
    if img:
        return img
    if sock and sock.links:
        for link in sock.links:
            node = link.from_node
            for inp in getattr(node, "inputs", []):
                img = first_linked_image(inp)
                if img:
                    return img
    return None


def node_val(node, name, default=None):
    def inp(name):
        return node.inputs.get(name)

    s = inp(name)
    return s.default_value if s is not None else default


def transparent_bsdf_to_quick3d(bsdf, mat, img_dir, exported_images, indent=0):
    ind = "    " * indent
    ind1 = "    " * (indent + 1)
    base_color = node_val(bsdf, 'Color', (1.0, 1.0, 1.0, 0.0))
    out = [f"{ind}PrincipledMaterial {{",
           f'{ind1}id: mat_{sanitize(mat.name)}',
           f'{ind1}objectName: "{mat.name}"',
           f'{ind1}baseColor: {rgba4(base_color)}',
           f'{ind1}alphaMode: PrincipledMaterial.Blend',
           # f'{ind1}alphaMode: PrincipledMaterial.Mask',
           f'{ind1}depthDrawMode: PrincipledMaterial.OpaquePrePassDepthDraw',
           # f'{ind1}cullMode: Material.NoCulling',
           f"{ind1}metalness: {mat.metallic:.4f}",
           f"{ind1}roughness: {mat.roughness:.4f}",
           f"{ind}}}"]
    return out


def default_to_quick3d(mat, img_dir, exported_images, indent=0):
    ind = "    " * indent
    ind1 = "    " * (indent + 1)
    out = [f"{ind}PrincipledMaterial {{",
           f'{ind1}id: mat_{sanitize(mat.name)}',
           f'{ind1}objectName: "{mat.name}"',
           f'{ind1}baseColor: {rgba4(mat.diffuse_color)}',
           f'{ind1}alphaMode: PrincipledMaterial.Opaque',
           f'{ind1}cullMode: Material.NoCulling']

    if not mat.use_nodes:
        out += [f"{ind1}metalness: {mat.metallic:.4f}",
                f"{ind1}roughness: {mat.roughness:.4f}"]

    out += [f"{ind}}}"]
    return out


def principled_bsdf_to_quick3d(bsdf, mat, img_dir, exported_images, indent=0):
    # if not mat or not mat.use_nodes or not mat.node_tree:
    #    return []

    # bsdf = next((n for n in mat.node_tree.nodes if n.type ==
    #            'BSDF_PRINCIPLED'), None)
    # if not bsdf:
    #    return []

    ind = "    " * indent
    ind1 = "    " * (indent + 1)

    def inp(name):
        return bsdf.inputs.get(name)

    def val(name, default=None):
        s = inp(name)
        return s.default_value if s is not None else default

    def tex_source_from_image(img):
        rel_ = exported_images.get(img.name) or save_image(img, img_dir)
        exported_images[img.name] = rel_
        # img.filepath.replace("\\", "/") if img.filepath else img.name
        return rel_

    lines = [
        f"{ind}PrincipledMaterial {{",
        f"{ind1}id: mat_{sanitize(mat.name)}",
        f'{ind1}objectName: "{mat.name}"',
    ]

    base_color = val("Base Color", (1.0, 1.0, 1.0, 1.0))
    alpha = float(val("Alpha", 1.0))
    metallic = float(val("Metallic", 0.0))
    roughness = float(val("Roughness", 0.5))
    # emission_color = socket_default(bsdf, "Emission Color", socket_default(bsdf, "Emission", (0.0, 0.0, 0.0, 1.0)))
    emission_color = val("Emission Color", val(
        "Emission", (0.0, 0.0, 0.0, 1.0)))
    emission_strength = float(val("Emission Strength", 1.0))
    transmission = float(val("Transmission Weight", val("Transmission", 0.0)))
    ior = float(val("IOR", 1.5))
    specular_ior_level = float(val("Specular IOR Level", val("Specular", 0.5)))

    clearcoat = float(val("Coat Weight", val("Clearcoat", 0.0)))
    clearcoat_rough = float(
        val("Coat Roughness", val("Clearcoat Roughness", 0.03)))
    coat_ior = float(val("Coat IOR", 1.5))
    coat_tint = val("Coat Tint", (1.0, 1.0, 1.0, 1.0))
    sheen = float(val("Sheen Weight", val("Sheen", 0.0)))
    sheen_tint = val("Sheen Tint", (0, 0, 0, 1.))
    anisotropic = float(val("Anisotropic", 0.0))
    anisotropic_rotation = float(val("Anisotropic Rotation", 0.0))
    subsurface = float(val("Subsurface Weight", val("Subsurface", 0.0)))
    subsurface_scale = float(val("Subsurface Scale", 0.1))
    subsurface_radius = val("Subsurface Radius", (1.0, 0.2, 0.1))
    thickness = float(val("Thickness", 0.0))

    base_img = image_from_socket_or_normal_chain(inp("Base Color"))
    metal_img = image_from_socket_or_normal_chain(inp("Metallic"))
    rough_img = image_from_socket_or_normal_chain(inp("Roughness"))
    ao_img = image_from_socket_or_normal_chain(inp("Occlusion"))
    emissive_img = image_from_socket_or_normal_chain(
        inp("Emission Color")) or image_from_socket_or_normal_chain(inp("Emission"))
    opacity_img = image_from_socket_or_normal_chain(inp("Alpha"))
    transmission_img = image_from_socket_or_normal_chain(inp(
        "Transmission Weight")) or image_from_socket_or_normal_chain(inp("Transmission"))
    normal_img, normal_strength = image_from_normal_input(inp("Normal"))

    clearcoat_img = image_from_socket_or_normal_chain(
        inp("Coat Weight")) or image_from_socket_or_normal_chain(inp("Clearcoat"))
    clearcoat_rough_img = image_from_socket_or_normal_chain(inp(
        "Coat Roughness")) or image_from_socket_or_normal_chain(inp("Clearcoat Roughness"))
    clearcoat_normal_img, clearcoat_normal_strength = image_from_normal_input(
        inp("Coat Normal"))
    # coat_nm = find_node_upstream(bsdf.inputs.get("Coat Normal"), "NORMAL_MAP")
    # if coat_nm:
    # clearcoat_normal_img = first_image_from_socket(coat_nm.inputs.get("Color"))

    if base_img:
        src = tex_source_from_image(base_img)
        lines.append(f'{ind1}baseColorMap: Texture {{ source: "{src}" }}')
    else:
        lines.append(f"{ind1}baseColor: {rgba4(base_color)}")

    lines.append(f"{ind1}metalness: {metallic:.6f}")
    if metal_img:
        src = tex_source_from_image(metal_img)
        lines.append(f'{ind1}metalnessMap: Texture {{ source: "{src}" }}')

    lines.append(f"{ind1}roughness: {roughness:.6f}")
    if rough_img:
        src = tex_source_from_image(rough_img)
        lines.append(f'{ind1}roughnessMap: Texture {{ source: "{src}" }}')

    if normal_img:
        src = tex_source_from_image(normal_img)
        lines.append(f'{ind1}normalMap: Texture {{ source: "{src}" }}')
        if normal_strength is not None:
            lines.append(f"{ind1}normalStrength: {float(normal_strength):.6f}")

    if ao_img:
        src = tex_source_from_image(ao_img)
        lines.append(f'{ind1}occlusionMap: Texture {{ source: "{src}" }}')

    emissive_rgb = (
        emission_color[0] * emission_strength,
        emission_color[1] * emission_strength,
        emission_color[2] * emission_strength,
    )
    if emissive_img:
        src = tex_source_from_image(emissive_img)
        lines.append(f'{ind1}emissiveMap: Texture {{ source: "{src}" }}')
        lines.append(f"{ind1}emissiveFactor: {rgb(emissive_rgb)}")
    elif any(c > 1e-6 for c in emissive_rgb):
        lines.append(f"{ind1}emissiveFactor: {rgb(emissive_rgb)}")

    '''
    if alpha < 0.999:
        lines.append(f"{ind1}opacity: {alpha:.6f}")
        lines.append(f"{ind1}alphaMode: PrincipledMaterial.Blend")
    if opacity_img:
        src = tex_source_from_image(opacity_img)
        lines.append(f'{ind1}opacityMap: Texture {{ source: "{src}" }}')
        if alpha >= 0.999:
            lines.append(f"{ind1}alphaMode: PrincipledMaterial.Blend")

    if not opacity_img and alpha >= 0.999:
        lines.append(f"{ind1}alphaMode: PrincipledMaterial.Opaque")
    '''
    if alpha < 0.999 or opacity_img:
        lines.append(f"{ind1}opacity: {alpha:.6f}")
        lines.append(f"{ind1}alphaMode: PrincipledMaterial.Blend")
        # lines.append(f"{ind1}blendMode: PrincipledMaterial.SourceOver")
        # lines.append(f"{ind1}alphaCutoff: 0.5")
        # lines.append(f"{ind1}invertOpacityMapValue: 0.0")
    else:
        lines.append(f"{ind1}alphaMode: PrincipledMaterial.Opaque")
        # lines.append(f"{ind1}alphaCutoff: 0.5")
        # lines.append(f"{ind1}invertOpacityMapValue: 0.0")

    if opacity_img:
        lines.append(
            f'{ind1}opacityMap: Texture {{ source: "{tex_source_from_image(opacity_img)}" }}')
        lines.append(f"{ind1}opacityChannel: PrincipledMaterial.A")

    if transmission > 1e-6:
        lines.append(f"{ind1}transmissionFactor: {transmission:.6f}")
    # lines.append(f"{ind1}transmissionChannel: PrincipledMaterial.R")

    if transmission_img:
        lines.append(
            f'{ind1}transmissionMap: Texture {{ source: "{tex_source_from_image(transmission_img)}" }}')
    # if transmission > 1e-6:
    #    lines.append(f"{ind1}transmissionFactor: {transmission:.6f}")

    '''
    if clearcoat > 0:
        lines.append(f"{ind1}clearcoatFresnelBias: 0.0")
        lines.append(f"{ind1}clearcoatFresnelPower: 5.0")
        lines.append(f"{ind1}clearcoatFresnelScale: 1.0")
        lines.append(f"{ind1}clearcoatFresnelScaleBiasEnabled: false")
    else:
        lines.append(f"{ind1}clearcoatFresnelBias: 0.0")
        lines.append(f"{ind1}clearcoatFresnelPower: 5.0")
        lines.append(f"{ind1}clearcoatFresnelScale: 1.0")
        lines.append(f"{ind1}clearcoatFresnelScaleBiasEnabled: false")
    '''

    if clearcoat_normal_img:
        lines.append(
            f'{ind1}clearcoatNormalMap: Texture {{ source: "{tex_source_from_image(clearcoat_normal_img)}" }}')

    lines.append(f"{ind1}clearcoatNormalStrength: {clearcoat_normal_strength}")

    lines.append(f"{ind1}clearcoatAmount: {clamp01(clearcoat):.6f}")
    lines.append(f"{ind1}clearcoatChannel: PrincipledMaterial.R")
    lines.append(
        f"{ind1}clearcoatRoughnessAmount: {clamp01(clearcoat_rough):.6f}")
    lines.append(f"{ind1}clearcoatRoughnessChannel: PrincipledMaterial.R")

    if clearcoat_img:
        lines.append(
            f'{ind1}clearcoatMap: Texture {{ source: "{tex_source_from_image(clearcoat_img)}" }}')

    if clearcoat_rough_img:
        lines.append(
            f'{ind1}clearcoatRoughnessMap: Texture {{ source: "{tex_source_from_image(clearcoat_rough_img)}" }}')

    lines.append(f"{ind1}indexOfRefraction: {ior:.6f}")
    lines.append(f"{ind1}thicknessFactor: {thickness:.6f}")

    # spec_amount = max(0.0, min(1.0, specular_ior_level))
    lines.append(f"{ind1}specularAmount: {clamp01(specular_ior_level):.6f}")

    lines.append(f"{ind1}cullMode: Material.NoCulling")
    lines.append(f"{ind}}}")
    return lines  # "\n".join(lines)


def mat_to_quick3d(mat, img_dir, exported_images, indent=0):
    if not mat or not mat.use_nodes or not mat.node_tree:
        return []

    nodes_ = []
    for node_ in mat.node_tree.nodes:
        nodes_.append(
            f'// type: {node_.type}, id_name: {node_.bl_idname}, id_name: {node_.bl_label}')

    for node in mat.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return nodes_ + principled_bsdf_to_quick3d(node, mat, img_dir, exported_images, indent)
        elif node.type == 'BSDF_TRANSPARENT':
            return nodes_ + transparent_bsdf_to_quick3d(node, mat, img_dir, exported_images, indent)

    # bsdf = next((n for n in mat.node_tree.nodes if n.type ==
            # 'BSDF_PRINCIPLED'), None)
    # if not bsdf:
    return nodes_ + default_to_quick3d(mat, img_dir, exported_images, indent)
