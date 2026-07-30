import bpy
import sys
import os
import importlib
from mathutils import Vector


def parse_args():
    if "--" not in sys.argv:
        print("Usage: blender -b -P generator_core.py -- <donor.blend> "
              "<image_path> <output_path> [--object realistic_body_male] "
              "[--landmarks path.json] [--face-bias 0.35] "
              "[--face-scale-margin 0.75] [--debug-mask] [--skip-head-warp] "
              "[--mpfb-live] [--clothing-fit ASSET_NAME] [--hair-fit ASSET_NAME]")
        sys.exit(1)

    idx = sys.argv.index("--")
    argv = sys.argv[idx + 1:]

    args = {
        "donor": argv[0],
        "image": argv[1],
        "output": argv[2],
        "object": "GEO-body_male_realistic",
        "landmarks": None,
        "face_bias": 0.35,
        "face_scale_margin": 0.75,
        "debug_mask": False,
        "skip_head_warp": False,
        "gender": "female",
        "age": 0.3,
        "weight": 0.5,
        "skin_tone_adjust": 0,
        "mpfb_live": False,
        "clothing_fit": None,
        "hair_fit": None,
    }

    rest = argv[3:]

    import json as _json
    if rest and rest[0].strip().startswith("{"):
        try:
            config = _json.loads(rest[0])
        except _json.JSONDecodeError as e:
            print(f"[ERROR] Could not parse JSON config argument: {rest[0]!r} ({e})")
            sys.exit(1)
        args["gender"] = config.get("gender", args["gender"])
        args["age"] = float(config.get("age", args["age"]))
        args["weight"] = float(config.get("weight", args["weight"]))
        args["skin_tone_adjust"] = int(config.get("skin_tone_adjust", args["skin_tone_adjust"]))
        rest = rest[1:]

    try:
        args["gender_value"] = max(0.0, min(1.0, float(args["gender"])))
        args["gender"] = "male" if args["gender_value"] >= 0.5 else "female"
    except (TypeError, ValueError):
        args["gender"] = str(args["gender"]).strip().lower()
        if args["gender"] not in ("male", "female"):
            args["gender"] = "female"
        args["gender_value"] = 1.0 if args["gender"] == "male" else 0.0

    args["age"] = max(0.0, min(1.0, args["age"]))
    args["weight"] = max(0.0, min(1.0, args["weight"]))
    args["skin_tone_adjust"] = max(-6, min(6, args["skin_tone_adjust"]))

    object_explicitly_set = False
    i = 0
    while i < len(rest):
        if rest[i] == "--object":
            args["object"] = rest[i + 1]; object_explicitly_set = True; i += 2
        elif rest[i] == "--landmarks":
            args["landmarks"] = rest[i + 1]; i += 2
        elif rest[i] == "--face-bias":
            args["face_bias"] = float(rest[i + 1]); i += 2
        elif rest[i] == "--face-scale-margin":
            args["face_scale_margin"] = float(rest[i + 1]); i += 2
        elif rest[i] == "--debug-mask":
            args["debug_mask"] = True; i += 1
        elif rest[i] == "--skip-head-warp":
            args["skip_head_warp"] = True; i += 1
        elif rest[i] == "--mpfb-live":
            args["mpfb_live"] = True; i += 1
        elif rest[i] == "--clothing-fit":
            args["clothing_fit"] = rest[i + 1]; args["mpfb_live"] = True; i += 2
        elif rest[i] == "--hair-fit":
            args["hair_fit"] = rest[i + 1]; args["mpfb_live"] = True; i += 2
        else:
            i += 1

    if not args["mpfb_live"] and not object_explicitly_set:
        args["object"] = (
            "GEO-body_female_realistic" if args["gender"] == "female"
            else "GEO-body_male_realistic"
        )

    return args


def clean_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()


def _find_mpfb_module_name():
    import addon_utils
    candidates = [
        "mpfb",
        "bl_ext.user_default.mpfb",
        "bl_ext.blender_org.mpfb",
        "bl_ext.system.mpfb",
    ]
    for name in candidates:
        try:
            addon_utils.enable(name, default_set=True, persistent=True)
        except Exception:
            pass
        try:
            importlib.import_module(name)
            importlib.import_module(f"{name}.services.humanservice")
            return name
        except Exception:
            continue
    sys.exit(1)


class _DummyOperator:
    def report(self, type_set, message):
        pass


def _remove_clothes_and_hair(mpfb_module, basemesh):
    SMALL_PART_VERTEX_THRESHOLD = 300

    for obj in list(bpy.data.objects):
        if obj.type == 'MESH' and obj != basemesh and len(obj.data.vertices) == 8 \
                and len(obj.data.polygons) == 6:
            bpy.data.objects.remove(obj, do_unlink=True)

    bpy.context.view_layer.objects.active = basemesh
    bpy.ops.object.select_all(action='DESELECT')
    basemesh.select_set(True)

    before_objs = set(o.name for o in bpy.data.objects)
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.separate(type='LOOSE')
    bpy.ops.object.mode_set(mode='OBJECT')

    new_pieces = [o for o in bpy.data.objects if o.name not in before_objs and o.type == 'MESH']
    all_pieces = [basemesh] + new_pieces
    all_pieces.sort(key=lambda o: len(o.data.vertices), reverse=True)

    body_piece = all_pieces[0]
    to_keep = [body_piece]
    to_delete = []
    
    for p in all_pieces[1:]:
        vcount = len(p.data.vertices)
        is_default_cube = (vcount == 8 and len(p.data.polygons) == 6)
        if is_default_cube or vcount > SMALL_PART_VERTEX_THRESHOLD:
            to_delete.append(p)
        else:
            to_keep.append(p)

    for p in to_delete:
        bpy.data.objects.remove(p, do_unlink=True)

    bpy.ops.object.select_all(action='DESELECT')
    for p in to_keep:
        p.select_set(True)
    bpy.context.view_layer.objects.active = body_piece
    if len(to_keep) > 1:
        bpy.ops.object.join()

    for keyword in ["hair", "genital"]:
        matching_groups = [vg for vg in body_piece.vertex_groups if keyword in vg.name.lower()]
        if matching_groups:
            bpy.context.view_layer.objects.active = body_piece
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='DESELECT')
            bpy.ops.object.mode_set(mode='OBJECT')
            vert_indices = set()
            for vg in matching_groups:
                for v in body_piece.data.vertices:
                    for g in v.groups:
                        if g.group == vg.index and g.weight > 0.01:
                            vert_indices.add(v.index)
            for vi in vert_indices:
                body_piece.data.vertices[vi].select = True
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.delete(type='VERT')
            bpy.ops.object.mode_set(mode='OBJECT')

    return body_piece


def _configure_mpfb_asset_root(mpfb_module):
    root = os.environ.get("MPFB_ASSET_ROOT", "/opt/mpfb-assets")
    if not os.path.isdir(root):
        return False
    try:
        addon_prefs = bpy.context.preferences.addons[mpfb_module].preferences
        setattr(addon_prefs, "mh_user_data", root)
        return True
    except Exception:
        return False


def generate_mpfb_human(gender_value, age, weight, standard_rig="cmu_mb",
                         viseme_pack="visemes02", remove_hair_genitals=True):
    mpfb_module = _find_mpfb_module_name()
    HumanService = importlib.import_module(f"{mpfb_module}.services.humanservice").HumanService
    TargetService = importlib.import_module(f"{mpfb_module}.services.targetservice").TargetService
    AssetService = importlib.import_module(f"{mpfb_module}.services.assetservice").AssetService

    basemesh = HumanService.create_human()
    try:
        HumanObjectProperties = importlib.import_module(
            f"{mpfb_module}.entities.objectproperties").HumanObjectProperties
    except ModuleNotFoundError:
        HumanObjectProperties = importlib.import_module(
            f"{mpfb_module}.services.humanobjectproperties").HumanObjectProperties

    for key, value in {"gender": gender_value, "age": age, "weight": weight}.items():
        try:
            HumanObjectProperties.set_value(key, value, entity_reference=basemesh)
        except Exception:
            pass

    TargetService.reapply_macro_details(basemesh)
    dummy_op = _DummyOperator()
    for rig_option in [standard_rig, "default", "default_no_toes", "game_engine"]:
        try:
            HumanService.add_builtin_rig(basemesh, rig_option, import_weights=True, operator=dummy_op)
            break
        except Exception:
            continue

    if remove_hair_genitals:
        basemesh = _remove_clothes_and_hair(mpfb_module, basemesh)

    basemesh.name = "Human"
    basemesh.data.name = "Human"
    return basemesh


def _bbox_max_dimension(obj):
    bpy.context.view_layer.update()
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    xs = [c.x for c in corners]
    ys = [c.y for c in corners]
    zs = [c.z for c in corners]
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def _sanity_check_and_correct_scale(clothes, human):
    try:
        human_h = _bbox_max_dimension(human)
        clothes_h = _bbox_max_dimension(clothes)
    except Exception:
        return

    if human_h <= 0 or clothes_h <= 0:
        return

    ratio = clothes_h / human_h
    if ratio > 1.5 or ratio < 0.15:
        correction = (1.0 / ratio) * 0.9
        local_corners = [Vector(c) for c in clothes.bound_box]
        center = sum(local_corners, Vector((0.0, 0.0, 0.0))) / len(local_corners)
        mesh_data = clothes.data
        for v in mesh_data.vertices:
            v.co = center + (v.co - center) * correction
        mesh_data.update()
        bpy.context.view_layer.update()


def _fit_mhclo_asset_to_human(mpfb_module, human, asset_name, asset_subdir="clothes", asset_label="clothes"):
    AssetService = importlib.import_module(f"{mpfb_module}.services.assetservice").AssetService
    mhclo_filename = f"{asset_name}.mhclo"
    asset_path = AssetService.find_asset_absolute_path(mhclo_filename, asset_subdir)
    if not asset_path:
        return None

    Mhclo = importlib.import_module(f"{mpfb_module}.entities.clothes.mhclo").Mhclo
    ClothesService = importlib.import_module(f"{mpfb_module}.services.clothesservice").ClothesService

    try:
        mhclo = Mhclo()
        mhclo.load(asset_path)
        asset_obj = mhclo.load_mesh(bpy.context)
    except Exception:
        return None

    if not asset_obj:
        return None

    if mhclo.material:
        try:
            MaterialService = importlib.import_module(f"{mpfb_module}.services.materialservice").MaterialService
            MakeSkinMaterial = importlib.import_module(f"{mpfb_module}.entities.material.makeskinmaterial").MakeSkinMaterial
            makeskin_material = MakeSkinMaterial()
            makeskin_material.populate_from_mhmat(mhclo.material)
            mat_name = os.path.basename(mhclo.material)
            blender_material = MaterialService.create_empty_material(mat_name, asset_obj)
            makeskin_material.apply_node_tree(blender_material)
        except Exception:
            pass

    try:
        ClothesService.fit_clothes_to_human(asset_obj, human, mhclo)
        mhclo.set_scalings(bpy.context, human)
    except Exception as e:
        raise e

    if asset_label != "hair":
        _sanity_check_and_correct_scale(asset_obj, human)

    asset_obj.parent = human
    return asset_obj


def run_asset_fit(args, asset_name, asset_subdir, asset_label):
    human = generate_mpfb_human(args["gender_value"], args["age"], args["weight"], remove_hair_genitals=False)
    mpfb_module = _find_mpfb_module_name()
    _configure_mpfb_asset_root(mpfb_module)
    asset_obj = _fit_mhclo_asset_to_human(mpfb_module, human, asset_name, asset_subdir=asset_subdir, asset_label=asset_label)

    if asset_obj is None:
        sys.exit(1)

    bpy.ops.object.select_all(action='DESELECT')
    asset_obj.select_set(True)
    bpy.context.view_layer.objects.active = asset_obj

    bpy.ops.export_scene.gltf(
        filepath=args["output"],
        export_format='GLB',
        export_materials='EXPORT',
        export_yup=True,
        export_apply=False,
        export_skins=True,
        export_morph=False,
        use_selection=True,
    )


def run_clothing_fit(args):
    run_asset_fit(args, args["clothing_fit"], asset_subdir="clothes", asset_label="clothes")


def run_hair_fit(args):
    run_asset_fit(args, args["hair_fit"], asset_subdir="hair", asset_label="hair")


def append_donor_body(donor_path, object_name):
    directory = os.path.join(donor_path, "Object")
    filepath = os.path.join(directory, object_name)
    bpy.ops.wm.append(filepath=filepath, directory=directory, filename=object_name)
    meshes = [o for o in bpy.context.selected_objects if o.type == 'MESH']
    human = max(meshes, key=lambda o: len(o.data.vertices))
    human.name = "Human"
    human.data.name = "Human"
    return human


def recenter_mesh_x(human):
    xs = [v.co.x for v in human.data.vertices]
    x_center = (min(xs) + max(xs)) / 2.0
    if abs(x_center) > 0.001:
        for v in human.data.vertices:
            v.co.x -= x_center


def project_face_texture(human, input_image_path, landmarks_path=None, output_path=None):
    mesh = human.data
    flat_mat = bpy.data.materials.new(name="SkinMaterial")
    flat_mat.use_nodes = True
    mesh.materials.clear()
    mesh.materials.append(flat_mat)
    return flat_mat, (0.76, 0.57, 0.47, 1.0)


def main():
    args = parse_args()
    clean_scene()

    if args["clothing_fit"]:
        run_clothing_fit(args)
        return

    if args["hair_fit"]:
        run_hair_fit(args)
        return

    if args["mpfb_live"]:
        human = generate_mpfb_human(args["gender_value"], args["age"], args["weight"])
    else:
        human = append_donor_body(args["donor"], args["object"])

    recenter_mesh_x(human)
    project_face_texture(human, args["image"], landmarks_path=args["landmarks"], output_path=args["output"])

    bpy.ops.export_scene.gltf(
        filepath=args["output"],
        export_format='GLB',
        export_materials='EXPORT',
        export_yup=True,
        export_apply=False,
        export_skins=True,
        export_morph=True,
    )


if __name__ == "__main__":
    main()
