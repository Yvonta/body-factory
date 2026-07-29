"""
Yvonta's Body Factory 
---------------------
Author...: Dirk Jan Buter (Yvonta)
Email....: hello@yvonta.com
Date.....: 29-07-2026
License..: CC0 1.0 Universal

Extracts face landmarks from a photo to build the face of the avatar.

Run this OUTSIDE Blender, in a normal terminal with Python 3.
Install once:  pip install mediapipe --break-system-packages
Usage:         python3 extract_face_landmarks.py photo.jpg landmarks.json
"""
import sys
import json
import os
import urllib.request
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 extract_face_landmarks.py <photo.jpg> <landmarks.json>")
        sys.exit(1)

    image_path, output_path = sys.argv[1], sys.argv[2]

    # Downloads the face landmark model once and caches it locally.
    model_path = "face_landmarker.task"
    if not os.path.exists(model_path):
        print("[INFO] Downloading face landmark model (one-time)...")
        urllib.request.urlretrieve(
            "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
            "face_landmarker/float16/1/face_landmarker.task",
            model_path,
        )

    base_options = mp_python.BaseOptions(model_asset_path=model_path)
    options = vision.FaceLandmarkerOptions(
        base_options=base_options,
        output_face_blendshapes=False,
        num_faces=1,
    )
    landmarker = vision.FaceLandmarker.create_from_options(options)

    image = mp.Image.create_from_file(image_path)
    result = landmarker.detect(image)

    if not result.face_landmarks:
        print("[ERROR] No face detected in the photo. Use a clear, front-facing photo.")
        sys.exit(1)

    landmarks = result.face_landmarks[0]  # 478 normalized (x, y, z) points
    points = [{"x": lm.x, "y": lm.y, "z": lm.z} for lm in landmarks]

    with open(output_path, "w") as f:
        json.dump({"landmarks": points, "image_width": image.width, "image_height": image.height}, f)

    print(f"[SUCCESS] Saved {len(points)} landmarks to {output_path}")
    print("Key indices for reference: left eye outer=33, right eye outer=263, "
          "nose tip=1, chin=152, left jaw=234, right jaw=454, forehead=10")


if __name__ == "__main__":
    main()
