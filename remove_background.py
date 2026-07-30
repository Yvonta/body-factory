#!/usr/bin/env python3
"""
remove_background.py -- replace a selfie photo's background with a neutral
fill color using MediaPipe's Selfie Segmentation model, so that any
mesh polygon that happens to sample near the edge of the projected face
(e.g. jaw/ear-adjacent geometry) picks up a plausible neutral tone instead
of whatever's actually behind the person (a colorful wall, other people,
clutter, etc).

Integration point: call this in server.py, in the SAME place the EXIF
orientation fix already runs -- right after the uploaded photo is saved
to disk, before extract_face_landmarks.py or generator_core.py/Blender
ever load it. Both of those downstream consumers should see the
background-replaced version, not the original.

Requires the Selfie Segmenter model file. Download once (this exact URL
is Google's official, stable MediaPipe model host):

    curl -L -o selfie_segmenter.tflite \\
        https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite

Usage:
    python3 remove_background.py input.jpg output.jpg [--model selfie_segmenter.tflite]

Or import and call remove_background(...) directly from server.py.
"""
import argparse
import json
import sys

import numpy as np
from PIL import Image

import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

# Same landmark indices already used elsewhere in this pipeline
# (generator_core.py's measure_photo_face_features() etc.) -- reusing
# them here keeps this consistent with the rest of the codebase rather
# than picking new ones.
LM_FOREHEAD = 10
LM_CHIN = 152
LM_CHEEK_LEFT = 234
LM_CHEEK_RIGHT = 454


def _geometric_face_mask(landmarks_path: str, img_w: int, img_h: int,
                          horizontal_expand: float = 1.4,
                          upward_expand: float = 1.0,
                          downward_expand: float = 2.2) -> np.ndarray:
    """Build a generous elliptical region around the detected face, using
    the SAME landmarks.json format extract_face_landmarks.py already
    produces elsewhere in this pipeline (data['landmarks'] indexed by
    MediaPipe's standard point indices, data['image_width']/['image_height']).

    Anything outside this region gets excluded from the foreground mask
    regardless of what the segmentation model thinks -- a second,
    independent signal for the case morphological cleanup alone can't
    handle: a background region the model confidently (not just
    ambiguously) merges with the real hair/head, immediately adjacent to
    it. This can't fix that specific failure mode if the misclassified
    region sits WITHIN this expansion (still close to the head) -- it
    targets cases where the segmenter's confident-but-wrong region
    extends further from the head than any real hair/shoulder content
    would.

    Expansion factors are multiples of face_height/face_width (chin-to-
    forehead / cheek-to-cheek), tuned to realistic anatomy rather than
    generic "be generous" guesses -- a first pass at 2.5x/4.0x upward/
    downward covered 84.7% of a synthetic test image, far too loose to
    exclude anything actually close to the head. upward_expand=1.0 gives
    room for a tall/voluminous hairstyle without covering most of the
    frame; downward_expand=2.2 covers shoulders/upper chest in a typical
    selfie framing.
    """
    with open(landmarks_path) as f:
        data = json.load(f)
    lm = data["landmarks"]
    w = data.get("image_width", img_w)
    h = data.get("image_height", img_h)

    def px(i):
        return lm[i]["x"] * w, lm[i]["y"] * h

    _, forehead_y = px(LM_FOREHEAD)
    _, chin_y = px(LM_CHIN)
    cheek_l_x, _ = px(LM_CHEEK_LEFT)
    cheek_r_x, _ = px(LM_CHEEK_RIGHT)

    face_height = max(abs(chin_y - forehead_y), 1.0)
    face_width = max(abs(cheek_r_x - cheek_l_x), 1.0)
    center_x = (cheek_l_x + cheek_r_x) / 2.0
    center_y = forehead_y

    semi_x = face_width * horizontal_expand
    semi_up = face_height * upward_expand
    semi_down = face_height * downward_expand

    ys, xs = np.mgrid[0:img_h, 0:img_w]
    dx = (xs - center_x) / semi_x
    dy = np.where(ys < center_y, (ys - center_y) / semi_up,
                  (ys - center_y) / semi_down)
    return (dx ** 2 + dy ** 2) <= 1.0


def remove_background(input_path: str, output_path: str,
                       model_path: str = "selfie_segmenter.tflite",
                       confidence_threshold: float = 0.7,
                       landmarks_path: str = None) -> None:
    """Replace background pixels in input_path with a neutral fill color
    (the average color of the foreground/person region itself, so any
    future edge-sampling lands on a plausible skin/hair-ish tone rather
    than something jarring), writing the result to output_path.

    landmarks_path (optional): path to a landmarks.json in the same
    format extract_face_landmarks.py produces elsewhere in this
    pipeline. When given, adds a geometric constraint (see
    _geometric_face_mask) alongside the ML segmentation, to catch cases
    where the model confidently misclassifies a background region
    immediately adjacent to the real head as foreground.
    """
    base_options = mp_python.BaseOptions(model_asset_path=model_path)
    options = vision.ImageSegmenterOptions(
        base_options=base_options,
        output_category_mask=False,
        output_confidence_masks=True,
    )

    img = Image.open(input_path).convert("RGB")
    arr = np.array(img)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=arr)

    with vision.ImageSegmenter.create_from_options(options) as segmenter:
        result = segmenter.segment(mp_image)

    # confidence_masks[0] is the foreground (person) confidence for the
    # selfie segmenter model specifically -- confirmed via MediaPipe's
    # documented model output for this task. numpy_view() returns it with
    # a trailing size-1 channel dimension (H, W, 1), not (H, W) -- squeeze
    # it down to 2D so it lines up correctly against the (H, W, 3) RGB
    # image below (confirmed via a real crash: using it un-squeezed raised
    # "boolean index did not match indexed array along axis 2").
    confidence_mask = result.confidence_masks[0].numpy_view()
    if confidence_mask.ndim == 3:
        confidence_mask = confidence_mask[:, :, 0]
    foreground_mask = confidence_mask > confidence_threshold

    if landmarks_path is not None:
        geo_mask = _geometric_face_mask(landmarks_path, arr.shape[1], arr.shape[0])
        before_count = foreground_mask.sum()
        foreground_mask = foreground_mask & geo_mask
        dropped_geo = before_count - foreground_mask.sum()
        print(f"[INFO] remove_background: geometric face-region constraint "
              f"dropped ~{dropped_geo} pixels outside the expanded "
              f"face/hair/shoulder region.")

    # FIX: a real test showed an isolated patch of background (a vivid
    # colorful shape unconnected to the person) surviving alongside the
    # person -- the model misclassified it as foreground too. The actual
    # person is always ONE connected region; a stray misclassified patch
    # elsewhere in the image isn't connected to it. A second real test
    # showed some stray patches are joined to the real foreground by a
    # thin bridge of ambiguous edge pixels, which plain connected-
    # component labeling can't separate (they're technically one region).
    # Morphological opening (erosion then dilation) breaks thin bridges
    # like this while preserving the bulk of larger regions -- then
    # keeping only the largest resulting component removes the stray
    # patch cleanly. This shaves a few pixels off the true edge (a real,
    # accepted tradeoff -- radius 7, confirmed to reliably break bridges
    # roughly 2x thicker than radius 3 could handle, with ~3% pixel loss
    # on the main region in that harder synthetic test; radius 3 looked
    # fine on a first synthetic test but real photos showed thicker
    # hair-edge bridges than that test covered),
    # not something worth chasing further for this use case.
    from scipy import ndimage
    from skimage.morphology import opening, disk
    opened_mask = opening(foreground_mask, disk(7))
    labeled, num_features = ndimage.label(opened_mask)
    if num_features > 1:
        sizes = ndimage.sum(opened_mask, labeled, range(1, num_features + 1))
        largest_label = np.argmax(sizes) + 1
        dropped = foreground_mask.sum() - int(sizes[largest_label - 1])
        foreground_mask = labeled == largest_label
        print(f"[INFO] remove_background: kept only the largest connected "
              f"foreground region after morphological opening "
              f"({num_features} disconnected regions found, dropped "
              f"~{dropped} stray pixels from the others).")
    elif num_features == 1:
        foreground_mask = opened_mask

    if not foreground_mask.any():
        print(f"[WARNING] remove_background: no foreground detected in "
              f"{input_path} (mask is empty) -- leaving the image "
              f"unmodified rather than risk replacing the whole photo "
              f"with a flat fill.")
        img.save(output_path)
        return

    fill_color = arr[foreground_mask].mean(axis=0).astype(np.uint8)

    out = arr.copy()
    out[~foreground_mask] = fill_color

    Image.fromarray(out).save(output_path)
    print(f"[INFO] remove_background: {input_path} -> {output_path} "
          f"({foreground_mask.mean():.1%} of pixels kept as foreground, "
          f"background replaced with RGB{tuple(int(c) for c in fill_color)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--model", default="selfie_segmenter.tflite")
    parser.add_argument("--threshold", type=float, default=0.7)
    parser.add_argument("--landmarks", default=None,
                         help="Optional path to a landmarks.json (same "
                              "format extract_face_landmarks.py produces) "
                              "to add the geometric face-region constraint.")
    args = parser.parse_args()
    try:
        remove_background(args.input, args.output, args.model,
                           args.threshold, args.landmarks)
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
