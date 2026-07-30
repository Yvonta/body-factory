#!/usr/bin/env python3
"""
merge_glbs.py -- combine two GLB files into one, pure Python (no Blender,
no Node.js). Appends every mesh/material/texture/skin/node from the second
file into the first, adjusting all internal index references, and merges
both binary buffers into one.

Usage:
    python3 merge_glbs.py avatar.glb clothing_1.glb merged.glb

Requires: pip install pygltflib --break-system-packages
"""
import sys
from pygltflib import GLTF2


def _extend(target_list, source_list):
    """Append source_list's items onto target_list, returning the offset
    (the original length of target_list) that every index INTO
    source_list's old positions must be shifted by."""
    offset = len(target_list)
    target_list.extend(source_list)
    return offset


def merge(path_a: str, path_b: str, path_out: str):
    a = GLTF2().load(path_a)
    b = GLTF2().load(path_b)

    if len(a.buffers) != 1 or len(b.buffers) != 1:
        raise ValueError(
            f"This script assumes exactly one buffer per file "
            f"(a has {len(a.buffers)}, b has {len(b.buffers)}) -- "
            f"true for a standard single-file GLB export, but not "
            f"guaranteed for every possible GLB."
        )

    # --- Merge the two binary blobs into one buffer ---
    blob_a = a.binary_blob()
    blob_b = b.binary_blob()
    # glTF buffer views must start at 4-byte-aligned offsets.
    pad = (-len(blob_a)) % 4
    buffer_offset = len(blob_a) + pad
    merged_blob = blob_a + (b"\x00" * pad) + blob_b
    a.buffers[0].byteLength = len(merged_blob)
    a.set_binary_blob(merged_blob)

    # --- Shift every index that a copied object from b will reference ---
    bv_offset = _extend(a.bufferViews, b.bufferViews)
    for bv in a.bufferViews[bv_offset:]:
        bv.buffer = 0  # both now point at the single merged buffer
        bv.byteOffset = (bv.byteOffset or 0) + buffer_offset

    acc_offset = _extend(a.accessors, b.accessors)
    for acc in a.accessors[acc_offset:]:
        if acc.bufferView is not None:
            acc.bufferView += bv_offset
        if acc.sparse is not None:
            acc.sparse.indices.bufferView += bv_offset
            acc.sparse.values.bufferView += bv_offset

    img_offset = _extend(a.images, b.images)
    for img in a.images[img_offset:]:
        if img.bufferView is not None:
            img.bufferView += bv_offset

    smp_offset = _extend(a.samplers, b.samplers)

    tex_offset = _extend(a.textures, b.textures)
    for tex in a.textures[tex_offset:]:
        if tex.source is not None:
            tex.source += img_offset
        if tex.sampler is not None:
            tex.sampler += smp_offset

    def _shift_texture_info(ti):
        if ti is not None and ti.index is not None:
            ti.index += tex_offset

    mat_offset = _extend(a.materials, b.materials)
    for mat in a.materials[mat_offset:]:
        if mat.pbrMetallicRoughness is not None:
            _shift_texture_info(mat.pbrMetallicRoughness.baseColorTexture)
            _shift_texture_info(mat.pbrMetallicRoughness.metallicRoughnessTexture)
        _shift_texture_info(mat.normalTexture)
        _shift_texture_info(mat.occlusionTexture)
        _shift_texture_info(mat.emissiveTexture)

    mesh_offset = _extend(a.meshes, b.meshes)
    for mesh in a.meshes[mesh_offset:]:
        for prim in mesh.primitives:
            if prim.material is not None:
                prim.material += mat_offset
            if prim.indices is not None:
                prim.indices += acc_offset
            for key in list(prim.attributes.__dict__.keys()):
                val = getattr(prim.attributes, key)
                if val is not None:
                    setattr(prim.attributes, key, val + acc_offset)
            if prim.targets:
                for target in prim.targets:
                    for key in list(target.keys()):
                        target[key] += acc_offset

    node_offset = _extend(a.nodes, b.nodes)
    for node in a.nodes[node_offset:]:
        if node.mesh is not None:
            node.mesh += mesh_offset
        if node.skin is not None:
            node.skin += (len(a.skins) - len(b.skins))  # placeholder, fixed below
        if node.children:
            node.children = [c + node_offset for c in node.children]

    skin_offset = _extend(a.skins, b.skins)
    for skin in a.skins[skin_offset:]:
        if skin.inverseBindMatrices is not None:
            skin.inverseBindMatrices += acc_offset
        if skin.skeleton is not None:
            skin.skeleton += node_offset
        skin.joints = [j + node_offset for j in skin.joints]
    # Now that skin_offset is known, fix the placeholder skin refs above.
    for node in a.nodes[node_offset:]:
        if node.skin is not None:
            node.skin = node.skin - (len(a.skins) - len(b.skins)) + skin_offset

    # --- Add b's root-level scene nodes into a's scene ---
    scene_index = a.scene if a.scene is not None else 0
    b_scene = b.scenes[b.scene if b.scene is not None else 0]
    a.scenes[scene_index].nodes.extend(n + node_offset for n in b_scene.nodes)

    a.save_binary(path_out)
    print(f"[SUCCESS] Merged {path_a} + {path_b} -> {path_out}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} avatar.glb clothing.glb merged.glb")
        sys.exit(1)
    merge(sys.argv[1], sys.argv[2], sys.argv[3])
