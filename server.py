"""
Yvonta's Body Factory 
---------------------
Author...: Dirk Jan Buter (Yvonta)
Email....: hello@yvonta.com
Date.....: 29-07-2026
License..: CC0 1.0 Universal

The Yvonta's Body Factory web API server

"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
import subprocess
import json
import os
import re
import shutil
import uuid

from remove_background import remove_background

# 3. Omgevingsvariabelen
env = os.environ.copy()
env["BLENDER_USER_CONFIG"] = "/root/.config/blender"
# NOTE: this points at a Blender 5.1 extensions path. Local testing during
# --mpfb-live development was done against Blender 4.5.2, where MPFB2
# resolved as 'bl_ext.blender_org.mpfb' via Blender's own extension-loading
# system -- independent of this PYTHONPATH override. Worth confirming this
# path actually matches whatever Blender version/profile server.py's
# subprocess runs in production; generator_core.py's _find_mpfb_module_name()
# searches several candidate module paths regardless, so a mismatch here
# likely isn't fatal, but may be pointing at the wrong place for no reason.
env["PYTHONPATH"] = "/root/.config/blender/5.1/extensions/user_default"
# Force headless mode and GPU compatibility
env["DISPLAY"] = ":0"
env["MESA_GL_VERSION_OVERRIDE"] = "4.3"
env["MESA_GLSL_VERSION_OVERRIDE"] = "430"

# NEW: persistent per-avatar storage. Previously every generated avatar
# lived only in /tmp and was deleted right after being streamed back
# (background_tasks.add_task(os.remove, target_file) below) -- fine when
# the GLB was the only thing anyone ever needed, but the on-demand
# clothing endpoint needs to (a) reproduce this avatar's exact body/rig
# later, from the same gender/age/weight, and (b) cache fitted clothing
# GLBs per avatar so a second request for the same avatar+garment is
# instant. Both require the avatar to still exist after the original
# request returns.
AVATAR_STORAGE_DIR = os.environ.get("AVATAR_STORAGE_DIR", "/data/avatars")

# Path to the MediaPipe Selfie Segmenter model used by remove_background()
# below. Download once, same directory as this file / generator_core.py:
#   curl -L -o selfie_segmenter.tflite \
#     https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite
SELFIE_SEGMENTER_MODEL_PATH = os.environ.get(
    "SELFIE_SEGMENTER_MODEL_PATH", "selfie_segmenter.tflite"
)

# Both avatar_id (a uuid4 we generate ourselves) and clothes_name (comes
# from the client) end up as path components below -- restrict both to a
# safe slug so neither can path-traverse (e.g. "../../etc/passwd") or
# inject shell-meaningful characters into the Blender subprocess argv.
_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _require_safe_slug(value: str, field_name: str) -> str:
    if not value or not _SAFE_SLUG_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field_name} '{value}' -- only letters, "
                   f"digits, '_' and '-' are allowed."
        )
    return value


app = FastAPI()


class AvatarRequest(BaseModel):
    gender: str = "female"
    age: float = 0.3
    weight: float = 0.5


@app.post("/v1/avatar/generate")
async def generate_avatar(
    background_tasks: BackgroundTasks,
    gender: str = Form("0.0"),
    age: float = Form(0.3),
    weight: float = Form(0.5),
    skin_tone_adjust: int = Form(0),
    file: UploadFile = File(...)
):
    # --- Validate early so bad input fails fast instead of silently ---
    # CHANGED: gender now accepts EITHER the word "male"/"female" (old
    # contract, kept for backward compatibility with existing callers) OR
    # a continuous float string like "0.3" (new -- generator_core.py's
    # --mpfb-live path uses MakeHuman's own continuous gender convention:
    # 0.0=female, 1.0=male). Both forms are passed straight through in
    # config_data; generator_core.py's parse_args() already handles
    # parsing either representation.
    gender = gender.strip().lower()
    if gender not in ("male", "female"):
        try:
            gender_float = float(gender)
            if not (0.0 <= gender_float <= 1.0):
                raise ValueError()
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid gender '{gender}'; expected 'male', 'female', "
                       f"or a continuous value between 0.0 (female) and 1.0 (male)."
            )
    if not (0.0 <= age <= 1.0):
        raise HTTPException(status_code=400, detail=f"age must be in [0.0, 1.0], got {age}")
    if not (0.0 <= weight <= 1.0):
        raise HTTPException(status_code=400, detail=f"weight must be in [0.0, 1.0], got {weight}")
    if not (-6 <= skin_tone_adjust <= 6):
        raise HTTPException(status_code=400, detail=f"skin_tone_adjust must be in [-6, 6], got {skin_tone_adjust}")

    job_id = str(uuid.uuid4())
    target_file = f"/tmp/avatar_{job_id}.glb"
    landmarks_file = f"/tmp/landmarks_{job_id}.json"

    # Save the uploaded image temporarily
    image_extension = os.path.splitext(file.filename)[1] or ".jpg"
    temp_image_path = f"/tmp/input_{job_id}{image_extension}"

    try:
        with open(temp_image_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded image: {str(e)}")

    # Normalize EXIF orientation ONCE, here, before ANY downstream consumer
    # (extract_face_landmarks.py / MediaPipe, or generator_core.py / Blender)
    # ever loads this file. Phone photos are commonly stored with the raw
    # pixel data in one orientation plus an EXIF tag saying "display
    # rotated". If MediaPipe respects that tag (likely, via most image
    # loaders) but Blender's bpy.data.images.load() does NOT (it reads raw
    # pixel data, ignoring EXIF orientation), landmark coordinates computed
    # against the correctly-rotated photo land on the WRONG pixels in
    # Blender's unrotated buffer -- explains a face texture bake landing in
    # the wrong place, mostly failing to paint, and skin-tone sampling
    # picking up background/hair instead of skin. Physically rotating the
    # pixels here (and dropping the orientation tag) makes every consumer
    # agree, rather than trying to handle rotation differently in each one.
    try:
        from PIL import Image as PILImage, ImageOps
        img = PILImage.open(temp_image_path)
        normalized = ImageOps.exif_transpose(img)
        normalized.save(temp_image_path)
    except Exception as e:
        # Don't hard-fail generation over this -- proceed with the
        # original file (same behavior as before this fix existed) but
        # make the risk visible in logs rather than failing silently.
        print(f"[WARNING] Could not normalize EXIF orientation for "
              f"{temp_image_path}: {e}")

    # NEW: replace the photo's background with a neutral fill color, same
    # place/pattern as the EXIF fix above -- runs once, in-place, before
    # ANY downstream consumer (landmark extraction or the face-texture
    # projection in generator_core.py/Blender) sees the file. Without
    # this, a colorful/busy background can bleed onto the model wherever
    # the projected face material extends close to the photo's edge (real
    # bug, confirmed via testing: a vivid background shape ended up
    # painted directly onto the jaw/ear area of a generated avatar).
    # Graceful degradation to match the EXIF fix's pattern: never hard-
    # fail generation over this -- proceed with the original photo (same
    # background-bleed risk as before this fix existed) but make it
    # visible in logs rather than failing silently or blocking a request
    # over what's ultimately a quality improvement, not a correctness
    # requirement.
    try:
        remove_background(temp_image_path, temp_image_path,
                           model_path=SELFIE_SEGMENTER_MODEL_PATH)
    except Exception as e:
        print(f"[WARNING] Could not remove background for "
              f"{temp_image_path}: {e}")

    # 1. Stap 1: Genereer landmarks op basis van de geüploade afbeelding
    landmark_cmd = [
        "python3",
        "extract_face_landmarks.py",
        temp_image_path,
        landmarks_file
    ]

    landmark_result = subprocess.run(
        landmark_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env
    )

    if landmark_result.returncode != 0:
        if os.path.exists(temp_image_path):
            os.remove(temp_image_path)
        raise HTTPException(
            status_code=500,
            detail=f"Landmark extraction failed! Stderr: {landmark_result.stderr}\nStdout: {landmark_result.stdout}"
        )

    # 2. Bereid de config voor
    # NOTE: this JSON blob is positional argv[3] on the Blender side (see
    # generator_core.py's parse_args) -- it MUST be a single shell/argv
    # token, so json.dumps with default separators is fine, but never put
    # spaces-containing flags after it without the "--landmarks" etc. token
    # boundaries staying intact (they already do, each is its own argv item).
    config_data = {
        "gender": gender,
        "age": age,
        "weight": weight,
        "skin_tone_adjust": skin_tone_adjust,
    }

    # 3. Stap 2: Bouw het Blender commando met de gegenereerde landmarks
    #
    # FIX: previously this JSON config was appended to argv but
    # generator_core.py's parser only recognized fixed flag strings
    # (--object, --landmarks, etc.) and silently dropped anything else,
    # including this JSON blob -- so gender/age/weight never reached the
    # mesh generation logic. generator_core.py now parses this positional
    # JSON argument explicitly (see its parse_args()).
    #
    # CHANGED: --mpfb-live makes generator_core.py generate the body LIVE
    # via MPFB2 (continuous gender/age/weight, real rig + visemes) instead
    # of appending the old static human_base_meshes_bundle.blend donor.
    # The donor path argument below is IGNORED in this mode (kept as a
    # placeholder positional arg since generator_core.py's parse_args
    # still expects argv[0] to be present) -- if you ever revert to the
    # static-donor path, remove --mpfb-live and this placeholder becomes
    # the real donor path again.
    #
    # REMOVED: --face-scale-margin 1.2 was tuned specifically for the old
    # donor mesh's proportions. Carrying it forward onto the MPFB basemesh
    # unchanged risks reintroducing the exact face-placement mismatch this
    # was previously debugged for -- needs fresh empirical tuning against
    # the new mesh, not a value inherited from a different one. Falls back
    # to generator_core.py's own default (0.75) until re-tuned.
    cmd = [
        "/opt/blender/blender",
        "-b",
        "-P", "generator_core.py",
        "--",
        "human_base_meshes_bundle.blend",  # ignored when --mpfb-live is set
        temp_image_path,
        target_file,
        json.dumps(config_data),
        "--landmarks", landmarks_file,
        "--skip-head-warp",
        "--mpfb-live",
        "--debug-mask"
    ]

    # 4. Blender uitvoeren en stderr opvangen
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env
    )

    # FIX: previously Blender's console output was only ever surfaced to
    # the CALLER when the subprocess crashed (via the HTTPException
    # below) -- for a successful generation, result.stdout was captured
    # into this variable and then simply discarded once the function
    # returned, with no way to see it afterward. That blocked debugging
    # anything printed by generator_core.py's own diagnostic output
    # (material slot names, UV footprint info, etc.) on any request that
    # didn't outright crash -- which is exactly the case that needed
    # debugging (a successful generation with wrong-looking output).
    # Printing it here goes to server.py's OWN stdout, which Docker
    # captures automatically -- `docker logs <container>` now shows the
    # full Blender console output for every request, not just failures.
    print(f"### Blender output for job {job_id} (gender={gender}, age={age}, "
          f"weight={weight}, skin_tone_adjust={skin_tone_adjust}, "
          f"returncode={result.returncode}) ###")
    print(result.stdout)
    print(f"### end Blender output for job {job_id} ###")

    # Opruimen van tijdelijke bestanden (afbeelding en landmarks)
    for tmp_path in [temp_image_path, landmarks_file]:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # 5. Check op fouten in Blender
    if result.returncode != 0:
        if os.path.exists(target_file):
            os.remove(target_file)

        raise HTTPException(
            status_code=500,
            detail=f"Blender crash! Stderr: {result.stderr}\nStdout: {result.stdout}"
        )

    # 6. Bestand teruggeven
    if os.path.exists(target_file):
        # NEW: copy into persistent per-avatar storage (keyed by job_id,
        # now doubling as this avatar's public avatar_id) before serving,
        # and save the macro config alongside it. This is what lets the
        # clothing endpoint below rebuild an identical body/skeleton for
        # this exact avatar later, without needing the original photo.
        avatar_dir = os.path.join(AVATAR_STORAGE_DIR, job_id)
        os.makedirs(avatar_dir, exist_ok=True)
        persistent_glb = os.path.join(avatar_dir, "avatar.glb")
        shutil.copyfile(target_file, persistent_glb)
        with open(os.path.join(avatar_dir, "macros.json"), "w") as f:
            json.dump(config_data, f)

        # The /tmp copy was only ever scratch space for this one request --
        # the persistent copy above is what survives. Still fine to clean
        # up /tmp right after the response is sent.
        background_tasks.add_task(os.remove, target_file)
        return FileResponse(
            path=persistent_glb,
            filename=f"avatar_{job_id}.glb",
            media_type="model/gltf-binary",
            headers={"X-Avatar-Id": job_id},
        )

    raise HTTPException(
        status_code=500,
        detail=f"Blender finished but no file found at {target_file}. Logs: {result.stdout}"
    )


@app.get("/v1/avatar/{avatar_id}/clothing/{clothes_name}")
async def get_avatar_clothing(avatar_id: str, clothes_name: str):
    """On-demand, cached clothing fitting. First request for a given
    (avatar_id, clothes_name) pair costs a real Blender run (rebuilds this
    avatar's body from its saved macros, fits the garment, exports it);
    every request after that is served straight from disk.

    Mirrors avatar-creator-pro's acp_get_animations pattern: PHP just
    proxies to this and passes through whatever comes back.
    """
    avatar_id = _require_safe_slug(avatar_id, "avatar_id")
    clothes_name = _require_safe_slug(clothes_name, "clothes_name")

    avatar_dir = os.path.join(AVATAR_STORAGE_DIR, avatar_id)
    macros_path = os.path.join(avatar_dir, "macros.json")
    if not os.path.exists(macros_path):
        raise HTTPException(
            status_code=404,
            detail=f"No avatar found with id '{avatar_id}' (or it has no "
                   f"saved macros -- was it generated before this endpoint "
                   f"existed?)."
        )

    clothing_dir = os.path.join(avatar_dir, "clothing")
    os.makedirs(clothing_dir, exist_ok=True)
    cache_path = os.path.join(clothing_dir, f"{clothes_name}.glb")

    # --- Fast path: already fitted for this avatar, no Blender needed ---
    if os.path.exists(cache_path):
        return FileResponse(
            path=cache_path,
            filename=f"{avatar_id}_{clothes_name}.glb",
            media_type="model/gltf-binary",
        )

    # --- Slow path: fit it now, then cache for every request after this ---
    with open(macros_path) as f:
        macros = json.load(f)

    cmd = [
        "/opt/blender/blender",
        "-b",
        "-P", "generator_core.py",
        "--",
        "human_base_meshes_bundle.blend",  # ignored in --clothing-fit mode
        "/dev/null",                       # image path -- ignored, no photo needed
        cache_path,
        json.dumps(macros),
        "--clothing-fit", clothes_name,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env
    )

    print(f"### Blender clothing-fit output for avatar {avatar_id}, "
          f"garment '{clothes_name}' (returncode={result.returncode}) ###")
    print(result.stdout)
    print(f"### end Blender clothing-fit output ###")

    if result.returncode != 0:
        if os.path.exists(cache_path):
            os.remove(cache_path)
        raise HTTPException(
            status_code=500,
            detail=f"Blender crash while fitting clothing! Logs: {result.stdout}"
        )

    if not os.path.exists(cache_path):
        raise HTTPException(
            status_code=500,
            detail=f"Blender finished but no clothing file found at "
                   f"{cache_path}. Logs: {result.stdout}"
        )

    return FileResponse(
        path=cache_path,
        filename=f"{avatar_id}_{clothes_name}.glb",
        media_type="model/gltf-binary",
    )
