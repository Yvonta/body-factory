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
              "[--mpfb-live] [--clothing-fit ASSET_NAME]\n"
              "  --landmarks: also auto-detects eye/nose/mouth position and "
              "scale for the face texture projection (not just head shape). "
              "Without it, --face-bias is used as a fallback heuristic.\n"
              "  --face-scale-margin: tune if eyes/nose/mouth land too high "
              "(raise it) or too low (lower it) on the mesh.\n"
              "  --mpfb-live: generate the body live via MPFB2 for this "
              "request (continuous gender/age/weight, real-time rig+viseme "
              "creation) instead of appending a static donor mesh. "
              "<donor.blend> is ignored in this mode (pass any placeholder).\n"
              "  --clothing-fit ASSET_NAME: skip normal avatar generation "
              "entirely. Rebuilds a body via generate_mpfb_human() from the "
              "gender/age/weight JSON config (same values used for the "
              "original avatar reproduce an identical body+skeleton), fits "
              "the named MPFB2 clothes asset to it, and exports ONLY the "
              "clothing mesh -- bound to that same skeleton/bind pose -- to "
              "<output>. <image_path>/<landmarks>/face options are ignored "
              "in this mode. Implies --mpfb-live.")
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
    }

    rest = argv[3:]

    # --- Positional JSON config (argv[3]) ---
    # BUGFIX: server.py sends a JSON blob (gender/age/weight) as the 4th
    # positional argument. The flag-parsing loop below only recognizes
    # fixed "--flag" strings, so this JSON token used to fall straight
    # into the `else: i += 1` branch and get silently discarded -- none of
    # gender/age/weight ever reached the mesh generation logic. Parse it
    # here, before the flag loop, and remove it from `rest` so the loop
    # below never sees it.
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
        # Integer "steps" darker (negative) or lighter (positive) applied
        # on top of whatever skin tone was determined (sampled from the
        # photo, or the flat fallback) -- see _apply_skin_tone_adjust().
        args["skin_tone_adjust"] = int(config.get("skin_tone_adjust", args["skin_tone_adjust"]))
        rest = rest[1:]
        print(f"[INFO] Parsed body config from argv: gender={args['gender']!r}, "
              f"age={args['age']:.2f}, weight={args['weight']:.2f}, "
              f"skin_tone_adjust={args['skin_tone_adjust']}")

    # Gender can arrive two ways: the CURRENT server.py contract sends the
    # word "male"/"female" (matches the old donor-object-selection path).
    # For the new --mpfb-live path's continuous control, gender is better
    # as a float in MakeHuman's own convention (0.0=female, 1.0=male,
    # confirmed via mpfb_setup_donor.py -- gender=0.0 produced a female
    # macro-detail shape). Accept either: try parsing as a float first,
    # fall back to the word mapping. args["gender_value"] is always a
    # float afterward, for the live path; args["gender"] stays a
    # male/female STRING for the old donor-object-selection path.
    try:
        args["gender_value"] = max(0.0, min(1.0, float(args["gender"])))
        args["gender"] = "male" if args["gender_value"] >= 0.5 else "female"
        print(f"[INFO] Gender given as a continuous float: "
              f"{args['gender_value']:.3f} (mapped to '{args['gender']}' "
              f"for the old donor-object path, if used).")
    except (TypeError, ValueError):
        args["gender"] = str(args["gender"]).strip().lower()
        if args["gender"] not in ("male", "female"):
            print(f"[WARNING] Unrecognized gender '{args['gender']}'; defaulting to 'female'.")
            args["gender"] = "female"
        args["gender_value"] = 1.0 if args["gender"] == "male" else 0.0

    args["age"] = max(0.0, min(1.0, args["age"]))
    args["weight"] = max(0.0, min(1.0, args["weight"]))
    args["skin_tone_adjust"] = max(-6, min(6, args["skin_tone_adjust"]))

    # --- Track whether --object was explicitly passed, so gender-based
    # resolution below only kicks in when it wasn't. ---
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
            args["clothing_fit"] = rest[i + 1]
            # Clothing-fit mode always rebuilds the body live via MPFB2 (the
            # only path that gives us the human + skeleton to fit against) --
            # never through the static-donor-mesh path.
            args["mpfb_live"] = True
            i += 2
        else:
            # BUGFIX: previously unrecognized args were dropped with zero
            # feedback. Now at least a warning shows up in the Blender log
            # so a future silently-ignored argument doesn't take an hour to
            # diagnose the way gender/age/weight did.
            print(f"[WARNING] Unrecognized argument '{rest[i]}' -- ignoring it.")
            i += 1

    # --- Resolve donor object name from gender, unless --object was given
    # (only relevant when NOT using --mpfb-live) ---
    if not args["mpfb_live"] and not object_explicitly_set:
        # NOTE: confirm this is the ACTUAL female object name in
        # human_base_meshes_bundle.blend (run list_blend_objects.py against
        # it). This is currently an unverified guess based on the known
        # male object's naming convention ('GEO-body_male_realistic').
        args["object"] = (
            "GEO-body_female_realistic" if args["gender"] == "female"
            else "GEO-body_male_realistic"
        )
        print(f"[INFO] No --object given; derived '{args['object']}' from gender='{args['gender']}'.")
    elif not args["mpfb_live"]:
        print(f"[INFO] --object explicitly set to '{args['object']}' (overrides gender).")

    return args


def clean_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()


def _find_mpfb_module_name():
    """Locate MPFB2's real top-level module name, and make sure it's
    actually ENABLED in this Blender session before returning it.

    Ported from mpfb_setup_donor.py, where this was worked out the hard
    way: `import mpfb` can succeed while resolving to a DIFFERENT,
    unrelated addon (the old pre-MPFB2 "MakeHuman Plugin for Blender",
    which historically also used the name "mpfb"). Only accept a
    candidate once `<name>.services.humanservice` is also confirmed
    importable under it, not just the bare top-level name.

    FIX (round 2): a real run showed MPFB present on disk and importable
    up to a point, but crashing with a TypeError from inside its OWN
    logservice.py -- because it wasn't actually enabled as an addon in
    THIS Blender session (confirmed by the accompanying message "The
    'bl_ext.user_default.mpfb' addon does not exist!?"). Installing the
    extension during the Docker BUILD doesn't guarantee every fresh
    headless Blender PROCESS at runtime automatically re-enables it --
    that's a separate, per-session step. addon_utils.enable() is the
    correct API for a script context (as opposed to
    bpy.ops.preferences.addon_enable, meant for UI-driven calls).

    Also fixed: the previous version only caught ModuleNotFoundError,
    so the TypeError above crashed straight through uncaught instead of
    being diagnosed. Now catches any Exception during the import attempt.
    """
    import addon_utils

    candidates = [
        "mpfb",
        "bl_ext.user_default.mpfb",
        "bl_ext.blender_org.mpfb",
        "bl_ext.system.mpfb",
    ]
    for name in candidates:
        try:
            enable_result = addon_utils.enable(name, default_set=True, persistent=True)
            if enable_result is not None:
                print(f"[INFO] addon_utils.enable('{name}') succeeded.")
        except Exception as e:
            print(f"[INFO] addon_utils.enable('{name}') raised "
                  f"{type(e).__name__}: {e} (may just mean this candidate "
                  f"isn't installed under this name -- trying others).")

        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        except Exception as e:
            print(f"[WARNING] import_module('{name}') raised "
                  f"{type(e).__name__}: {e} -- trying next candidate.")
            continue

        try:
            importlib.import_module(f"{name}.services.humanservice")
            print(f"[INFO] Confirmed MPFB2 (has services.humanservice) at: '{name}'")
            return name
        except ModuleNotFoundError:
            continue
        except Exception as e:
            print(f"[WARNING] import_module('{name}.services.humanservice') "
                  f"raised {type(e).__name__}: {e}. This usually means the "
                  f"module is on disk but not properly ENABLED in this "
                  f"Blender session -- addon_utils.enable() was already "
                  f"attempted above; if it still fails, MPFB's install may "
                  f"be genuinely broken/incomplete, not just unenabled.")
            continue

    matches = sorted(n for n in sys.modules if "mpfb" in n.lower())
    print(f"[ERROR] No candidate module both imported AND had "
          f"services.humanservice: {candidates}")
    if matches:
        print(f"[DIAGNOSTIC] sys.modules entries containing 'mpfb': {matches}")
    print("[ACTION NEEDED] --mpfb-live requires MPFB2 to be installed and "
          "enabled in this Blender environment (the one server.py's "
          "subprocess actually runs in -- confirm it's not just installed "
          "in a different Blender version/profile than what's being "
          "invoked). See https://extensions.blender.org/add-ons/mpfb/")
    sys.exit(1)


class _DummyOperator:
    """Minimal stand-in for a bpy.types.Operator instance, for MPFB
    internal calls that accept an operator= kwarg purely to call
    operator.report(...). No real operator exists in this headless
    per-request script."""
    def report(self, type_set, message):
        print(f"[MPFB REPORT] {type_set}: {message}")


def _remove_clothes_and_hair(mpfb_module, basemesh):
    """Remove hair/clothing geometry, and separately genital geometry
    (unnecessary detail, requested removed for both genders), from the
    human mesh.

    FIX (round 3): both previous approaches (separate objects, material
    slots) came back completely empty on real generations -- confirmed by
    actual log output showing 0 material slots and no matching separate
    objects, at every point in the pipeline. Yet hair/dress/glove shapes
    were still clearly visible.

    Root cause, found via direct connected-components analysis of an
    actual exported file: hair (and possibly other pieces) exist as
    TOPOLOGICALLY DISCONNECTED SHELLS merged into the same mesh object,
    with no distinguishing material or name at all -- e.g. a real export
    showed one dominant 13508-vertex component (70% of the mesh, the
    actual body) plus a separate, non-touching 2674-vertex shell (very
    likely hair) and a few other mid-size pieces, alongside many small
    (<100 vertex) pieces that are very likely eyes/teeth/nails/eyelashes
    -- details a talking avatar actually wants to KEEP.

    This uses Blender's own "Separate by Loose Parts" to split the mesh
    along its real connectivity, keeps the single largest piece (the
    body) plus any small piece below SMALL_PART_VERTEX_THRESHOLD
    (anatomical details), deletes anything else (hair/clothing-sized
    disconnected shells), then rejoins the kept pieces back into one
    mesh object since the rest of this pipeline assumes a single mesh.
    """
    SMALL_PART_VERTEX_THRESHOLD = 300  # eyes/teeth/nails/eyelashes are
    # all well under this in the real data (32-72 verts each); the
    # confirmed hair shell (2674) and other clothing-sized pieces (720,
    # 226, 200) are all well above it. Adjust based on real component
    # size printouts below if this ever needs tuning for a different mesh.

    # A stray Blender default-cube artifact has also shown up as a fully
    # INDEPENDENT object (not merged into the human mesh at all) in an
    # earlier MPFB test file (Cube, Human, Human.rig all separate
    # top-level objects). Check for that case too, before the loose-parts
    # separation below (which only operates on basemesh's own geometry
    # and wouldn't see an already-separate object).
    for obj in list(bpy.data.objects):
        if obj.type == 'MESH' and obj != basemesh and len(obj.data.vertices) == 8 \
                and len(obj.data.polygons) == 6 \
                and all(len(poly.vertices) == 4 for poly in obj.data.polygons):
            print(f"[INFO] Deleting standalone '{obj.name}' -- matches "
                  f"Blender's default cube signature as an independent "
                  f"object (not merged into the human mesh).")
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

    print(f"[INFO] Separated '{basemesh.name}' into {len(all_pieces)} loose "
          f"piece(s) by connectivity:")
    for p in all_pieces:
        print(f"[INFO]   '{p.name}': {len(p.data.vertices)} vertices")

    body_piece = all_pieces[0]  # largest = the actual body
    to_keep = [body_piece]
    to_delete = []
    for p in all_pieces[1:]:
        vcount = len(p.data.vertices)
        # FIX: a stray Blender default-cube artifact (8 verts, 6 quad
        # faces) has shown up in MPFB-generated scenes before (seen even
        # in a plain mpfb_setup_donor.py run, alongside 'Human' and
        # 'Human.rig' -- likely a leftover from something internal to
        # MPFB's human construction, not Blender's own startup scene,
        # since clean_scene() already runs before this). It's small
        # enough to otherwise slip through the "keep small anatomical
        # details" threshold below and get rejoined into the body,
        # visible as a floating box artifact. Detect and delete it
        # specifically regardless of the size threshold.
        is_default_cube = (
            len(p.data.vertices) == 8
            and len(p.data.polygons) == 6
            and all(len(poly.vertices) == 4 for poly in p.data.polygons)
        )
        if is_default_cube:
            to_delete.append(p)
            print(f"[INFO] Deleting '{p.name}' ({vcount} verts, 6 quad "
                  f"faces) -- matches Blender's default cube signature, "
                  f"a known stray artifact, not an anatomical detail.")
        elif vcount <= SMALL_PART_VERTEX_THRESHOLD:
            to_keep.append(p)
            print(f"[INFO] Keeping '{p.name}' ({vcount} verts, <= "
                  f"{SMALL_PART_VERTEX_THRESHOLD} threshold -- likely an "
                  f"anatomical detail like eyes/teeth/nails).")
        else:
            to_delete.append(p)
            print(f"[INFO] Deleting '{p.name}' ({vcount} verts, > "
                  f"{SMALL_PART_VERTEX_THRESHOLD} threshold -- likely "
                  f"hair/clothing).")

    for p in to_delete:
        bpy.data.objects.remove(p, do_unlink=True)

    # Rejoin the kept pieces back into one mesh object (basemesh), since
    # the rest of this pipeline assumes a single mesh throughout.
    bpy.ops.object.select_all(action='DESELECT')
    for p in to_keep:
        p.select_set(True)
    bpy.context.view_layer.objects.active = body_piece
    if len(to_keep) > 1:
        bpy.ops.object.join()
    print(f"[INFO] Rejoined {len(to_keep)} kept piece(s) into '{body_piece.name}' "
          f"-- {len(body_piece.data.vertices)} vertices total. "
          f"Deleted {len(to_delete)} hair/clothing piece(s).")

    # --- Hair: separate mechanism needed. ---
    # FIX: direct connected-components analysis of a real export showed
    # NO large disconnected shell remaining after the above (clothing was
    # correctly removed as a genuine separate shell) -- yet hair was
    # still visibly present. This means hair is WELDED directly to the
    # scalp (same continuous surface, no seam), not a separate loose part
    # at all -- the loose-parts approach above structurally cannot catch
    # it. MakeHuman-style tools commonly tag body regions with named
    # VERTEX GROUPS for exactly this situation. Checking for one here,
    # with full diagnostic listing either way so the real group names
    # (if this guess is wrong) are visible for a precise fix.
    print(f"[INFO] '{body_piece.name}' vertex groups "
          f"({len(body_piece.vertex_groups)}): "
          f"{[vg.name for vg in body_piece.vertex_groups]}")

    def _delete_by_vertex_group_keyword(keyword, label):
        """Delete all vertices belonging to any vertex group whose name
        contains `keyword`. Confirmed real group names on this mesh
        (from an actual generation's vertex group listing) include
        'helper-hair', 'genitals', and 'helper-genital' -- this same
        mechanism handles all of them by keyword.
        """
        matching_groups = [vg for vg in body_piece.vertex_groups
                            if keyword in vg.name.lower()]
        if not matching_groups:
            print(f"[WARNING] No vertex group name contained '{keyword}' "
                  f"({label}) -- see the full vertex group listing above "
                  f"for the real names on this mesh if this needs fixing.")
            return 0

        print(f"[INFO] Found {label} vertex group(s): "
              f"{[vg.name for vg in matching_groups]} -- deleting their vertices.")
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
        print(f"[INFO] Deleted {len(vert_indices)} vertices belonging to "
              f"{label} vertex group(s).")
        return len(vert_indices)

    _delete_by_vertex_group_keyword("hair", "hair")
    # Removes both the 'genitals' group and 'helper-genital' in one pass,
    # since both contain "genital" -- unnecessary detail for this avatar's
    # use case, requested for both genders.
    _delete_by_vertex_group_keyword("genital", "genitals")

    return body_piece


MPFB_ASSET_ROOT_ENV_VAR = "MPFB_ASSET_ROOT"
MPFB_ASSET_ROOT_DEFAULT = "/opt/mpfb-assets"


def _configure_mpfb_asset_root(mpfb_module):
    """Point MPFB2 at the baked-in clothing asset directory (see the
    Dockerfile step that COPYs asset packs to MPFB_ASSET_ROOT_DEFAULT) so
    AssetService can find clothes placed there under clothes/<name>/.

    Set at the START of every headless run rather than relying on a saved
    Blender userpref.blend persisting across container restarts -- same
    reasoning as _find_mpfb_module_name() re-enabling the addon fresh every
    process instead of trusting install-time state.

    NOTE -- UNVERIFIED ATTRIBUTE NAME: the real preference name has moved
    across MPFB2 versions (same situation as everywhere else in this file
    flagged "UNVERIFIED"). Confirm the actual name by grepping your
    installed MPFB2's _preferences.py / locationservice.py for
    'asset.?root|secondary|extra.?root', then trim the candidate list
    below to just the real one.
    """
    root = os.environ.get(MPFB_ASSET_ROOT_ENV_VAR, MPFB_ASSET_ROOT_DEFAULT)
    if not os.path.isdir(root):
        print(f"[WARNING] MPFB asset root '{root}' does not exist on disk -- "
              f"clothing assets placed there won't be found. Check the "
              f"Dockerfile COPY step / {MPFB_ASSET_ROOT_ENV_VAR} env var.")
        return False

    try:
        addon_prefs = bpy.context.preferences.addons[mpfb_module].preferences
    except (KeyError, AttributeError) as e:
        print(f"[WARNING] Could not get addon preferences for "
              f"'{mpfb_module}': {e} -- is it actually enabled in this "
              f"session? (_find_mpfb_module_name() should have handled "
              f"that already.)")
        return False

    # CONFIRMED (real log output, 2026-07-27): 'mh_user_data' is the real
    # attribute -- MPFB2's own locationservice logged "mh_user_data
    # explicitly set to /opt/mpfb-assets" after this fired. Kept as a
    # single-item list (not a bare assignment) so a future MPFB2 version
    # renaming it again just means adding back to this list, not rewriting
    # the whole function.
    candidate_attrs = ["mh_user_data"]
    for attr in candidate_attrs:
        if hasattr(addon_prefs, attr):
            try:
                setattr(addon_prefs, attr, root)
                print(f"[INFO] Set MPFB2 preference '{attr}' = '{root}' "
                      f"(clothing assets will be discovered from here).")
                return True
            except Exception as e:
                print(f"[WARNING] Setting addon_prefs.{attr} = '{root}' "
                      f"failed: {e}")

    print(f"[ERROR] None of the candidate preference attributes "
          f"{candidate_attrs} exist on this MPFB2 install's preferences -- "
          f"grep your installed _preferences.py for the real name and add "
          f"it to candidate_attrs above.")
    return False


def generate_mpfb_human(gender_value, age, weight, standard_rig="cmu_mb",
                         viseme_pack="visemes02", remove_hair_genitals=True):
    """Generate a rigged, viseme-ready human LIVE via MPFB2, replacing the
    static-donor-mesh append_donor_body() path. Used when --mpfb-live is
    set. Every call in here is copied from mpfb_setup_donor.py's
    successful run (confirmed working: rig=163 bones correctly linked,
    15/15 visemes loaded) -- not new guesses.

    gender_value, age, weight are all continuous floats in [0, 1]
    (MakeHuman's own convention: gender 0.0=female, 1.0=male).

    CHANGED: rig switched to "cmu_mb" for CMU mocap BVH compatibility (see
    apply_cmu_mocap_animation()). Falls back to "default" etc. if cmu_mb
    isn't available for some reason -- but note the retargeting script
    below assumes cmu_mb bone naming, so a silent fallback here would mean
    retargeting fails downstream with a bone-name mismatch, not silently.

    Returns the mesh Object (named "Human", matching what the rest of the
    pipeline expects).
    """
    mpfb_module = _find_mpfb_module_name()
    HumanService = importlib.import_module(f"{mpfb_module}.services.humanservice").HumanService
    TargetService = importlib.import_module(f"{mpfb_module}.services.targetservice").TargetService
    AssetService = importlib.import_module(f"{mpfb_module}.services.assetservice").AssetService

    print(f"[INFO] --mpfb-live: creating base human via HumanService.create_human() "
          f"(gender={gender_value:.3f}, age={age:.3f}, weight={weight:.3f})...")
    basemesh = HumanService.create_human()
    print(f"[INFO] Created base human object: {basemesh.name}")

    # --- Set macro details (gender/age/weight) ---
    try:
        HumanObjectProperties = importlib.import_module(
            f"{mpfb_module}.entities.objectproperties").HumanObjectProperties
    except ModuleNotFoundError:
        HumanObjectProperties = importlib.import_module(
            f"{mpfb_module}.services.humanobjectproperties").HumanObjectProperties

    for key, value in {"gender": gender_value, "age": age, "weight": weight}.items():
        try:
            HumanObjectProperties.set_value(key, value, entity_reference=basemesh)
            print(f"[INFO] Set macro detail '{key}' = {value:.3f}")
        except AttributeError as e:
            print(f"[WARNING] HumanObjectProperties.set_value('{key}', ...) failed: {e}")

    TargetService.reapply_macro_details(basemesh)
    print("[INFO] Reapplied macro details (gender/age/weight now baked "
          "into the base shape).")

    # --- Add rig ---
    dummy_op = _DummyOperator()
    rig_added = False
    for rig_option in [standard_rig, "default", "default_no_toes", "game_engine"]:
        try:
            HumanService.add_builtin_rig(
                basemesh, rig_option, import_weights=True, operator=dummy_op)
            print(f"[INFO] Added rig via HumanService.add_builtin_rig(basemesh, "
                  f"'{rig_option}', import_weights=True)")
            rig_added = True
            break
        except Exception as e:
            print(f"[WARNING] add_builtin_rig(basemesh, '{rig_option}', ...) "
                  f"raised {type(e).__name__}: {e}")
    if not rig_added:
        print("[WARNING] Could not add a rig -- this avatar will export "
              "without a skeleton. Continuing anyway (face texture "
              "pipeline doesn't require a rig).")

    # --- Load viseme shape keys ---
    try:
        names = AssetService.get_asset_names_in_pack(viseme_pack)
        print(f"[INFO] get_asset_names_in_pack('{viseme_pack}') returned "
              f"{len(names) if names else 0} names.")
    except Exception as e:
        names = None
        print(f"[WARNING] get_asset_names_in_pack('{viseme_pack}') raised "
              f"{type(e).__name__}: {e} -- is the '{viseme_pack}' asset "
              f"pack actually installed in this Blender environment?")

    loaded = 0
    if names:
        for name in names:
            try:
                path = TargetService.target_full_path(name)
                TargetService.load_target(basemesh, path, weight=0.0, name=name)
                loaded += 1
            except Exception as e:
                print(f"[WARNING] Loading viseme '{name}' failed: {e}")
    print(f"[INFO] Loaded {loaded}/{len(names) if names else 0} viseme "
          f"shape keys from '{viseme_pack}'.")

    # FIX (ordering): this used to run right after create_human(), but a
    # real log showed the mesh had ZERO material slots at that point --
    # confirming clothing/hair (whatever form they take) get attached by
    # a LATER step in this pipeline (rig setup, macro reapplication, or
    # something else), not by create_human() itself. Running this check
    # here instead, after every other setup step, gives it an actual
    # chance of finding what it's looking for.
    # NEW: skippable for the clothing-fit reference body (see
    # remove_hair_genitals param). Real diagnosis (2026-07-30): our own
    # hair/genital vertex removal changes this mesh's vertex COUNT --
    # confirmed our post-removal count is exactly 14136, and
    # ClothesService.set_up_rigging() failed with "index 17150 out of
    # range, size 14136" -- its internal weight-transfer looks up
    # vertices by INDEX against the topology .mhclo calibration data
    # expects, which needs MORE vertices than our reduced mesh has (very
    # likely the pristine, unmodified MakeHuman basemesh vertex count).
    # The clothing-fit reference body is NEVER itself exported (only the
    # garment + armature are), so there's no visual reason to strip
    # hair/genitals from it at all -- keeping the pristine vertex
    # topology there should let real rigging succeed instead of falling
    # back to unskinned (which we've now confirmed positions garments
    # essentially at random, sometimes far above/below the body).
    if remove_hair_genitals:
        print("[INFO] Checking for clothing/hair to remove (moved to run after "
              "rig/macro/viseme setup, since they weren't present immediately "
              "after create_human()).")
        basemesh = _remove_clothes_and_hair(mpfb_module, basemesh)
    else:
        print("[INFO] Skipping hair/genital removal -- keeping the pristine "
              "vertex topology (needed for clothing rig weight-transfer to "
              "find the vertex indices it expects; this body is only used "
              "as a rigging reference here, never exported itself).")

    basemesh.name = "Human"
    basemesh.data.name = "Human"
    print(f"[INFO] --mpfb-live human generation complete: '{basemesh.name}', "
          f"{len(basemesh.data.vertices)} vertices, rig_added={rig_added}, "
          f"visemes_loaded={loaded}.")
    return basemesh


def _bbox_height(obj):
    """World-space bounding-box height (Z extent) of an object, accounting
    for its current transform -- used to sanity-check clothing scale."""
    bpy.context.view_layer.update()
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    zs = [c.z for c in corners]
    return max(zs) - min(zs)


def _sanity_check_and_correct_scale(clothes, human):
    """mhclo.set_scalings() (the real MPFB2 scale step, called right
    before this) SHOULD already correct clothes to the human's
    proportions. In practice, some third-party/community .mhclo files --
    as opposed to the official curated MakeHuman asset library -- ship
    with missing or miscalibrated scale data, leaving the garment wildly
    oversized or undersized after fitting even though the fitting call
    itself succeeded without error.

    This is a heuristic safety net, not a proper fix for the underlying
    asset data: if a single garment's bounding-box height comes out
    wildly disproportionate to the whole body's height, apply a rough
    corrective uniform scale. A well-calibrated garment covering part of
    the body should never come close to matching or exceeding the full
    body's own height -- if it does, something upstream (this asset's
    calibration) is already wrong, and this only makes the result usable
    rather than obviously broken.
    """
    try:
        human_h = _bbox_height(human)
        clothes_h = _bbox_height(clothes)
    except Exception as e:
        print(f"[WARNING] Could not compute bounding boxes for scale "
              f"sanity check: {e}")
        return

    if human_h <= 0 or clothes_h <= 0:
        return

    ratio = clothes_h / human_h
    if ratio > 1.5 or ratio < 0.15:
        # Pull it back toward roughly matching the body's own scale --
        # deliberately a rough, conservative correction (not a precise
        # fit), since this is patching over bad source data, not
        # recomputing the fit properly.
        correction = (1.0 / ratio) * 0.9
        print(f"[WARNING] Clothes bounding-box height ({clothes_h:.3f}) is "
              f"wildly mismatched vs human ({human_h:.3f}), ratio={ratio:.2f} "
              f"-- this asset's .mhclo is likely missing proper scale "
              f"calibration. Applying corrective uniform scale ×{correction:.3f} "
              f"as a fallback (not a substitute for fixing the source asset).")
        # FIX: scale the mesh's VERTEX DATA directly, around the garment's
        # own local bounding-box center -- NOT via the object's .scale
        # property. Object-level scaling happens around whatever the
        # object's origin point is (often not the garment's own visual
        # center for third-party assets), which shifts the mesh's
        # apparent position as a side effect of "correcting" its size.
        # Confirmed via a real exported GLB: the garment came out
        # correctly proportioned but floating well below the body,
        # because this mesh isn't skinned (rigging failed for it, see
        # the warning below) and gets exported as a static parented mesh
        # carrying its raw object-level scale as a glTF node transform.
        # Scaling vertex data around the mesh's own bbox center fixes
        # the size without touching position at all, and avoids leaving
        # any non-identity node transform in the export.
        local_corners = [Vector(c) for c in clothes.bound_box]
        center = sum(local_corners, Vector((0.0, 0.0, 0.0))) / len(local_corners)
        mesh_data = clothes.data
        for v in mesh_data.vertices:
            v.co = center + (v.co - center) * correction
        mesh_data.update()
        bpy.context.view_layer.update()
    else:
        print(f"[INFO] Clothes/human bounding-box ratio {ratio:.2f} looks "
              f"reasonable -- no scale correction applied.")


def _fit_clothes_to_human(mpfb_module, human, clothes_name):
    """Load and fit an MPFB2 clothes asset onto `human`, binding it to the
    same armature `human` is already rigged with (via add_builtin_rig in
    generate_mpfb_human()).

    CONFIRMED (real source read, 2026-07-27, from both assetservice.py AND
    the actual MPFB2 UI operator that does this in the Blender interface --
    ui/apply_assets/loadclothes/operators/loadclothes.py). The real
    sequence is NOT a single fit call -- it's: parse the .mhclo file into
    an Mhclo entity, import the mesh it references (giving you the actual
    clothes Object fit_clothes_to_human needs -- passing a bare file path
    there is what caused the earlier "not an instance of Object" error),
    THEN fit, THEN separately bind it to the skeleton via set_up_rigging
    (fit_clothes_to_human only matches shape/position, it does not itself
    copy bone weights -- set_up_rigging is what makes the garment actually
    deform with the body's animation).
    """
    AssetService = importlib.import_module(f"{mpfb_module}.services.assetservice").AssetService

    # AssetService.find_asset_absolute_path(asset_path_fragment, asset_subdir="clothes")
    # does an EXACT FILENAME match against files on disk (`if filename in files`) --
    # it does NOT append an extension for you.
    mhclo_filename = f"{clothes_name}.mhclo"
    clothes_path = None
    try:
        clothes_path = AssetService.find_asset_absolute_path(mhclo_filename, "clothes")
        if clothes_path:
            print(f"[INFO] Resolved clothes asset '{clothes_name}' -> "
                  f"{clothes_path} (via AssetService.find_asset_absolute_path)")
    except Exception as e:
        print(f"[WARNING] AssetService.find_asset_absolute_path('{mhclo_filename}', "
              f"'clothes') failed: {e}")

    if not clothes_path:
        print(f"[ERROR] Could not resolve a file path for clothes asset "
              f"'{clothes_name}' (looked for filename '{mhclo_filename}' "
              f"under the 'clothes' asset subdir).")
        return None

    Mhclo = importlib.import_module(f"{mpfb_module}.entities.clothes.mhclo").Mhclo
    ClothesService = importlib.import_module(f"{mpfb_module}.services.clothesservice").ClothesService

    # --- Step 1: parse the .mhclo file and import the mesh it references.
    # THIS is the real "clothes" Object -- not the file path itself. ---
    try:
        mhclo = Mhclo()
        mhclo.load(clothes_path)
        clothes = mhclo.load_mesh(bpy.context)
    except Exception as e:
        print(f"[ERROR] Mhclo().load('{clothes_path}') / load_mesh() failed: {e}")
        return None

    if not clothes:
        print(f"[ERROR] mhclo.load_mesh() returned no object for "
              f"'{clothes_path}' -- failed to import the clothes mesh.")
        return None
    print(f"[INFO] Imported clothes mesh object '{clothes.name}' from "
          f"'{clothes_path}'.")

    # --- Step 1.5: build a REAL textured material, matching the actual
    # MPFB2 load-clothes operator's MAKESKIN branch (confirmed via direct
    # source inspection, 2026-07-29). Without this, mhclo.load_mesh()
    # leaves whatever bare/default material the raw .obj import produced,
    # which is why clothing textures weren't showing at all before this. ---
    if mhclo.material:
        try:
            MaterialService = importlib.import_module(f"{mpfb_module}.services.materialservice").MaterialService
            MakeSkinMaterial = importlib.import_module(f"{mpfb_module}.entities.material.makeskinmaterial").MakeSkinMaterial
            makeskin_material = MakeSkinMaterial()
            makeskin_material.populate_from_mhmat(mhclo.material)
            mat_name = os.path.basename(mhclo.material)
            blender_material = MaterialService.create_empty_material(mat_name, clothes)
            makeskin_material.apply_node_tree(blender_material)
            print(f"[INFO] Built real textured material '{mat_name}' for "
                  f"'{clothes.name}' via MakeSkinMaterial.populate_from_mhmat() "
                  f"+ apply_node_tree() (from mhclo.material='{mhclo.material}').")
        except Exception as e:
            print(f"[WARNING] Building the real MakeSkin material failed "
                  f"(continuing with whatever material load_mesh() left in "
                  f"place -- likely untextured): {e}")
    else:
        print(f"[WARNING] mhclo.material is empty for '{clothes.name}' -- "
              f"this .mhclo doesn't reference a .mhmat material file, so "
              f"there's no texture to build regardless of the code above.")

    # --- Step 2: fit it to this specific human's shape (position/scale to
    # match the body's macrodetail values -- NOT bone weights yet). ---
    try:
        ClothesService.fit_clothes_to_human(clothes, human, mhclo)
        mhclo.set_scalings(bpy.context, human)
        print(f"[INFO] Fitted '{clothes.name}' to human via "
              f"ClothesService.fit_clothes_to_human() + mhclo.set_scalings().")
    except Exception as e:
        print(f"[WARNING] Fitting step failed (continuing anyway -- "
              f"clothes may be mispositioned): {e}")

    _sanity_check_and_correct_scale(clothes, human)

    # --- Step 3: find the human's armature, and bind the garment to it
    # (bone weights) via ClothesService.set_up_rigging() -- this is what
    # makes it actually deform with the body's animation, distinct from
    # just being positioned correctly at rest. ---
    armature = None
    for mod in human.modifiers:
        if mod.type == 'ARMATURE' and mod.object:
            armature = mod.object
            break

    if armature is not None:
        try:
            clothes.location = (0.0, 0.0, 0.0)
            ClothesService.set_up_rigging(
                human, clothes, armature, mhclo,
                interpolate_weights=True, import_subrig=False, import_weights=True)
            print(f"[INFO] Rigged '{clothes.name}' to armature "
                  f"'{armature.name}' via ClothesService.set_up_rigging().")
        except Exception as e:
            print(f"[WARNING] ClothesService.set_up_rigging(...) failed: "
                  f"{e} -- clothes will be parented but NOT bone-weighted "
                  f"(won't deform with animation). Falling back to simple "
                  f"parenting.")
            clothes.parent = human
    else:
        print("[WARNING] No armature found on human -- parenting clothes "
              "without bone weights (won't deform with animation).")
        clothes.parent = human

    return clothes


def run_clothing_fit(args):
    """Handle --clothing-fit: rebuild a body from the given gender/age/
    weight (identical to how the matching avatar's own body was built,
    since generate_mpfb_human() is deterministic for the same inputs and
    fixed rig_option order), fit the requested clothing asset to it, and
    export ONLY the clothing mesh -- skinned to that same armature/bind
    pose -- to args["output"]. The face/photo/landmarks pipeline is not
    involved at all in this mode.
    """
    print(f"[INFO] --clothing-fit '{args['clothing_fit']}': building body "
          f"(gender={args['gender_value']:.3f}, age={args['age']:.2f}, "
          f"weight={args['weight']:.2f}) to fit clothing against...")
    human = generate_mpfb_human(args["gender_value"], args["age"], args["weight"],
                                 remove_hair_genitals=False)

    mpfb_module = _find_mpfb_module_name()
    _configure_mpfb_asset_root(mpfb_module)
    clothes_obj = _fit_clothes_to_human(mpfb_module, human, args["clothing_fit"])

    if clothes_obj is None:
        print(f"[ERROR] Could not fit clothing asset "
              f"'{args['clothing_fit']}' -- see warnings above for which "
              f"API candidates were tried.")
        sys.exit(1)

    # Find the armature human was rigged with, so it can be exported
    # alongside the clothing mesh (glTF needs the armature object present
    # to write correct joint/skin data, same reasoning as the main export
    # path's export_apply=False comment below).
    armature = None
    for mod in human.modifiers:
        if mod.type == 'ARMATURE' and mod.object:
            armature = mod.object
            break

    if armature is None:
        print("[WARNING] Could not find the body's armature -- exporting "
              "the clothing mesh without a skeleton. It will NOT bind to "
              "an avatar's bones in the viewer.")

    bpy.ops.object.select_all(action='DESELECT')
    clothes_obj.select_set(True)
    if armature is not None:
        armature.select_set(True)
    bpy.context.view_layer.objects.active = clothes_obj

    # Same export_apply=False reasoning as the main avatar export: the
    # Armature modifier must stay un-applied for the exporter to write
    # correct skin/joint data instead of static baked geometry.
    bpy.ops.export_scene.gltf(
        filepath=args["output"],
        export_format='GLB',
        export_materials='EXPORT',
        export_yup=True,
        export_apply=False,
        export_skins=True,
        export_morph=True,
        use_selection=True,
    )
    print(f"[SUCCESS] Exported clothing-only GLB to: {args['output']}")


def _try_append_armature(donor_path, mesh_object_name):
    """Attempt to append a same-named-with-'.rig'-suffix Armature object
    from the same donor .blend (matching the exact naming MPFB2's
    add_builtin_rig produces: mesh 'Human' + armature 'Human.rig',
    confirmed from a real generated donor file).

    WHY THIS EXISTS: appending a single object via bpy.ops.wm.append does
    NOT automatically bring along a separately-named object it merely
    references (a parent, or an Armature modifier's target) -- a
    well-known Blender gotcha. Without this, a rigged donor mesh would
    still export with a broken/empty Armature modifier (object=None) and
    no skinning data at all, even though the donor .blend itself is
    correctly rigged.

    Returns the appended armature Object, or None if no such object
    exists in this donor (expected/harmless for older, unrigged donor
    meshes that haven't been through the MPFB rigging setup).
    """
    armature_name = f"{mesh_object_name}.rig"
    directory = os.path.join(donor_path, "Object")
    filepath = os.path.join(directory, armature_name)
    bpy.ops.object.select_all(action='DESELECT')
    try:
        bpy.ops.wm.append(filepath=filepath, directory=directory, filename=armature_name)
    except RuntimeError as e:
        print(f"[INFO] No armature object named '{armature_name}' in this "
              f"donor (append failed: {e}) -- proceeding without a rig "
              f"link. Expected/harmless for donor meshes that haven't been "
              f"through the MPFB rigging setup yet.")
        return None

    appended = [o for o in bpy.context.selected_objects if o.type == 'ARMATURE']
    if not appended:
        print(f"[INFO] Append of '{armature_name}' completed but no "
              f"ARMATURE-type object was selected afterward -- treating "
              f"as 'no rig present' for this donor.")
        return None

    armature_obj = appended[0]
    print(f"[INFO] Appended armature object '{armature_obj.name}' "
          f"({len(armature_obj.data.bones)} bones) from donor.")
    return armature_obj


def append_donor_body(donor_path, object_name):
    if not os.path.exists(donor_path):
        print(f"[ERROR] Donor .blend not found at {donor_path}")
        sys.exit(1)

    directory = os.path.join(donor_path, "Object")
    filepath = os.path.join(directory, object_name)

    bpy.ops.wm.append(filepath=filepath, directory=directory, filename=object_name)

    appended = list(bpy.context.selected_objects)
    print(f"[INFO] Append brought in {len(appended)} object(s):")
    for obj in appended:
        vcount = len(obj.data.vertices) if obj.type == 'MESH' else 'n/a'
        print(f"[INFO]   - {obj.name} (type={obj.type}, verts={vcount})")

    # Don't trust the exact name match -- it may resolve to a camera/empty
    # used for the asset browser's preview thumbnail rather than the mesh
    # itself. Find the actual mesh among whatever got appended.
    meshes = [o for o in appended if o.type == 'MESH']
    if not meshes:
        # Fall back to scanning the whole scene in case selection state
        # didn't carry the mesh (e.g. it came in as an unselected child)
        meshes = [o for o in bpy.data.objects if o.type == 'MESH']
    if not meshes:
        print(f"[ERROR] No mesh object found after appending '{object_name}'. "
              "Check the object name against list_blend_objects.py output "
              "(especially likely if this name was derived from gender and "
              "the guessed female object name doesn't exist in this file).")
        sys.exit(1)

    # If several meshes came in (e.g. eyes as separate objects), take the
    # one with the most vertices -- that's the body, not eyeballs/accessories.
    human = max(meshes, key=lambda o: len(o.data.vertices))
    human.name = "Human"
    human.data.name = "Human"

    # Link up a rig if this donor has one (see _try_append_armature
    # docstring for why this needs a second, explicit append + manual
    # relink rather than happening automatically). Must run BEFORE the
    # modifier-apply loop below, since that loop's ARMATURE skip only
    # matters once there's an actual armature object to preserve a link to.
    armature_obj = _try_append_armature(donor_path, object_name)
    if armature_obj:
        linked_any = False
        for mod in human.modifiers:
            if mod.type == 'ARMATURE' and mod.object is None:
                mod.object = armature_obj
                linked_any = True
                print(f"[INFO] Linked Armature modifier '{mod.name}' to "
                      f"appended '{armature_obj.name}'.")
        if not linked_any:
            print(f"[WARNING] Appended armature '{armature_obj.name}' but "
                  f"'{human.name}' has no ARMATURE-type modifier with an "
                  f"empty object slot to link it to -- the modifier setup "
                  f"on this donor may differ from what was expected.")
        if human.parent is None:
            human.parent = armature_obj
            print(f"[INFO] Parented '{human.name}' to '{armature_obj.name}'.")

    # These sculpting base meshes are often modeled as half a body with a
    # Mirror modifier for symmetry -- apply that. But they may ALSO carry a
    # Multiresolution or Subdivision Surface modifier for sculpt detail --
    # applying those would explode vertex count by 16-64x and give us a
    # dense sculpt mesh instead of the clean low-poly base cage we actually
    # want for a real-time avatar. Remove those without applying; apply
    # everything else (Mirror, etc.) normally.
    #
    # FIX: ARMATURE modifiers must ALSO be skipped here, for the opposite
    # reason -- MULTIRES/SUBSURF get removed because we don't want their
    # effect at all, but an Armature modifier's effect (live skin
    # deformation driven by bone poses) is exactly what a rigged avatar
    # needs to keep working after export. Applying it would permanently
    # bake the current rest-pose deformation into static mesh geometry and
    # sever the live link to the skeleton -- no animation or lip-sync would
    # be possible afterward, even though the export might look fine at
    # rest. This only matters once the donor mesh actually has an Armature
    # modifier (e.g. after a one-time rig + Automatic Weights setup done
    # directly on human_base_meshes_bundle.blend); harmless no-op until then.
    bpy.context.view_layer.objects.active = human
    for mod in list(human.modifiers):
        if mod.type in ('MULTIRES', 'SUBSURF'):
            print(f"[INFO] Removing '{mod.name}' ({mod.type}) without applying "
                  f"-- keeping the low-poly base cage instead of sculpt detail.")
            human.modifiers.remove(mod)
            continue
        if mod.type == 'ARMATURE':
            print(f"[INFO] Leaving '{mod.name}' (ARMATURE) un-applied -- "
                  f"this is what keeps the mesh skinned/riggable after export.")
            continue
        try:
            bpy.ops.object.modifier_apply(modifier=mod.name)
        except RuntimeError as e:
            print(f"[WARNING] Could not apply modifier '{mod.name}': {e}")

    # FIXED ORDERING: shape keys used to be removed right here, which ran
    # BEFORE main() ever called apply_age_weight_morphs() -- so any
    # age/weight shape keys were silently discarded, unused, every single
    # time. Shape key handling has been moved out of this function
    # entirely; see finalize_shape_keys() below, which main() now calls
    # AFTER apply_age_weight_morphs() sets the key values, so the mix
    # actually gets baked in before the shape keys are flattened away. If
    # this mesh has shape keys, they intentionally survive this function
    # returning -- that's correct, not a leftover bug.
    if human.data.shape_keys:
        print(f"[INFO] '{human.name}' has shape keys "
              f"({[k.name for k in human.data.shape_keys.key_blocks]}); "
              f"left intact here so apply_age_weight_morphs() can use them. "
              f"finalize_shape_keys() bakes and removes them later in main().")

    # NOTE: the manual X-recentering loop that used to live here (directly
    # editing v.co.x on every vertex) has also moved -- to
    # recenter_mesh_x(), called from main() AFTER finalize_shape_keys().
    # Editing mesh.vertices coordinates directly while shape keys are still
    # present can desync the Basis shape key's stored data from the actual
    # mesh coordinates (they are separate buffers in Blender, not the same
    # one), which would silently corrupt any live shape-key mix. Doing the
    # recenter only after finalize_shape_keys() has flattened everything to
    # a single plain mesh avoids that.

    # CRITICAL: asset-library .blend files commonly lay multiple objects
    # out side-by-side in their own scene for browser thumbnails (e.g. male
    # body at one X position, female at another). bpy.ops.wm.append keeps
    # that original object transform -- if we don't reset it, the whole
    # body (head included) carries that donor-scene offset into our output.
    # Bake the current transform into the mesh data, then zero it out.
    #
    # FIX: if this donor has a linked armature (human.parent = armature_obj,
    # set above), resetting ONLY the mesh's own transform here would leave
    # the armature's transform untouched -- since world position = parent
    # matrix x local matrix, zeroing just the child's local transform while
    # the parent (armature) keeps a nonzero one shifts the mesh relative to
    # its rig. Selecting both objects for this transform_apply call resets
    # them together, preserving their relative position correctly.
    print(f"[INFO] Object transform before reset: "
          f"location={tuple(round(c, 4) for c in human.location)}, "
          f"rotation={tuple(round(c, 4) for c in human.rotation_euler)}, "
          f"scale={tuple(round(c, 4) for c in human.scale)}")
    bpy.context.view_layer.objects.active = human
    bpy.ops.object.select_all(action='DESELECT')
    human.select_set(True)
    if armature_obj:
        armature_obj.select_set(True)
        print(f"[INFO] Including armature '{armature_obj.name}' in the "
              f"same transform reset so it stays aligned with the mesh.")
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    print(f"[INFO] Object transform after reset: "
          f"location={tuple(human.location)}")

    print(f"[INFO] Using '{human.name}' as body: "
          f"{len(human.data.vertices)} vertices, "
          f"{len(human.data.materials)} material slot(s). "
          f"(X-recentering happens later, in recenter_mesh_x(), after shape "
          f"keys are finalized -- see finalize_shape_keys().)")

    return human


def finalize_shape_keys(human):
    """Bake whatever age/weight shape-key mix apply_age_weight_morphs() just
    set into the base mesh, then remove ONLY those specific shape keys.

    FIX: this used to call shape_key_remove(all=True, apply_mix=True),
    which bakes AND DELETES every shape key on the mesh -- fine as long as
    age/weight were the only shape keys that ever existed, but if the donor
    mesh also carries viseme/ARKit blend shapes (added directly on
    human_base_meshes_bundle.blend for lip-sync), that call would silently
    destroy them too, every single generation. Viseme shape keys must
    survive all the way to export; age/weight keys must not (they're a
    one-time mix baked in at generation time, and the exporter doesn't
    know or care about them). This version distinguishes the two by name
    and only touches the age/weight ones.

    Must run AFTER apply_age_weight_morphs() and BEFORE recenter_mesh_x() /
    deform_head_from_landmarks() / project_face_texture().

    No-op if the mesh has no shape keys, or has none matching the
    age/weight naming convention (see apply_age_weight_morphs()).
    """
    if not human.data.shape_keys or not human.data.shape_keys.key_blocks:
        return

    key_blocks = human.data.shape_keys.key_blocks
    age_weight_names = ("Age", "age", "Age_Young", "age_young", "Age_Old", "age_old",
                         "Weight", "weight", "Weight_Thin", "weight_thin",
                         "Weight_Heavy", "weight_heavy")
    # FIX (--mpfb-live path): MPFB's own macro-detail system encodes
    # gender/age/weight/muscle etc. as shape keys with names like
    # '$md-$as-$fe-$yn' (confirmed from a real generated donor). These
    # need baking into the basis and removing before export too, same as
    # the old Age/Weight names, so they don't clutter the final glTF's
    # morph target list alongside the (intentionally preserved) viseme_*
    # shape keys. Match by the '$md-' prefix rather than exact names,
    # since the encoded suffix varies per combination of settings.
    keys_to_bake = [name for name in age_weight_names if name in key_blocks]
    keys_to_bake += [k.name for k in key_blocks if k.name.startswith("$md-")]

    if not keys_to_bake:
        print("[INFO] finalize_shape_keys: no age/weight or MPFB macro-detail "
              "shape keys present; nothing to bake. Any other shape keys "
              "(e.g. viseme blend shapes) are left untouched.")
        return

    other_keys = [k.name for k in key_blocks
                  if k.name not in keys_to_bake and k.name != "Basis"]
    print(f"[INFO] finalize_shape_keys: baking {keys_to_bake} "
          f"into the basis; preserving {len(other_keys)} other shape key(s) "
          f"untouched: {other_keys}")

    mesh = human.data
    basis = key_blocks["Basis"]
    vert_count = len(mesh.vertices)
    preserved_keys = [k for k in key_blocks if k.name not in keys_to_bake and k.name != "Basis"]

    # Manually accumulate each age/weight key's weighted delta from Basis
    # directly onto the Basis (and mesh.vertices, which mirrors it) -- same
    # effect as shape_key_remove(apply_mix=True) but restricted to just
    # these key blocks.
    #
    # CORRECTNESS: every OTHER shape key (e.g. visemes) stores its own
    # ABSOLUTE vertex coordinates; Blender evaluates its effective shape at
    # runtime as (that key's coords - Basis coords) * value. If we move the
    # Basis without also moving every preserved key's stored coordinates by
    # the same delta, each preserved key's offset FROM the new Basis
    # silently changes -- every viseme would come out distorted by however
    # much the age/weight bake shifted things. Shifting preserved keys by
    # the identical per-vertex delta keeps their relative shape exactly as
    # originally sculpted.
    for i in range(vert_count):
        old_basis_co = basis.data[i].co.copy()
        delta = old_basis_co.copy()
        delta.zero()
        for name in keys_to_bake:
            kb = key_blocks[name]
            delta += (kb.data[i].co - old_basis_co) * kb.value
        new_basis_co = old_basis_co + delta
        basis.data[i].co = new_basis_co
        mesh.vertices[i].co = new_basis_co
        for k in preserved_keys:
            k.data[i].co = k.data[i].co + delta

    # Now remove just the age/weight key blocks (in reverse index order so
    # removing one doesn't shift the indices of the others mid-loop).
    for name in sorted(keys_to_bake, key=lambda n: key_blocks.find(n), reverse=True):
        human.shape_key_remove(key_blocks[name])

    print(f"[INFO] finalize_shape_keys: baked and removed {keys_to_bake}. "
          f"Remaining shape keys: {[k.name for k in human.data.shape_keys.key_blocks] if human.data.shape_keys else []}")


def recenter_mesh_x(human):
    """Defensive recentering: if the donor mesh's own geometry (or object
    transform) isn't perfectly symmetric about x=0, everything downstream
    (head detection, lattice deformation, camera framing) inherits that
    offset -- most visible as a head that looks shifted to one side.
    Recenter based on the actual vertex bounding box, not just the object
    origin, so this catches both causes.

    Must run AFTER finalize_shape_keys() -- editing mesh.vertices directly
    while shape keys are still present can desync the Basis shape key's
    stored data from the mesh's actual coordinates.
    """
    xs = [v.co.x for v in human.data.vertices]
    x_center = (min(xs) + max(xs)) / 2.0
    if abs(x_center) > 0.001:
        print(f"[INFO] Mesh X-center was {x_center:.4f} (should be ~0); recentering.")
        for v in human.data.vertices:
            v.co.x -= x_center
    else:
        print(f"[INFO] Mesh X-center is {x_center:.4f}, already centered.")


# ---------------------------------------------------------------------------
# Age / weight morphs
#
# PLACEHOLDER: I have not seen human_base_meshes_bundle.blend's actual
# age/weight mechanism (shape keys? alternate meshes? bone scale?). This
# tries the most common shape-key naming conventions and logs plainly what
# it did or didn't find, rather than doing nothing silently. Replace this
# once the real mechanism is confirmed.
#
# Ordering (fixed): main() now calls this BEFORE finalize_shape_keys(), so
# if this donor mesh's age/weight morphs really are shape keys, they still
# exist when this function runs and their baked result survives.
# ---------------------------------------------------------------------------
def apply_age_weight_morphs(human, age, weight):
    if not human.data.shape_keys or not human.data.shape_keys.key_blocks:
        print(f"[WARNING] No shape keys found on '{human.name}' -- "
              f"age={age:.2f} and weight={weight:.2f} were requested but "
              f"there is no morph target to apply them to right now. Either "
              f"this donor mesh doesn't use shape keys for age/weight, or "
              f"they were already stripped earlier in append_donor_body() "
              f"(see the ordering warning above apply_age_weight_morphs).")
        return

    key_blocks = human.data.shape_keys.key_blocks
    applied = []

    for candidate in ("Age", "age"):
        if candidate in key_blocks:
            key_blocks[candidate].value = age
            applied.append(candidate)
            break
    else:
        young_key = next((k for k in ("Age_Young", "age_young") if k in key_blocks), None)
        old_key = next((k for k in ("Age_Old", "age_old") if k in key_blocks), None)
        if old_key:
            key_blocks[old_key].value = age
            applied.append(old_key)
        if young_key:
            key_blocks[young_key].value = 1.0 - age
            applied.append(young_key)

    for candidate in ("Weight", "weight"):
        if candidate in key_blocks:
            key_blocks[candidate].value = weight
            applied.append(candidate)
            break
    else:
        thin_key = next((k for k in ("Weight_Thin", "weight_thin") if k in key_blocks), None)
        heavy_key = next((k for k in ("Weight_Heavy", "weight_heavy") if k in key_blocks), None)
        if heavy_key:
            key_blocks[heavy_key].value = weight
            applied.append(heavy_key)
        if thin_key:
            key_blocks[thin_key].value = 1.0 - weight
            applied.append(thin_key)

    if applied:
        print(f"[INFO] Applied age={age:.2f}, weight={weight:.2f} via shape key(s): {applied}")
    else:
        print(f"[WARNING] Shape keys exist on '{human.name}' but none match "
              f"the expected age/weight naming. Available: "
              f"{list(key_blocks.keys())}. Update apply_age_weight_morphs() "
              f"with the correct names.")


# ---------------------------------------------------------------------------
# Deform the head region to approximate the real face's proportions, using
# MediaPipe face landmarks (from extract_face_landmarks.py) and a Lattice
# modifier restricted to the head via a vertex group. This adjusts overall
# face width-to-height aspect and jaw taper -- proportion-matching, not a
# true 3D reconstruction (a single photo has no real depth information).
# ---------------------------------------------------------------------------
# Key MediaPipe FaceLandmarker indices (478-point face mesh)
LM_LEFT_EYE_OUTER = 33
LM_RIGHT_EYE_OUTER = 263
LM_FOREHEAD = 10
LM_CHIN = 152
LM_CHEEK_LEFT = 234
LM_CHEEK_RIGHT = 454
LM_JAW_LEFT = 172
LM_JAW_RIGHT = 397
LM_NOSE_TIP = 4
LM_MOUTH_TOP = 13
LM_MOUTH_BOTTOM = 14
LM_MOUTH_LEFT = 61
LM_MOUTH_RIGHT = 291


def _dist_px(landmarks, i, j, img_w, img_h):
    a, b = landmarks[i], landmarks[j]
    dx = (a["x"] - b["x"]) * img_w
    dy = (a["y"] - b["y"]) * img_h
    return (dx ** 2 + dy ** 2) ** 0.5


def measure_photo_ratios(landmarks_path):
    import json
    with open(landmarks_path) as f:
        data = json.load(f)
    lm = data["landmarks"]
    w, h = data["image_width"], data["image_height"]

    face_width = _dist_px(lm, LM_CHEEK_LEFT, LM_CHEEK_RIGHT, w, h)
    face_height = _dist_px(lm, LM_FOREHEAD, LM_CHIN, w, h)
    jaw_width = _dist_px(lm, LM_JAW_LEFT, LM_JAW_RIGHT, w, h)

    return {
        "width_to_height": face_width / face_height,
        "jaw_to_face_width": jaw_width / face_width,
    }


def measure_photo_face_features(landmarks_path):
    """Measure where the eyes, nose tip, and mouth sit in the photo, purely
    from landmark data -- used to auto-position and auto-scale the face
    projection instead of relying on a manually-tuned camera bias.
    """
    import json
    with open(landmarks_path) as f:
        data = json.load(f)
    lm = data["landmarks"]
    w, h = data["image_width"], data["image_height"]

    def px(i):
        return lm[i]["x"] * w, lm[i]["y"] * h

    _, forehead_y = px(LM_FOREHEAD)
    _, chin_y = px(LM_CHIN)
    face_height_px = abs(chin_y - forehead_y)
    if face_height_px < 1.0:
        face_height_px = 1.0

    def y_frac(i):
        _, y = px(i)
        return (y - forehead_y) / face_height_px

    eye_line_frac = (y_frac(LM_LEFT_EYE_OUTER) + y_frac(LM_RIGHT_EYE_OUTER)) / 2.0
    nose_tip_frac = y_frac(LM_NOSE_TIP)
    mouth_line_frac = (y_frac(LM_MOUTH_TOP) + y_frac(LM_MOUTH_BOTTOM)) / 2.0

    face_width_px = _dist_px(lm, LM_CHEEK_LEFT, LM_CHEEK_RIGHT, w, h)
    if face_width_px < 1.0:
        face_width_px = 1.0

    nose_x_frac = lm[LM_NOSE_TIP]["x"]
    eye_center_x_frac = (lm[LM_LEFT_EYE_OUTER]["x"] + lm[LM_RIGHT_EYE_OUTER]["x"]) / 2.0
    eye_dist_px = _dist_px(lm, LM_LEFT_EYE_OUTER, LM_RIGHT_EYE_OUTER, w, h)

    return {
        "eye_line_frac": eye_line_frac,
        "nose_tip_frac": nose_tip_frac,
        "mouth_line_frac": mouth_line_frac,
        "face_height_px": face_height_px,
        "face_width_px": face_width_px,
        "image_width_px": w,
        "image_height_px": h,
        "face_frac_of_image_h": face_height_px / h if h else 0.5,
        "face_frac_of_image_w": face_width_px / w if w else 0.5,
        "nose_x_frac": nose_x_frac,
        "eye_center_x_frac": eye_center_x_frac,
        "eye_dist_px": eye_dist_px,
    }


def detect_neck_z(mesh, candidate_range=(0.05, 0.30), search_frac=0.6,
                   bins=80, widen_factor=2.5, min_bin_verts=5):
    """Find the actual neck by scanning cross-sectional X-width top-down.

    Correction from an earlier version of this function: a naive top-down
    "first point wider than the running minimum" scan false-triggers
    immediately below the crown, because the very top of a head (skull tip)
    is naturally narrower than the head's widest point (cheek/ear level) --
    so almost anything below the crown looks like "widening" relative to
    it, even though the real neck is still much further down. Verified
    empirically: that naive version reported a "neck" only 1.25% below the
    top of a real donor mesh, which is obviously just crown noise.

    Fix: restrict the search for the width MINIMUM to a plausible
    head-height window (candidate_range, default 5%-30% from the top --
    typical human heads run roughly 12-15% of total height), and only
    accept it as a real neck if the width further down (beyond
    candidate_range) grows to at least widen_factor times that minimum --
    i.e. a substantial, sustained widen into shoulders, not a local blip.
    """
    zs = [v.co.z for v in mesh.vertices]
    xs = [v.co.x for v in mesh.vertices]
    z_min, z_max = min(zs), max(zs)
    total_height = z_max - z_min
    search_bottom = z_max - total_height * search_frac

    bin_edges = [z_max - (z_max - search_bottom) * i / bins for i in range(bins + 1)]
    bin_data = []  # (frac_from_top, bin_bottom_z, width_or_None)
    for i in range(bins):
        hi, lo = bin_edges[i], bin_edges[i + 1]
        xs_in_bin = [x for x, z in zip(xs, zs) if lo <= z < hi]
        frac_from_top = (z_max - lo) / total_height
        if len(xs_in_bin) >= min_bin_verts:
            bin_data.append((frac_from_top, lo, max(xs_in_bin) - min(xs_in_bin)))
        else:
            bin_data.append((frac_from_top, lo, None))

    candidates = [(f, lo, w) for f, lo, w in bin_data
                  if w is not None and candidate_range[0] <= f <= candidate_range[1]]
    if not candidates:
        print(f"[WARNING] detect_neck_z: no bins with >= {min_bin_verts} "
              f"vertices found within candidate_range={candidate_range}; "
              f"falling back to the fixed head_fraction heuristic.")
        return None

    min_frac, min_z, min_width = min(candidates, key=lambda t: t[2])

    tail_widths = [w for f, lo, w in bin_data if w is not None and f > candidate_range[1]]
    if not tail_widths:
        print("[WARNING] detect_neck_z: no data below the candidate range "
              "to confirm a widening transition; falling back to the fixed "
              "head_fraction heuristic.")
        return None

    max_tail_width = max(tail_widths)
    if max_tail_width >= min_width * widen_factor:
        print(f"[INFO] detect_neck_z: minimum cross-section width "
              f"{min_width:.4f} found at {min_frac*100:.1f}% from top "
              f"(Z={min_z:.4f}), widening to {max_tail_width:.4f} "
              f"({max_tail_width/min_width:.1f}x) further down -- "
              f"confirmed as the neck boundary.")
        return min_z

    print(f"[WARNING] detect_neck_z: minimum width {min_width:.4f} at "
          f"{min_frac*100:.1f}% from top never widens by >= {widen_factor}x "
          f"within the searched range (max seen: {max_tail_width:.4f}, "
          f"{max_tail_width/min_width:.1f}x) -- no confident neck found. "
          f"Falling back to the fixed head_fraction heuristic.")
    return None


def select_head_vertices(mesh, head_fraction=0.14, min_verts=40, max_fraction=0.5):
    """Select the mesh's head vertices.

    Primary method: detect_neck_z() finds the actual anatomical neck and
    uses everything above it as "head" -- robust to this mesh's proportions
    regardless of what head_fraction was originally tuned against.

    Fallback (if neck detection fails): the previous fixed-height-fraction
    approach, adaptively grown until it captures at least min_verts
    vertices (helps on sparse/low-poly meshes where a small fraction
    starves the count -- a real but separate issue from the neck-boundary
    one).
    """
    neck_z = detect_neck_z(mesh)
    if neck_z is not None:
        head_verts = [v for v in mesh.vertices if v.co.z >= neck_z]
        z_max = max(v.co.z for v in mesh.vertices)
        if len(head_verts) >= min_verts:
            print(f"[INFO] Head vertex selection via detected neck boundary: "
                  f"{len(head_verts)} vertices at Z>={neck_z:.4f}.")
            return head_verts, neck_z, z_max
        else:
            print(f"[WARNING] Neck-based selection only found "
                  f"{len(head_verts)} vertices (< min_verts={min_verts}); "
                  f"falling back to the fraction-based heuristic instead.")

    zs = [v.co.z for v in mesh.vertices]
    z_min, z_max = min(zs), max(zs)
    total_height = z_max - z_min

    fraction = head_fraction
    head_verts = []
    while True:
        head_threshold = z_max - (total_height * fraction)
        head_verts = [v for v in mesh.vertices if v.co.z >= head_threshold]
        if len(head_verts) >= min_verts or fraction >= max_fraction:
            break
        fraction = min(fraction * 1.5, max_fraction)

    if fraction != head_fraction:
        print(f"[INFO] Head vertex selection: requested head_fraction="
              f"{head_fraction:.3f} only captured a small slice of this mesh; "
              f"grew it to {fraction:.3f} to get {len(head_verts)} vertices "
              f"(min_verts={min_verts}). This usually means the donor mesh "
              f"is sparser (lower-poly) than whatever head_fraction was "
              f"originally tuned against.")

    if len(head_verts) < min_verts:
        print(f"[WARNING] Head vertex selection only found {len(head_verts)} "
              f"vertices even at head_fraction={fraction:.3f} (wanted >= "
              f"{min_verts}). This mesh may be too low-poly for reliable "
              f"face-region measurements/masking -- results downstream "
              f"(face placement, mask shape) may be imprecise.")

    return head_verts, head_threshold, z_max


def measure_mesh_ratios(human, head_fraction=0.14):
    mesh = human.data
    head_verts, _, _ = select_head_vertices(mesh, head_fraction)

    chin_z = min(v.co.z for v in head_verts)
    top_z = max(v.co.z for v in head_verts)
    head_span = top_z - chin_z

    cheek_lo, cheek_hi = chin_z + head_span * 0.4, chin_z + head_span * 0.7
    cheek_verts = [v for v in head_verts if cheek_lo <= v.co.z <= cheek_hi]
    # FIX: the old fallback (a hardcoded 0.1) is an arbitrary world-unit
    # magnitude that has no relationship to this mesh's actual scale --
    # wildly wrong on a mesh sized differently from whatever the constant
    # was eyeballed against. Falling back to a fraction of head_span keeps
    # the fallback at least dimensionally sane relative to THIS mesh.
    if cheek_verts:
        face_width = max(v.co.x for v in cheek_verts) - min(v.co.x for v in cheek_verts)
    else:
        print(f"[WARNING] No vertices found in the cheek Z-band "
              f"({cheek_lo:.4f}..{cheek_hi:.4f}) -- falling back to "
              f"0.6 * head_span for face_width instead of the old fixed "
              f"0.1 constant, which didn't scale with mesh size.")
        face_width = head_span * 0.6

    jaw_lo, jaw_hi = chin_z, chin_z + head_span * 0.25
    jaw_verts = [v for v in head_verts if jaw_lo <= v.co.z <= jaw_hi]
    jaw_width = (max(v.co.x for v in jaw_verts) - min(v.co.x for v in jaw_verts)) if jaw_verts else face_width * 0.8

    return {
        "width_to_height": face_width / head_span,
        "jaw_to_face_width": jaw_width / face_width,
        "chin_z": chin_z,
        "top_z": top_z,
        "head_verts": head_verts,
    }


def deform_head_from_landmarks(human, landmarks_path, head_fraction=0.14):
    if not landmarks_path or not os.path.exists(landmarks_path):
        print(f"[WARNING] Landmarks file not found ({landmarks_path}); skipping head shape deformation.")
        return

    photo = measure_photo_ratios(landmarks_path)
    mesh_ratios = measure_mesh_ratios(human, head_fraction)
    head_verts = mesh_ratios["head_verts"]
    chin_z, top_z = mesh_ratios["chin_z"], mesh_ratios["top_z"]
    head_span = top_z - chin_z

    global_width_scale = photo["width_to_height"] / mesh_ratios["width_to_height"]
    jaw_taper_scale = photo["jaw_to_face_width"] / mesh_ratios["jaw_to_face_width"]
    global_width_scale = max(0.7, min(1.4, global_width_scale))
    jaw_taper_scale = max(0.7, min(1.4, jaw_taper_scale))
    print(f"[INFO] Head shape scale factors -- global width: {global_width_scale:.3f}, "
          f"jaw taper (on top of global): {jaw_taper_scale:.3f}")

    mesh = human.data

    vg = human.vertex_groups.new(name="HeadRegion")
    head_vert_indices = [v.index for v in head_verts]
    vg.add(head_vert_indices, 1.0, 'REPLACE')

    xs = [v.co.x for v in head_verts]
    ys = [v.co.y for v in head_verts]
    pad = 0.03
    lat_min = (min(xs) - pad, min(ys) - pad, chin_z - pad)
    lat_max = (max(xs) + pad, max(ys) + pad, top_z + pad)
    lat_center = tuple((lat_min[i] + lat_max[i]) / 2 for i in range(3))
    lat_size = tuple(lat_max[i] - lat_min[i] for i in range(3))

    lat_data = bpy.data.lattices.new("HeadLattice")
    lat_obj = bpy.data.objects.new("HeadLattice", lat_data)
    bpy.context.collection.objects.link(lat_obj)
    lat_data.points_u, lat_data.points_v, lat_data.points_w = 2, 2, 3
    lat_obj.location = lat_center
    lat_obj.scale = lat_size

    row_scale = {0: global_width_scale * jaw_taper_scale, 1: global_width_scale, 2: global_width_scale}
    for i, pt in enumerate(lat_data.points):
        u = i % 2
        w_row = i // 4
        base_x = -0.5 if u == 0 else 0.5
        pt.co_deform.x = base_x * row_scale.get(w_row, global_width_scale)

    lattice_mod = human.modifiers.new(name="HeadShape", type='LATTICE')
    lattice_mod.object = lat_obj
    lattice_mod.vertex_group = "HeadRegion"

    bpy.context.view_layer.objects.active = human
    bpy.ops.object.modifier_apply(modifier=lattice_mod.name)
    bpy.data.objects.remove(lat_obj, do_unlink=True)

    print("[INFO] Applied head shape deformation from photo landmarks.")


def _srgb_to_linear(c):
    """Convert an array of sRGB-gamma-encoded 0-1 values to linear light
    values (standard sRGB transfer function). Blender's Image.pixels
    returns values as stored in the file -- gamma-encoded for a normal
    photo -- but a shader node's color input (default_value) is
    interpreted as LINEAR. Skipping this conversion is a likely reason a
    sampled color can look washed-out/not-quite-right compared to a
    hand-tuned linear default: the same numeric values mean two visually
    different things in the two spaces.
    """
    import numpy as np
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _sample_skin_tone_from_photo(input_image_path, landmarks_path):
    """Sample a skin-tone RGB from several small patches across reliably-
    skin landmark regions (both cheeks, chin, forehead) -- chosen because
    they sit well away from eyes/eyebrows/mouth/hair -- using the same
    landmarks.json this pipeline already parses elsewhere
    (measure_photo_face_features). Returns a LINEAR (r, g, b) tuple in
    0-1 floats (see _srgb_to_linear), or None if anything about the input
    can't be read -- callers should fall back to the previous flat
    default in that case.
    """
    import json
    import numpy as np
    if not landmarks_path or not os.path.exists(landmarks_path) or not os.path.exists(input_image_path):
        return None

    try:
        with open(landmarks_path) as f:
            data = json.load(f)
        lm = data["landmarks"]
    except Exception as e:
        print(f"[WARNING] Could not read landmarks for skin-tone sampling: {e}")
        return None

    img = None
    try:
        img = bpy.data.images.load(input_image_path)
        w, h = img.size
        # PERF: foreach_get() bulk-copies the pixel buffer; the more
        # familiar img.pixels[:] slicing goes through Blender's Python
        # API element-by-element and is dramatically slower on anything
        # but a tiny image -- this was the actual fix for the added
        # generation time, and is safe on its own.
        #
        # NOTE: a prior version of this function also called img.scale()
        # to downscale before reading, as a further speed optimization.
        # REMOVED -- two real test runs showed it corrupting the pixel
        # data (first as near-total desaturation, then as inverted
        # channel order, confirmed via the printed "Sampled skin tone"
        # log line showing B>G>R, which real skin never produces) even
        # after adding an explicit img.update() sync. Reading at full
        # resolution is somewhat slower but has shown no such corruption;
        # do not reintroduce img.scale() here without solid proof the
        # specific failure mode has actually been found and fixed.
        expected_len = w * h * 4
        actual_len = len(img.pixels)
        if actual_len != expected_len:
            print(f"[WARNING] Image pixel buffer length ({actual_len}) does "
                  f"not match expected {w}x{h}x4={expected_len} -- "
                  f"skipping skin-tone sampling for safety.")
            return None
        flat = np.empty(expected_len, dtype=np.float32)
        img.pixels.foreach_get(flat)
        pixels = flat.reshape(h, w, 4)
    except Exception as e:
        print(f"[WARNING] Could not load photo for skin-tone sampling: {e}")
        return None
    finally:
        if img is not None:
            bpy.data.images.remove(img)

    # Blender's pixel buffer is bottom-up (row 0 = bottom of the image);
    # landmark y fractions here are top-down (0=top), matching how
    # forehead/chin are already used elsewhere in this file (y_frac) --
    # flip when indexing into `pixels`.
    def sample_patch(landmark_idx, radius_frac):
        if landmark_idx >= len(lm):
            return None
        lx, ly = lm[landmark_idx]["x"], lm[landmark_idx]["y"]
        cx = int(lx * w)
        cy = h - 1 - int(ly * h)
        r = max(2, int(radius_frac * min(w, h)))
        x0, x1 = max(0, cx - r), min(w, cx + r + 1)
        y0, y1 = max(0, cy - r), min(h, cy + r + 1)
        patch = pixels[y0:y1, x0:x1, :3]
        return patch.reshape(-1, 3) if patch.size else None

    # Cheeks are the largest, flattest, most reliably-skin regions on the
    # face -- weighted with bigger patches. Forehead is smaller/more
    # cautious since MediaPipe's forehead landmark sits close to the
    # hairline and can catch hair or fringe.
    #
    # REMOVED chin as a sample point: a real test (My_Avatar__32_,
    # gender=male) showed the chin landmark sampling beard hair color
    # instead of skin for a bearded user -- since the flat fallback color
    # covers most of the visible surface (the same ~72-76% unpainted-black
    # gap tracked elsewhere), that contamination dominated the whole
    # head/body, not just a small patch. Cheeks and forehead are
    # essentially never covered by facial hair regardless of gender, which
    # is a safer universal default than chin ever was.
    landmark_points = [
        (LM_CHEEK_LEFT,  0.035),
        (LM_CHEEK_RIGHT, 0.035),
        (LM_FOREHEAD,    0.018),
    ]
    samples = [p for p in (sample_patch(i, r) for i, r in landmark_points) if p is not None]
    if not samples:
        return None

    all_px = np.concatenate(samples, axis=0)

    # Drop near-black (shadow) and near-white (specular highlight) pixels
    # outright -- neither represents the actual skin color, and a small
    # patch is disproportionately affected by even a handful of them.
    brightness = all_px.mean(axis=1)
    keep = (brightness > 0.06) & (brightness < 0.97)
    filtered = all_px[keep] if keep.any() else all_px

    # Robust outlier rejection by color DISTANCE rather than brightness
    # alone -- catches a stray red lip-reflection or bluish shadow pixel
    # even when its brightness happens to fall in a normal range, which
    # brightness-only trimming would miss entirely.
    median_color = np.median(filtered, axis=0)
    dist = np.linalg.norm(filtered - median_color, axis=1)
    threshold = np.percentile(dist, 70)
    close = filtered[dist <= threshold] if threshold > 0 else filtered

    # Median (not mean) as the final estimate -- more robust to whatever
    # outliers survived filtering, especially with a fairly small total
    # sample count.
    final_srgb = np.median(close, axis=0)
    # NOTE: previously converted this to linear on the theory that a
    # shader Base Color input expects linear values -- a real test showed
    # that made results too dark. bake_face_texture() writes skin_tone
    # directly into a baked image's pixel buffer (interpreted in the
    # image's own colorspace, not linear), and the original hardcoded
    # default (0.76, 0.57, 0.47) was apparently tuned empirically against
    # that same un-converted convention -- so returning the raw sampled
    # value, NOT gamma-converting it, is what's actually consistent with
    # the rest of this pipeline. Do not reintroduce _srgb_to_linear() here
    # without re-verifying against a real render first.
    return (float(final_srgb[0]), float(final_srgb[1]), float(final_srgb[2]))


SKIN_TONE_STEP_FACTOR = 0.88  # ~12% brightness change per step, chosen to
                               # give a visible-but-gradual result per step
                               # rather than jumping too far too fast.


def _apply_skin_tone_adjust(skin_tone_rgb, steps):
    """Darken (negative steps) or lighten (positive steps) an (r,g,b)
    tuple by scaling brightness uniformly across channels, preserving
    hue/undertone rather than shifting toward gray or a fixed color. Each
    step multiplies brightness by SKIN_TONE_STEP_FACTOR (darker) or its
    inverse (lighter); result is clamped to a valid 0-1 range per channel.
    """
    if not steps:
        return skin_tone_rgb
    factor = SKIN_TONE_STEP_FACTOR ** (-steps)
    return tuple(min(1.0, max(0.0, c * factor)) for c in skin_tone_rgb[:3])


def project_face_texture(human, input_image_path, landmarks_path=None,
                          head_fraction=0.14, face_bias=0.35,
                          face_scale_margin=0.75, skin_tone_adjust=0,
                          debug_mask=False, output_path=None):
    mesh = human.data

    # CHANGED (explicit request): stop projecting the photo onto the head
    # as a texture entirely. Keep using the photo for skin COLOR (same
    # sampling as before -- forehead + both cheeks, well away from eyes/
    # mouth/hair) and nothing else here -- head SHAPE (deform_head_from_
    # landmarks, controlled separately by --skip-head-warp) is unaffected
    # by this change, per instruction to leave everything else as-is.
    #
    # This short-circuits at the very top, before any of the camera/UV-
    # projection/material-split setup below ever runs, rather than
    # partway through -- that setup creates real Blender objects (a
    # camera, UV layers, a multi-node material graph) that would need
    # careful cleanup if abandoned mid-way; skipping it entirely avoids
    # that risk rather than trying to selectively unwind it.
    sampled_tone = _sample_skin_tone_from_photo(input_image_path, landmarks_path)
    if sampled_tone:
        base_tone = sampled_tone
        print(f"[INFO] Sampled skin tone from photo (forehead + both "
              f"cheeks): RGB={tuple(round(c, 3) for c in sampled_tone)}")
    else:
        base_tone = (0.76, 0.57, 0.47)
        print(f"[INFO] Using default flat skin tone (photo sampling "
              f"unavailable): RGB={base_tone}")

    adjusted_tone = _apply_skin_tone_adjust(base_tone, skin_tone_adjust)
    if skin_tone_adjust:
        print(f"[INFO] Applied skin_tone_adjust={skin_tone_adjust:+d} "
              f"step(s): RGB {tuple(round(c, 3) for c in base_tone)} -> "
              f"{tuple(round(c, 3) for c in adjusted_tone)}")
    skin_tone = (*adjusted_tone, 1.0)

    flat_mat = bpy.data.materials.new(name="SkinMaterial")
    flat_mat.use_nodes = True
    flat_bsdf = flat_mat.node_tree.nodes.get("Principled BSDF")
    if flat_bsdf:
        flat_bsdf.inputs["Base Color"].default_value = skin_tone

    mesh.materials.clear()
    mesh.materials.append(flat_mat)
    for poly in mesh.polygons:
        poly.material_index = 0

    print(f"[INFO] Face/head texture projection skipped (photo used only "
          f"for skin color, not as a texture) -- applied flat skin tone "
          f"RGB={tuple(round(c, 3) for c in adjusted_tone)} to the whole "
          f"mesh, {len(mesh.polygons)} polygons.")

    return flat_mat, skin_tone

    # FIX: previously used a fixed head_fraction with no minimum-vertex-count
    # safeguard (only an empty-set fallback), which starves the head/face
    # selection on lower-poly donor meshes -- a very plausible cause of the
    # face texture landing wrong specifically on a sparser mesh even after
    # the earlier UV-export bug was fixed. Now shared with
    # measure_mesh_ratios() via select_head_vertices(), which adaptively
    # grows the slice until it has enough vertices.
    head_verts, head_threshold, z_max = select_head_vertices(mesh, head_fraction)
    # FIX: cache indices as plain ints RIGHT NOW, not later -- a real test
    # showed 0 in-head polygons detected far below despite head_verts
    # genuinely having 4919 entries, even though head_vert_indices itself
    # reported the correct count. Between here and where that set gets
    # rebuilt (much later, after shape-key baking and other mesh-modifying
    # operations), the MeshVertex object references in head_verts can go
    # stale -- classic Blender scripting trap. Integer indices captured
    # immediately, before anything touches the mesh, stay valid as long as
    # vertex count/order doesn't change (which none of the later steps do).
    head_vert_indices_stable = {v.index for v in head_verts}

    xs = [v.co.x for v in head_verts]
    ys = [v.co.y for v in head_verts]
    head_height = z_max - head_threshold
    chin_z = head_threshold

    feat = None
    if landmarks_path:
        if os.path.exists(landmarks_path):
            feat = measure_photo_face_features(landmarks_path)
            print(f"[INFO] Photo face landmarks: eye_line_frac="
                  f"{feat['eye_line_frac']:.3f}, nose_tip_frac="
                  f"{feat['nose_tip_frac']:.3f}, mouth_line_frac="
                  f"{feat['mouth_line_frac']:.3f} "
                  f"(0=forehead landmark, 1=chin landmark)")
        else:
            print(f"[WARNING] Landmarks file not found ({landmarks_path}); "
                  f"falling back to face_bias={face_bias} heuristic.")

    use_ortho = feat is not None

    # NEW: instead of calibrating from a single eye measurement, measure
    # THIS mesh's own actual eye AND mouth positions (real vertex groups:
    # 'helper-l-eye'/'helper-r-eye', 'lips') and fit a least-squares line
    # through every available real anchor point (eye, mouth, chin) rather
    # than solving exactly from just one pair. More real points average
    # out any single measurement's noise -- this is the multi-landmark
    # alignment the person explicitly asked for.
    #
    # Ears are deliberately NOT included: MediaPipe's face landmark model
    # is a frontal-face model and does not reliably track ear position
    # (ears sit at/outside the edge of the tracked region), so there's no
    # trustworthy photo-side ear landmark to anchor against. Eyes serve as
    # the standard, more reliable substitute for horizontal alignment
    # further below.
    def _measure_vertex_group_avg(obj, mesh_data, group_names):
        """Average (x, y, z) of every vertex weighted into any of the
        named vertex groups on obj, or None if none found/weighted."""
        groups = [vg for vg in obj.vertex_groups if vg.name in group_names]
        if not groups:
            return None
        group_indices = {vg.index for vg in groups}
        xs, ys, zs = [], [], []
        for v in mesh_data.vertices:
            for g in v.groups:
                if g.group in group_indices and g.weight > 0.01:
                    xs.append(v.co.x); ys.append(v.co.y); zs.append(v.co.z)
                    break
        if not xs:
            return None
        n = len(xs)
        return (sum(xs) / n, sum(ys) / n, sum(zs) / n)

    real_eye = None
    real_mouth = None
    calibrated_face_scale_world = None
    if use_ortho:
        real_eye = _measure_vertex_group_avg(human, mesh, ("helper-l-eye", "helper-r-eye"))
        real_mouth = _measure_vertex_group_avg(human, mesh, ("lips",))

        pairs = [(1.0, chin_z)]  # chin landmark (frac=1) anchors at the directly-measured neck-boundary Z
        if real_eye is not None and feat["eye_line_frac"] < 1.0:
            pairs.append((feat["eye_line_frac"], real_eye[2]))
        else:
            print("[WARNING] 'helper-l-eye'/'helper-r-eye' vertex groups not "
                  "found/weighted on this mesh -- eye anchor unavailable for "
                  "vertical calibration.")
        if real_mouth is not None and feat["mouth_line_frac"] < 1.0:
            pairs.append((feat["mouth_line_frac"], real_mouth[2]))
        else:
            print("[WARNING] 'lips' vertex group not found/weighted on this "
                  "mesh -- mouth anchor unavailable for vertical calibration.")

        if len(pairs) >= 2:
            import numpy as np
            fracs = np.array([p[0] for p in pairs])
            zs = np.array([p[1] for p in pairs])
            slope, intercept = np.polyfit(fracs, zs, 1)
            calibrated_face_scale_world = -float(slope)
            heuristic_scale = head_height * face_scale_margin
            print(f"[INFO] Multi-point vertical calibration using "
                  f"{len(pairs)} real anchor(s): fitted mesh_z = "
                  f"{intercept:.4f} + {slope:.4f}*frac (least-squares over "
                  f"{[(round(f, 3), round(z, 4)) for f, z in pairs]}) -> "
                  f"calibrated face-zone scale={calibrated_face_scale_world:.4f} "
                  f"(heuristic head_height x face_scale_margin would have "
                  f"been {heuristic_scale:.4f}).")
        else:
            print("[WARNING] Not enough real mesh anchors to calibrate -- "
                  "falling back to the head_height x face_scale_margin "
                  "heuristic.")

    if use_ortho:
        # FIX: nose_tip_frac is measured as a fraction of the PHOTO's
        # forehead-landmark-to-chin span (the face zone only). The scale
        # calculation below already correctly accounts for this being a
        # SMALLER span than the mesh's full chin-to-crown head_height
        # (which includes scalp/forehead above the photo's forehead
        # landmark) by multiplying head_height by face_scale_margin
        # before using it. This aim calculation never got the same
        # correction -- it applied nose_mesh_frac directly to the full
        # head_height, systematically aiming too high (verified against a
        # real generation: nose_tip_frac=0.487 gave nose_mesh_frac=0.513,
        # ~51% up the FULL chin-to-crown span -- anatomically the nose
        # should sit around 51% up the smaller face-only zone, not the
        # full head including scalp/forehead headroom above it). Scaling
        # head_height by face_scale_margin here first, matching the scale
        # calculation, fixes this.
        mesh_face_height_world_for_aim = (
            calibrated_face_scale_world if calibrated_face_scale_world is not None
            else head_height * face_scale_margin
        )
        nose_mesh_frac = 1.0 - feat["nose_tip_frac"]
        face_center_z = chin_z + mesh_face_height_world_for_aim * nose_mesh_frac
        print(f"[INFO] Auto-detected aim: nose sits at {feat['nose_tip_frac']:.3f} "
              f"of the forehead->chin span in the photo -> mesh Z={face_center_z:.4f} "
              f"(using face-zone height {mesh_face_height_world_for_aim:.4f}, "
              f"{'multi-point calibrated (eye/mouth/chin)' if calibrated_face_scale_world is not None else f'heuristic: head_height {head_height:.4f} x face_scale_margin {face_scale_margin:.2f}'})")

        # DIAGNOSTIC (added to investigate "eyes/texture too high" reports):
        # extrapolate where eye_line_frac and mouth_line_frac land in mesh Z
        # using the SAME proportional face-zone scale as the nose-aim above
        # -- this isolates whether the core aim math itself is already
        # placing features too high, versus the separate mask up_mult/
        # down_mult stretch logic (printed later) being the actual source
        # of the distortion. head_top and chin_z must already be in scope
        # here (used to compute head_height above).
        eye_mesh_z = chin_z + mesh_face_height_world_for_aim * (1.0 - feat["eye_line_frac"])
        mouth_mesh_z = chin_z + mesh_face_height_world_for_aim * (1.0 - feat["mouth_line_frac"])
        eye_frac_of_head = (eye_mesh_z - chin_z) / head_height if head_height else float('nan')
        mouth_frac_of_head = (mouth_mesh_z - chin_z) / head_height if head_height else float('nan')
        print(f"[DIAGNOSTIC] Eye-line extrapolates to mesh Z={eye_mesh_z:.4f} "
              f"({eye_frac_of_head:.1%} up from chin to crown -- anatomically "
              f"expect roughly ~50%). Mouth-line extrapolates to mesh "
              f"Z={mouth_mesh_z:.4f} ({mouth_frac_of_head:.1%} up). If these "
              f"look anatomically reasonable but the final render still shows "
              f"eyes too high, the bug is in the mask up_mult/down_mult "
              f"stretch logic below, not this aim/scale math.")
    else:
        face_center_z = chin_z + head_height * face_bias
        print(f"[INFO] No landmarks -- using face_bias={face_bias:.2f} "
              f"heuristic aim -> mesh Z={face_center_z:.4f}")

    head_center = (
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        face_center_z,
    )
    print(f"[INFO] Head vertical span: chin/threshold={chin_z:.4f}, "
          f"top={z_max:.4f}, span={head_height:.4f}")

    cam_distance = max(head_height * 3.0, 0.35)
    bpy.ops.object.camera_add(
        location=(head_center[0], head_center[1] - cam_distance, head_center[2])
    )
    cam = bpy.context.active_object
    cam.rotation_euler = (1.57, 0, 0)

    if os.path.exists(input_image_path):
        img_probe = bpy.data.images.load(input_image_path)
        img_w, img_h = img_probe.size
        bpy.data.images.remove(img_probe)
    else:
        img_w, img_h = None, None

    if use_ortho:
        if img_w and img_h:
            bpy.context.scene.render.resolution_x = img_w
            bpy.context.scene.render.resolution_y = img_h

        mesh_face_height_world = (
            calibrated_face_scale_world if calibrated_face_scale_world is not None
            else head_height * face_scale_margin
        )
        face_frac_of_image = max(feat["face_frac_of_image_h"], 0.05)
        frame_height_world = mesh_face_height_world / face_frac_of_image

        cam.data.type = 'ORTHO'
        cam.data.sensor_fit = 'VERTICAL'
        cam.data.ortho_scale = frame_height_world
        print(f"[INFO] Orthographic camera scale = {frame_height_world:.4f} "
              f"world units (face-zone height {mesh_face_height_world:.4f} "
              f"[{'multi-point calibrated' if calibrated_face_scale_world is not None else 'heuristic'}], "
              f"divided by measured face_frac_of_image={face_frac_of_image:.3f})")

        aspect = (img_w / img_h) if (img_w and img_h) else 1.0

        # NEW: calibrate horizontal scale from the mesh's real inter-eye
        # distance instead of just deriving frame width from the vertical
        # scale x aspect ratio (which silently assumes the photo and mesh
        # share identical proportions -- not guaranteed). Also center on
        # the eye MIDPOINT rather than the nose tip -- eyes are a more
        # reliable symmetric anchor, since nose position in a photo can
        # shift with head yaw/rotation even when the face is otherwise
        # centered.
        left_eye = _measure_vertex_group_avg(human, mesh, ("helper-l-eye",))
        right_eye = _measure_vertex_group_avg(human, mesh, ("helper-r-eye",))
        frame_width_world = frame_height_world * aspect  # fallback default
        target_mesh_x = 0.0
        horizontal_calibrated = False
        if (left_eye is not None and right_eye is not None
                and feat.get("eye_dist_px", 0) > 1.0 and feat.get("image_width_px", 0) > 0):
            real_eye_dist_world = abs(right_eye[0] - left_eye[0])
            photo_eye_dist_frac_of_width = feat["eye_dist_px"] / feat["image_width_px"]
            if photo_eye_dist_frac_of_width > 0.001 and real_eye_dist_world > 0.0001:
                frame_width_world = real_eye_dist_world / photo_eye_dist_frac_of_width
                target_mesh_x = (left_eye[0] + right_eye[0]) / 2.0
                horizontal_calibrated = True
                print(f"[INFO] Horizontal calibration from real inter-eye "
                      f"distance: mesh eye separation={real_eye_dist_world:.4f} "
                      f"world units, photo eye separation="
                      f"{photo_eye_dist_frac_of_width:.3f} of image width -> "
                      f"calibrated frame_width_world={frame_width_world:.4f} "
                      f"(vs aspect-derived {frame_height_world * aspect:.4f}), "
                      f"target mesh eye-center X={target_mesh_x:.4f}.")
        if not horizontal_calibrated:
            print("[WARNING] Could not calibrate horizontal scale from real "
                  "eye distance (missing eye vertex groups or degenerate "
                  "measurement) -- falling back to aspect-ratio-derived "
                  "width and assuming mesh eye-center X=0.")

        eye_center_x_frac = feat.get("eye_center_x_frac", feat["nose_x_frac"])
        uncorrected_mesh_x = (eye_center_x_frac - 0.5) * frame_width_world
        world_x_offset = uncorrected_mesh_x - target_mesh_x
        cam.location.x -= world_x_offset
        print(f"[INFO] Horizontal aim correction: photo eye-midpoint sits "
              f"at x={eye_center_x_frac:.3f} of image width (0.5=center), "
              f"target mesh X={target_mesh_x:.4f} "
              f"({'real measured eye-center' if horizontal_calibrated else 'assumed 0'}) "
              f"-> camera shifted by {-world_x_offset:.4f} world units.")
    else:
        if img_w and img_h:
            pass

    mesh.uv_layers.new(name="FaceProjection")
    uv_mod = human.modifiers.new(name="FaceProjector", type='UV_PROJECT')
    uv_mod.uv_layer = "FaceProjection"

    face_mat = bpy.data.materials.new(name="FaceMaterial")
    face_mat.use_nodes = True
    nodes = face_mat.node_tree.nodes
    nodes.clear()

    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    output = nodes.new(type='ShaderNodeOutputMaterial')
    tex_node = nodes.new(type='ShaderNodeTexImage')
    uv_map_node = nodes.new(type='ShaderNodeUVMap')
    uv_map_node.uv_map = "FaceProjection"

    if os.path.exists(input_image_path):
        img = bpy.data.images.load(input_image_path)
        tex_node.image = img
        w, h = img.size
        uv_mod.aspect_x = 1.0
        if not use_ortho:
            uv_mod.aspect_y = (h / w) if w else 1.0
    else:
        print(f"[WARNING] Input image not found at {input_image_path}; "
              f"face material will have no texture.")

    sampled_tone = _sample_skin_tone_from_photo(input_image_path, landmarks_path)
    if sampled_tone:
        base_tone = sampled_tone
        print(f"[INFO] Sampled skin tone from photo (forehead + both "
              f"cheeks): RGB={tuple(round(c, 3) for c in sampled_tone)}")
    else:
        base_tone = (0.76, 0.57, 0.47)
        print(f"[INFO] Using default flat skin tone (photo sampling "
              f"unavailable): RGB={base_tone}")

    adjusted_tone = _apply_skin_tone_adjust(base_tone, skin_tone_adjust)
    if skin_tone_adjust:
        print(f"[INFO] Applied skin_tone_adjust={skin_tone_adjust:+d} "
              f"step(s): RGB {tuple(round(c, 3) for c in base_tone)} -> "
              f"{tuple(round(c, 3) for c in adjusted_tone)}")
    skin_tone = (*adjusted_tone, 1.0)
    ramp_mid = 0.465

    down_mult = 1.0
    up_mult = 1.0
    stretch_x = 1.0

    separate_uv = nodes.new(type='ShaderNodeSeparateXYZ')
    combine_centered = nodes.new(type='ShaderNodeCombineXYZ')
    node_sub_x = nodes.new(type='ShaderNodeMath')
    node_sub_x.operation = 'SUBTRACT'
    node_sub_x.inputs[1].default_value = 0.5
    node_stretch_x = nodes.new(type='ShaderNodeMath')
    node_stretch_x.operation = 'MULTIPLY'
    node_stretch_x.inputs[1].default_value = stretch_x
    node_sub_y = nodes.new(type='ShaderNodeMath')
    node_sub_y.operation = 'SUBTRACT'
    node_sub_y.inputs[1].default_value = 0.5
    node_gt = nodes.new(type='ShaderNodeMath')
    node_gt.operation = 'GREATER_THAN'
    node_gt.inputs[1].default_value = 0.0
    node_y_down = nodes.new(type='ShaderNodeMath')
    node_y_down.operation = 'MULTIPLY'
    node_y_down.inputs[1].default_value = down_mult
    node_y_up = nodes.new(type='ShaderNodeMath')
    node_y_up.operation = 'MULTIPLY'
    node_y_up.inputs[1].default_value = up_mult
    node_mix_y = nodes.new(type='ShaderNodeMix')
    node_mix_y.data_type = 'FLOAT'
    vec_length = nodes.new(type='ShaderNodeVectorMath')
    vec_length.operation = 'LENGTH'
    mask_ramp = nodes.new(type='ShaderNodeValToRGB')
    mask_ramp.color_ramp.elements[0].position = 0.38
    mask_ramp.color_ramp.elements[0].color = (1, 1, 1, 1)
    mask_ramp.color_ramp.elements[1].position = 0.55
    mask_ramp.color_ramp.elements[1].color = (0, 0, 0, 1)
    mask_ramp.color_ramp.interpolation = 'EASE'
    skin_color_node = nodes.new(type='ShaderNodeRGB')
    skin_color_node.outputs[0].default_value = skin_tone
    mix_color = nodes.new(type='ShaderNodeMix')
    mix_color.data_type = 'RGBA'

    links = face_mat.node_tree.links
    links.new(uv_map_node.outputs['UV'], tex_node.inputs['Vector'])
    links.new(uv_map_node.outputs['UV'], separate_uv.inputs['Vector'])
    links.new(separate_uv.outputs['X'], node_sub_x.inputs[0])
    links.new(separate_uv.outputs['Y'], node_sub_y.inputs[0])
    links.new(node_sub_x.outputs['Value'], node_stretch_x.inputs[0])
    links.new(node_stretch_x.outputs['Value'], combine_centered.inputs['X'])
    links.new(node_sub_y.outputs['Value'], node_gt.inputs[0])
    links.new(node_sub_y.outputs['Value'], node_y_down.inputs[0])
    links.new(node_sub_y.outputs['Value'], node_y_up.inputs[0])
    links.new(node_gt.outputs['Value'], node_mix_y.inputs['Factor'])
    links.new(node_y_down.outputs['Value'], node_mix_y.inputs['A'])
    links.new(node_y_up.outputs['Value'], node_mix_y.inputs['B'])
    links.new(node_mix_y.outputs['Result'], combine_centered.inputs['Y'])
    links.new(combine_centered.outputs['Vector'], vec_length.inputs[0])
    links.new(vec_length.outputs['Value'], mask_ramp.inputs['Fac'])
    links.new(mask_ramp.outputs['Color'], mix_color.inputs['Factor'])
    links.new(skin_color_node.outputs[0], mix_color.inputs['A'])
    links.new(tex_node.outputs['Color'], mix_color.inputs['B'])
    links.new(mix_color.outputs['Result'], bsdf.inputs['Base Color'])
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    skin_mat = bpy.data.materials.new(name="SkinMaterial")
    skin_mat.use_nodes = True
    skin_bsdf = skin_mat.node_tree.nodes.get("Principled BSDF")
    if skin_bsdf:
        skin_bsdf.inputs["Base Color"].default_value = skin_tone

    mesh.materials.clear()
    mesh.materials.append(skin_mat)
    mesh.materials.append(face_mat)

    head_vert_indices = head_vert_indices_stable
    cam_loc = cam.location
    print(f"[DIAGNOSTIC] Material-split camera position: cam_loc={tuple(cam_loc)}, "
          f"head_vert_indices count={len(head_vert_indices)}")
    in_head_count = 0
    facing_count = 0
    sample_printed = 0
    # WIDENED (requested: texture should reach the ears on both sides):
    # previously required poly.normal.dot(to_cam) > 0, a strict 90-degree
    # cutoff -- only polygons facing (roughly) straight at the camera
    # qualified, which excludes side-facing geometry like ears/temples
    # entirely (their normals point sideways, not toward an
    # approximately-centered camera). Using a normalized dot product
    # against a threshold below zero relaxes this to ~110 degrees from
    # dead-on, pulling in that side geometry. This doesn't give ears real
    # photo detail (a front photo genuinely doesn't show them), but it
    # does widen the region that gets the face material/projected texture
    # rather than falling back to flat skin right at the cheekbone.
    # REVERTED (was -0.15, briefly -0.35): confirmed via a real photo with
    # a colorful background right behind the head that ANY widening past
    # the original strict cutoff risks projecting background content onto
    # the model, not just distorted skin. Simple planar/orthographic
    # projection has no depth information, so it cannot tell "this pixel
    # is the person's jaw" from "this pixel is the wall behind them" once
    # the included polygons reach far enough toward the silhouette edge --
    # this is a real limitation of the projection method itself, not a
    # tuning problem, so pushing this value further negative again is
    # very likely to reproduce the same class of bug on some photos even
    # if it looks fine on others. Reaching all the way to the ears would
    # need actual photo background segmentation (a much larger feature),
    # not a threshold tweak. 0.0 = the original strict "must face the
    # camera at least partially" cutoff, with no deliberate widening.
    FACE_ANGLE_THRESHOLD = 0.0
    for poly in mesh.polygons:
        in_head = all(vi in head_vert_indices for vi in poly.vertices)
        if in_head:
            in_head_count += 1
            to_cam = cam_loc - poly.center
            to_cam_dir = to_cam.normalized() if to_cam.length > 0 else to_cam
            facing_camera = poly.normal.dot(to_cam_dir) > FACE_ANGLE_THRESHOLD
            if facing_camera:
                facing_count += 1
            elif sample_printed < 5:
                print(f"[DIAGNOSTIC] Sample in-head, NOT-facing polygon: "
                      f"center={tuple(poly.center)}, normal={tuple(poly.normal)}, "
                      f"to_cam={tuple(to_cam)}, dot={poly.normal.dot(to_cam_dir):.4f}")
                sample_printed += 1
        else:
            facing_camera = False
        poly.material_index = 1 if (in_head and facing_camera) else 0
    print(f"[DIAGNOSTIC] Material split result: {in_head_count} in-head "
          f"polygons, {facing_count} of those facing camera "
          f"(material_index=1).")

    if len(uv_mod.projectors) > 0:
        uv_mod.projectors[0].object = cam

    bpy.context.view_layer.objects.active = human
    bpy.ops.object.modifier_apply(modifier=uv_mod.name)
    bpy.data.objects.remove(cam, do_unlink=True)

    face_uv_layer = mesh.uv_layers["FaceProjection"]
    us, vs = [], []
    for poly in mesh.polygons:
        if poly.material_index == 1:
            for li in poly.loop_indices:
                uv = face_uv_layer.data[li].uv
                us.append(uv.x)
                vs.append(uv.y)

    if us:
        u_min, u_max = min(us), max(us)
        v_min, v_max = min(vs), max(vs)
        print(f"[DIAGNOSTIC] FaceProjection UV range on face polygons: "
              f"U=[{u_min:.3f}, {u_max:.3f}], V=[{v_min:.3f}, {v_max:.3f}] "
              f"(mask expects roughly centered near U=0.5, V=0.5, radius <0.48)")

        down_extent = max(v_max - 0.5, 0.01)
        up_extent = max(0.5 - v_min, 0.01)
        x_extent = max(max(u_max - 0.5, 0.5 - u_min), 0.01)

        margin = 0.70
        down_mult = max(0.3, min(6.0, ramp_mid / (down_extent * margin)))
        up_mult = max(0.3, min(6.0, ramp_mid / (up_extent * margin)))
        stretch_x = max(0.3, min(6.0, ramp_mid / (x_extent * margin)))

        node_y_down.inputs[1].default_value = down_mult
        node_y_up.inputs[1].default_value = up_mult
        node_stretch_x.inputs[1].default_value = stretch_x

        print(f"[INFO] Footprint-based mask sizing -- head UV footprint: "
              f"up={up_extent:.3f}, down={down_extent:.3f}, x={x_extent:.3f} "
              f"-> down_mult={down_mult:.2f}, up_mult={up_mult:.2f}, "
              f"stretch_x={stretch_x:.2f} (fade completes at {margin:.0%} "
              f"of the real footprint, leaving a buffer before the hard "
              f"head/body material edge)")
    else:
        print("[DIAGNOSTIC] No face polygons found with material_index==1 -- "
              "the head/face split itself may be empty. Leaving mask at "
              "placeholder sizing (down_mult=1.0, up_mult=1.0, stretch_x=1.0).")

    if debug_mask:
        _debug_bake_mask(human, face_mat, mask_ramp, output_path)
        _debug_render_uv_orientation(human, head_center, head_height, output_path)

    return face_mat, skin_tone


def _debug_render_uv_orientation(human, head_center, head_height, output_path):
    print("[INFO] --debug-mask: rendering a UV-orientation diagnostic "
          "(R=U, G=V) to identify the true left/right and up/down axes...")
    mesh = human.data
    if "FaceProjection" not in mesh.uv_layers:
        print("[WARNING] No FaceProjection UV layer found; skipping "
              "orientation render.")
        return

    original_materials = list(mesh.materials)
    original_mat_indices = [poly.material_index for poly in mesh.polygons]

    debug_mat = bpy.data.materials.new(name="__debug_uv_orientation")
    debug_mat.use_nodes = True
    nt = debug_mat.node_tree
    nt.nodes.clear()
    uv_map = nt.nodes.new(type='ShaderNodeUVMap')
    uv_map.uv_map = "FaceProjection"
    sep = nt.nodes.new(type='ShaderNodeSeparateXYZ')
    combine = nt.nodes.new(type='ShaderNodeCombineXYZ')
    combine.inputs['Z'].default_value = 0.15
    emission = nt.nodes.new(type='ShaderNodeEmission')
    out_node = nt.nodes.new(type='ShaderNodeOutputMaterial')
    nt.links.new(uv_map.outputs['UV'], sep.inputs['Vector'])
    nt.links.new(sep.outputs['X'], combine.inputs['X'])
    nt.links.new(sep.outputs['Y'], combine.inputs['Y'])
    nt.links.new(combine.outputs['Vector'], emission.inputs['Color'])
    nt.links.new(emission.outputs['Emission'], out_node.inputs['Surface'])

    mesh.materials.clear()
    mesh.materials.append(debug_mat)
    for poly in mesh.polygons:
        poly.material_index = 0

    cam_distance = max(head_height * 3.0, 0.35)
    bpy.ops.object.camera_add(
        location=(head_center[0], head_center[1] - cam_distance, head_center[2])
    )
    dbg_cam = bpy.context.active_object
    dbg_cam.rotation_euler = (1.57, 0, 0)
    dbg_cam.data.type = 'PERSP'

    scene = bpy.context.scene
    orig_camera = scene.camera
    orig_res_x, orig_res_y = scene.render.resolution_x, scene.render.resolution_y
    orig_engine = scene.render.engine
    orig_filepath = scene.render.filepath

    scene.camera = dbg_cam
    scene.render.resolution_x = 512
    scene.render.resolution_y = 512
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 8

    dbg_path = (os.path.splitext(output_path)[0] + "_uv_orientation_debug.png"
                if output_path else "/tmp/uv_orientation_debug.png")
    scene.render.filepath = dbg_path
    scene.render.image_settings.file_format = 'PNG'

    try:
        bpy.ops.render.render(write_still=True)
        print(f"[INFO] UV orientation diagnostic saved to: {dbg_path}")
    except RuntimeError as e:
        print(f"[WARNING] UV orientation render failed: {e}")
    finally:
        scene.camera = orig_camera
        scene.render.resolution_x, scene.render.resolution_y = orig_res_x, orig_res_y
        scene.render.engine = orig_engine
        scene.render.filepath = orig_filepath
        bpy.data.objects.remove(dbg_cam, do_unlink=True)
        mesh.materials.clear()
        for m in original_materials:
            mesh.materials.append(m)
        for poly, idx in zip(mesh.polygons, original_mat_indices):
            poly.material_index = idx
        bpy.data.materials.remove(debug_mat)


def _debug_bake_mask(human, face_mat, mask_ramp, output_path):
    print("[INFO] --debug-mask: baking a quick grayscale mask preview...")
    mesh = human.data
    nt = face_mat.node_tree

    debug_uv_name = "__debug_mask_uv"
    if debug_uv_name not in mesh.uv_layers:
        mesh.uv_layers.new(name=debug_uv_name)
    mesh.uv_layers.active = mesh.uv_layers[debug_uv_name]
    bpy.context.view_layer.objects.active = human
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.smart_project(angle_limit=66, island_margin=0.02)
    bpy.ops.object.mode_set(mode='OBJECT')

    emit = nt.nodes.new(type='ShaderNodeEmission')
    out_node = next((n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL'), None)
    old_surface_link = None
    if out_node and out_node.inputs['Surface'].links:
        old_surface_link = out_node.inputs['Surface'].links[0].from_socket
    nt.links.new(mask_ramp.outputs['Color'], emit.inputs['Color'])
    if out_node:
        nt.links.new(emit.outputs['Emission'], out_node.inputs['Surface'])

    debug_img = bpy.data.images.new("MaskDebug", width=512, height=512, alpha=False)
    bake_node = nt.nodes.new(type='ShaderNodeTexImage')
    bake_node.image = debug_img
    for n in nt.nodes:
        n.select = False
    bake_node.select = True
    nt.nodes.active = bake_node

    scene = bpy.context.scene
    original_engine = scene.render.engine
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 4
    bpy.ops.object.select_all(action='DESELECT')
    human.select_set(True)
    bpy.context.view_layer.objects.active = human
    try:
        bpy.ops.object.bake(type='EMIT')
        debug_path = (os.path.splitext(output_path)[0] + "_mask_debug.png"
                      if output_path else "/tmp/mask_debug.png")
        debug_img.filepath_raw = debug_path
        debug_img.file_format = 'PNG'
        debug_img.save()
        print(f"[INFO] Mask preview saved to: {debug_path} "
              f"(white = photo will show, black = flat skin)")
    except RuntimeError as e:
        print(f"[WARNING] Mask debug bake failed: {e}")
    finally:
        scene.render.engine = original_engine
        if out_node:
            if old_surface_link:
                nt.links.new(old_surface_link, out_node.inputs['Surface'])
            else:
                for l in list(out_node.inputs['Surface'].links):
                    nt.links.remove(l)
        nt.nodes.remove(emit)
        nt.nodes.remove(bake_node)
        bpy.data.images.remove(debug_img)
        if debug_uv_name in mesh.uv_layers:
            mesh.uv_layers.remove(mesh.uv_layers[debug_uv_name])


def bake_face_texture(human, face_mat, skin_tone, output_png_path,
                       image_size=2048, mirror_fill=False):
    mesh = human.data

    print("[INFO] Generating a fresh non-overlapping UV unwrap for baking "
          "(ignoring any existing UV layout, which may overlap body parts).")
    # FIX: previously used a hardcoded "BakeUV" string for every downstream
    # lookup (mesh.uv_layers[bake_uv_name]). If the mesh ALREADY has a UV
    # layer named exactly "BakeUV" (plausible on MPFB's mesh, which is far
    # more complex than the old donor and likely carries its own
    # pre-existing UV layers for its own texturing system), every one of
    # those lookups would silently return that OTHER, PRE-EXISTING layer
    # instead of the one just created here -- no crash, no error, just a
    # UV layout that doesn't match what got baked. Using new_uv.name (the
    # actual name Blender assigned, auto-uniquified if there was a
    # collision) instead of a hardcoded string everywhere below makes this
    # class of bug structurally impossible regardless of what UV layers
    # the mesh already had.
    new_uv = mesh.uv_layers.new(name="BakeUV")
    bake_uv_name = new_uv.name
    if bake_uv_name != "BakeUV":
        print(f"[WARNING] Requested UV layer name 'BakeUV' collided with an "
              f"existing layer on this mesh; Blender assigned '{bake_uv_name}' "
              f"instead. Using the real name throughout -- this warning "
              f"existing at all confirms the collision-proofing below was "
              f"necessary, not just defensive.")
    new_uv.active_render = True
    mesh.uv_layers.active = new_uv  # smart_project acts on the ACTIVE layer

    # Single global unwrap -- identical to the ORIGINAL script's proven,
    # working approach (unwraps the whole mesh at once, non-overlapping).
    bpy.context.view_layer.objects.active = human
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.uv.smart_project(angle_limit=66, island_margin=0.02)
    bpy.ops.object.mode_set(mode='OBJECT')

    # FIX: smart_project sizes each UV island roughly proportional to its
    # real 3D surface area / triangle density. On this donor mesh the head
    # region is ~33% of the whole mesh's triangles (confirmed: 7002/21160),
    # so the single global unwrap above gives the head ~87-97% of the
    # 2048x2048 texture atlas -- leaving the actual small face oval (where
    # the photo lands) as a tiny sliver inside a huge, mostly-flat-skin
    # island.
    #
    # CORRECTED APPROACH: an earlier version of this fix tried to unwrap
    # face-material and skin-material polygons in two SEPARATE smart_project
    # passes, using bpy poly.select + edit-mode toggling to select each
    # group. Verified against real output that this did not change the
    # exported result at all -- the operator-selection-based approach is a
    # known-fragile Blender scripting pattern that can silently fail to
    # affect the intended geometry with no Python exception raised.
    #
    # This version removes that dependency entirely: it keeps the ONE
    # proven global smart_project call above (same as the original working
    # script), then does a plain deterministic coordinate rescale in pure
    # Python -- reading each material group's existing loop UVs and
    # remapping them into a fixed target box. No operator calls, no
    # selection state, nothing that can silently no-op. Adjust HEAD_UV_BOX
    # below if the face still looks cramped/oversized relative to the body.
    HEAD_UV_BOX = (0.0, 0.0, 0.45, 1.0)   # (u0, v0, u1, v1) -- 45% of atlas width
    BODY_UV_BOX = (0.45, 0.0, 1.0, 1.0)   # remaining 55%

    uv_layer = mesh.uv_layers[bake_uv_name]

    def _rescale_material_group_uvs(material_index, target_box, label,
                                     source_uv_layer=None):
        # NEW: source_uv_layer lets this read coordinates from a DIFFERENT,
        # already-coherent UV layer (FaceProjection -- a simple camera
        # projection, not an organic-mesh unwrap, so it has no seams/
        # fragmentation at all for front-facing geometry) instead of the
        # smart_project-generated one on `uv_layer`. Writes always go to
        # `uv_layer` (BakeUV) regardless of where they're read from --
        # this replaces smart_project's fragmented face layout wholesale
        # rather than just repositioning it, which is what was leaving
        # "76-80% unpainted" (empty inter-island padding, not failed
        # painting) and isolated-looking features like the lips.
        read_layer = source_uv_layer if source_uv_layer is not None else uv_layer
        us, vs, loop_refs = [], [], []
        for poly in mesh.polygons:
            if poly.material_index != material_index:
                continue
            for li in poly.loop_indices:
                uv = read_layer.data[li].uv
                us.append(uv.x)
                vs.append(uv.y)
                loop_refs.append(li)

        if not us:
            print(f"[WARNING] No polygons found with material_index="
                  f"{material_index} ({label}); nothing to rescale.")
            return

        u_min, u_max = min(us), max(us)
        v_min, v_max = min(vs), max(vs)
        orig_w = max(u_max - u_min, 1e-6)
        orig_h = max(v_max - v_min, 1e-6)

        box_u0, box_v0, box_u1, box_v1 = target_box
        pack_margin = 0.03
        box_w = (box_u1 - box_u0) - 2 * pack_margin
        box_h = (box_v1 - box_v0) - 2 * pack_margin

        scale = min(box_w / orig_w, box_h / orig_h)
        new_w, new_h = orig_w * scale, orig_h * scale
        offset_u = box_u0 + pack_margin + (box_w - new_w) / 2.0
        offset_v = box_v0 + pack_margin + (box_h - new_h) / 2.0

        for li in loop_refs:
            src_uv = read_layer.data[li].uv
            dst_uv = uv_layer.data[li].uv
            dst_uv.x = offset_u + (src_uv.x - u_min) * scale
            dst_uv.y = offset_v + (src_uv.y - v_min) * scale

        print(f"[INFO] Rescaled '{label}' (material_index={material_index}, "
              f"{len(loop_refs)} loops) from footprint "
              f"U=[{u_min:.3f},{u_max:.3f}] V=[{v_min:.3f},{v_max:.3f}] "
              f"({orig_w:.3f}x{orig_h:.3f}) into box {target_box} "
              f"(scale={scale:.3f}). This print line is the proof this code "
              f"ran -- if it's missing from the console log, this file did "
              f"not execute for that run.")

    _rescale_material_group_uvs(1, HEAD_UV_BOX, "face/head",
                                 source_uv_layer=mesh.uv_layers["FaceProjection"])
    _rescale_material_group_uvs(0, BODY_UV_BOX, "skin/body")

    mesh.uv_layers.active = mesh.uv_layers[bake_uv_name]

    bake_image = bpy.data.images.new(
        name="HeadBakedTexture", width=image_size, height=image_size, alpha=False
    )
    import numpy as np
    fill = np.tile(np.array([*skin_tone[:3], 1.0], dtype=np.float32),
                    image_size * image_size)
    bake_image.pixels.foreach_set(fill)

    bake_node = face_mat.node_tree.nodes.new(type='ShaderNodeTexImage')
    bake_node.image = bake_image
    for node in face_mat.node_tree.nodes:
        node.select = False
    bake_node.select = True
    face_mat.node_tree.nodes.active = bake_node

    skin_mat = mesh.materials[0]
    skin_bake_node = skin_mat.node_tree.nodes.new(type='ShaderNodeTexImage')
    skin_bake_node.image = bake_image
    for node in skin_mat.node_tree.nodes:
        node.select = False
    skin_bake_node.select = True
    skin_mat.node_tree.nodes.active = skin_bake_node

    scene = bpy.context.scene
    original_engine = scene.render.engine
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = 4
    scene.render.bake.use_pass_direct = False
    scene.render.bake.use_pass_indirect = False
    scene.render.bake.use_pass_color = True
    scene.render.bake.margin = 32
    scene.render.bake.margin_type = 'EXTEND'

    bpy.context.view_layer.objects.active = human
    bpy.ops.object.select_all(action='DESELECT')
    human.select_set(True)

    print("[INFO] Baking diffuse color to image (this may take a moment)...")
    bpy.ops.object.bake(type='DIFFUSE')
    scene.render.engine = original_engine

    # FIX: direct pixel inspection on real --mpfb-live output showed ~88%
    # of the bake image remaining PURE BLACK instead of the intended flat
    # skin_tone fill, rendering as what looked like dark clothing. The
    # pre-bake fill (bake_image.pixels.foreach_set(fill) above) and the
    # bake margin/extend setting (32px) were both meant to prevent this,
    # but MPFB's mesh is far more topologically complex than the original
    # donor (individual fingers, toes, ears all get their own small UV
    # islands via smart_project), producing many more, smaller islands
    # with larger gaps between them than a 32px margin can close -- so
    # large swaths of the image never get touched by either the prefill
    # or the bake's margin-extend, and stay at Blender's default black.
    #
    # This is a direct, guaranteed fix for the VISIBLE symptom regardless
    # of exactly why the gaps exist: explicitly replace any remaining
    # near-black pixel with the intended flat skin tone after baking.
    import numpy as np
    w, h = bake_image.size
    pixels = np.array(bake_image.pixels[:], dtype=np.float32).reshape(h, w, 4)
    is_unpainted_black = pixels[:, :, :3].max(axis=-1) < 0.03
    black_fraction = is_unpainted_black.mean()
    print(f"[INFO] Post-bake check: {black_fraction:.1%} of the texture is "
          f"still unpainted black -- replacing with flat skin tone "
          f"{skin_tone[:3]}.")
    skin_rgba = np.array([*skin_tone[:3], 1.0], dtype=np.float32)
    pixels[is_unpainted_black] = skin_rgba
    bake_image.pixels[:] = pixels.flatten()

    if mirror_fill:
        import numpy as np
        w, h = bake_image.size
        pixels = np.array(bake_image.pixels[:], dtype=np.float32).reshape(h, w, 4)
        flipped = pixels[:, ::-1, :]
        skin = np.array(skin_tone, dtype=np.float32)
        is_flat_skin = np.all(np.abs(pixels - skin) < 0.02, axis=-1, keepdims=True)
        merged = np.where(is_flat_skin, flipped, pixels)
        bake_image.pixels[:] = merged.flatten()
        print("[INFO] Mirror-filled non-photographed side using UV symmetry.")

    bake_image.filepath_raw = output_png_path
    bake_image.file_format = 'PNG'
    bake_image.save()
    print(f"[SUCCESS] Baked head texture saved to: {output_png_path}")

    final_mat = bpy.data.materials.new(name="HeadTextureFinal")
    final_mat.use_nodes = True
    nodes = final_mat.node_tree.nodes
    nodes.clear()
    bsdf = nodes.new(type='ShaderNodeBsdfPrincipled')
    output = nodes.new(type='ShaderNodeOutputMaterial')
    tex_node = nodes.new(type='ShaderNodeTexImage')
    tex_node.image = bake_image
    uv_map_node = nodes.new(type='ShaderNodeUVMap')
    uv_map_node.uv_map = bake_uv_name
    links = final_mat.node_tree.links
    links.new(uv_map_node.outputs['UV'], tex_node.inputs['Vector'])
    links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
    links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])

    mesh.materials.clear()
    mesh.materials.append(final_mat)
    for poly in mesh.polygons:
        poly.material_index = 0

    # Re-affirm the render UV right before export -- some Blender versions
    # can reset the active-render flag when UV layers or materials are
    # churned. Cheap insurance for the earlier "wrong UV exported" bug.
    if bake_uv_name in mesh.uv_layers:
        mesh.uv_layers[bake_uv_name].active_render = True

    # FIX: reported real-world symptom -- a third-party website viewer
    # (not gltf-viewer.donmccurdy.com, which renders this correctly)
    # showed a blotchy, scrambled texture despite the GLB itself being
    # confirmed correct. Root cause: this export still carries 2-3 UV
    # sets (the donor mesh's original UV, "FaceProjection", and the
    # correct baked "BakeUV" one) -- the material correctly points its
    # texCoord at whichever index BakeUV ends up as (previously seen as
    # TEXCOORD_2 in this pipeline), but a naive/custom renderer that
    # assumes UV set 0 by default (common shortcut, since most glTF files
    # only have one UV set) would sample the WRONG, leftover UV data
    # instead. Removing every OTHER UV layer here guarantees BakeUV
    # becomes TEXCOORD_0 -- correct regardless of whether the consuming
    # renderer respects the texCoord index or just assumes set 0.
    other_uv_layers = [name for name in mesh.uv_layers.keys() if name != bake_uv_name]
    for name in other_uv_layers:
        mesh.uv_layers.remove(mesh.uv_layers[name])
    print(f"[INFO] Removed {len(other_uv_layers)} unused UV layer(s) "
          f"{other_uv_layers} before export, leaving only '{bake_uv_name}' "
          f"-- this guarantees it exports as TEXCOORD_0, fixing "
          f"compatibility with renderers that don't respect the "
          f"material's texCoord index.")


# Per-donor-object tuning overrides for head_fraction / face_scale_margin.
#
# WHY THIS EXISTS: both constants were originally fixed numbers (0.14 and
# whatever --face-scale-margin defaults to) tuned by eye against ONE donor
# mesh. If the male and female objects in human_base_meshes_bundle.blend
# have different proportions or poly density, the same constants can be
# right for one gender and wrong for the other -- which matches "face
# texture still wrong, and male/female have very different poly counts".
# select_head_vertices() now adapts head_fraction upward automatically when
# a mesh is too sparse, which should reduce (not necessarily eliminate)
# this. If face placement is still off for one gender after that, add a
# per-object entry here with values tuned specifically for that mesh --
# cheaper than re-deriving a single constant that has to work for both.
PER_OBJECT_TUNING = {
    # "GEO-body_female_realistic": {"head_fraction": 0.14, "face_scale_margin": 0.75},
    # "GEO-body_male_realistic": {"head_fraction": 0.14, "face_scale_margin": 0.75},
}


def main():
    args = parse_args()
    clean_scene()

    if args["clothing_fit"]:
        # Separate, much shorter code path -- no face texture, no photo,
        # no landmarks. Exits inside run_clothing_fit() (success) or via
        # sys.exit(1) (fit failed), so nothing below this branch runs.
        run_clothing_fit(args)
        return

    if args["mpfb_live"]:
        print(f"[INFO] --mpfb-live: generating body live via MPFB2 "
              f"(donor path '{args['donor']}' is ignored in this mode).")
        human = generate_mpfb_human(
            args["gender_value"], args["age"], args["weight"])
        head_fraction = 0.14
        face_scale_margin = args["face_scale_margin"]
        print("[INFO] --mpfb-live: using default head_fraction/"
              "face_scale_margin (PER_OBJECT_TUNING doesn't apply -- "
              "it's keyed by static donor object name, which this path "
              "doesn't use. Tune these directly if face placement needs "
              "adjustment on the MPFB basemesh's proportions).")
    else:
        tuning = PER_OBJECT_TUNING.get(args["object"], {})
        head_fraction = tuning.get("head_fraction", 0.14)
        face_scale_margin = tuning.get("face_scale_margin", args["face_scale_margin"])
        if tuning:
            print(f"[INFO] Using per-object tuning for '{args['object']}': {tuning}")

        human = append_donor_body(args["donor"], args["object"])

    # Ordering (fixed): apply_age_weight_morphs() now runs BEFORE
    # finalize_shape_keys(), so if this donor's age/weight morphs really
    # are shape keys, they're still present when values get set, and the
    # mix gets baked in (not discarded) by finalize_shape_keys() right
    # after. recenter_mesh_x() runs last of these three so it operates on
    # the final flattened mesh, not on live shape-key data.
    #
    # In --mpfb-live mode, gender/age/weight were already set via MPFB's
    # own macro-detail system inside generate_mpfb_human() -- this old
    # Age/Weight-named lookup will correctly no-op (see finalize_shape_keys
    # docstring: it now ALSO recognizes and bakes MPFB's '$md-' prefixed
    # macro-detail shape keys, so the actual baking still happens, just not
    # via this function call).
    apply_age_weight_morphs(human, args["age"], args["weight"])
    finalize_shape_keys(human)
    recenter_mesh_x(human)

    if args["landmarks"] and not args["skip_head_warp"]:
        deform_head_from_landmarks(human, args["landmarks"], head_fraction=head_fraction)
    elif args["landmarks"] and args["skip_head_warp"]:
        print("[INFO] --skip-head-warp set; leaving mesh proportions as-is "
              "(landmarks will still be used for texture position/scale).")
    else:
        print("[INFO] No --landmarks provided; skipping head shape deformation "
              "(mesh will use the donor's default proportions).")

    print("#" * 70)
    print(f"### SCENE OBJECTS after body append ({len(bpy.data.objects)} total):")
    for obj in bpy.data.objects:
        print(f"###   - {obj.name} (type={obj.type}, verts={len(obj.data.vertices) if obj.type == 'MESH' else 'n/a'})")
    print(f"### gender={args['gender']} (value={args['gender_value']:.3f}) "
          f"age={args['age']:.2f} weight={args['weight']:.2f} "
          f"mpfb_live={args['mpfb_live']}")
    print("#" * 70)

    face_mat, skin_tone = project_face_texture(
        human, args["image"],
        landmarks_path=args["landmarks"],
        head_fraction=head_fraction,
        face_bias=args["face_bias"],
        face_scale_margin=face_scale_margin,
        skin_tone_adjust=args["skin_tone_adjust"],
        debug_mask=args["debug_mask"],
        output_path=args["output"],
    )

    # CHANGED: project_face_texture() now returns an already-finished flat
    # material (skin color from the photo, no projected texture) -- no
    # baking step needed or possible (there's no camera-projected UV
    # layout for bake_face_texture() to read from anymore). Left the
    # function itself defined/unchanged below in case this gets reverted.
    print("[INFO] Skipping bake_face_texture() -- face material is already "
          "a finished flat color, not a projected photo texture.")

    # FIX: export_apply=True ("Apply Modifiers") was fine when there were
    # no shape keys or armature to worry about, but it's documented to
    # conflict with shape-key (morph target) export in Blender's glTF
    # exporter -- applying modifiers at export time would bake away the
    # very viseme shape keys this pipeline now needs to preserve, and would
    # also bake the live Armature deformation into static geometry, same
    # issue as the modifier_apply skip in append_donor_body(). By this
    # point every modifier that SHOULD be applied already has been
    # (MULTIRES/SUBSURF removed, everything else applied in
    # append_donor_body/deform_head_from_landmarks); the only modifier that
    # should still be live here is Armature, which the exporter needs to
    # see un-applied to write correct skin/joint data. export_skins and
    # export_morph are also Blender defaults, but set explicitly so this
    # doesn't silently regress if a future Blender version changes them.
    bpy.ops.export_scene.gltf(
        filepath=args["output"],
        export_format='GLB',
        export_materials='EXPORT',
        export_yup=True,
        export_apply=False,
        export_skins=True,
        export_morph=True,
    )

    print(f"[SUCCESS] Exported avatar to: {args['output']}")


if __name__ == "__main__":
    main()
