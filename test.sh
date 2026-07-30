#!/usr/bin/env bash
# Yvonta's Body Factory 
# ---------------------
# Author...: Dirk Jan Buter (Yvonta)
# Email....: hello@yvonta.com
# Date.....: 29-07-2026
# License..: CC0 1.0 Universal
#
# Testing the Yvonta's Body Factory web API in the commandline using curl
#
# End-to-end test for /v1/avatar/generate, /v1/avatar/{id}/clothing/{name},
# and /v1/avatar/{id}/hair/{name}
#
# Usage:
#   ./test.sh path/to/face.jpg [clothes_name] [hair_name]
#
# Env vars:
#   BASE_URL   default: http://localhost:8000
set -euo pipefail
BASE_URL="${BASE_URL:-http://localhost:8090}"
IMAGE_PATH="${1:?Usage: ./test_api.sh path/to/face.jpg [clothes_name] [hair_name]}"
CLOTHES_NAME="${2:-toigo_basic_tucked_t-shirt}"
HAIR_NAME="${3:-cortu_short_messy_hair}"
if [ ! -f "$IMAGE_PATH" ]; then
    echo "ERROR: no such file: $IMAGE_PATH"
    exit 1
fi
echo "=== Step 1: generate avatar ==="
HEADERS_FILE=$(mktemp)
curl -sS -D "$HEADERS_FILE" -o avatar.glb \
    -F "gender=1.0" -F "age=0.5" -F "weight=0.5" \
    -F "file=@${IMAGE_PATH}" \
    "${BASE_URL}/v1/avatar/generate"
echo "--- response headers ---"
cat "$HEADERS_FILE"
AVATAR_ID=$(grep -i '^x-avatar-id:' "$HEADERS_FILE" | awk '{print $2}' | tr -d '\r')
if [ -z "$AVATAR_ID" ]; then
    echo "ERROR: no X-Avatar-Id header in response -- generate_avatar() failed "
    echo "or crashed before reaching the FileResponse. Check server logs."
    exit 1
fi
echo "avatar_id = ${AVATAR_ID}"
echo "avatar.glb size: $(wc -c < avatar.glb) bytes"
echo ""
echo "=== Step 2: fit clothing (cache MISS expected -- real Blender run) ==="
time curl -sS -o clothing_1.glb -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/clothing/${CLOTHES_NAME}"
echo "clothing_1.glb size: $(wc -c < clothing_1.glb) bytes"
echo ""
echo "=== Step 3: fit SAME clothing again (cache HIT expected -- near-instant) ==="
time curl -sS -o clothing_2.glb -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/clothing/${CLOTHES_NAME}"
if diff -q clothing_1.glb clothing_2.glb > /dev/null; then
    echo "OK: cache hit served byte-identical file to the first fit."
else
    echo "WARNING: clothing_1.glb and clothing_2.glb differ -- caching may "
    echo "not be working as expected."
fi
echo ""
echo "=== Step 2b: fit hair (cache MISS expected -- real Blender run) ==="
time curl -sS -o hair_1.glb -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/hair/${HAIR_NAME}"
echo "hair_1.glb size: $(wc -c < hair_1.glb) bytes"
echo ""
echo "=== Step 3b: fit SAME hair again (cache HIT expected -- near-instant) ==="
time curl -sS -o hair_2.glb -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/hair/${HAIR_NAME}"
if diff -q hair_1.glb hair_2.glb > /dev/null; then
    echo "OK: cache hit served byte-identical file to the first fit."
else
    echo "WARNING: hair_1.glb and hair_2.glb differ -- caching may "
    echo "not be working as expected."
fi
echo ""
echo "=== Step 4: error cases ==="
echo "-- unknown avatar_id (expect 404) --"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/does-not-exist-00000000/clothing/${CLOTHES_NAME}"
echo "-- unsafe clothes_name / path traversal attempt (expect 400) --"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/clothing/..%2F..%2Fetc%2Fpasswd"
echo "-- unsafe avatar_id (expect 400) --"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/..%2F..%2Fetc/clothing/${CLOTHES_NAME}"
echo "-- unknown-but-safe clothes asset name (expect 500, garment not found) --"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/clothing/this-garment-does-not-exist"
echo "-- unknown-but-safe hair asset name (expect 500, hair not found) --"
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/hair/this-hair-does-not-exist"
echo ""
echo "=== Step 5: merge avatar + clothing + hair into one viewable GLB (pure Python, no Blender) ==="
MERGE_SCRIPT="$(dirname "$0")/merge_glbs_python.py"
if [ -f "$MERGE_SCRIPT" ]; then
    if python3 "$MERGE_SCRIPT" avatar.glb clothing_1.glb merged_body_clothing.glb \
        && python3 "$MERGE_SCRIPT" merged_body_clothing.glb hair_1.glb merged.glb; then
        echo "merged.glb size: $(wc -c < merged.glb) bytes"
    else
        echo "WARNING: merge failed -- see error above. You can still load "
        echo "avatar.glb, clothing_1.glb, and hair_1.glb as separate files instead."
    fi
else
    echo "WARNING: merge_glbs_python.py not found next to this script -- "
    echo "skipping merge. Place it in the same directory to enable this step."
fi
echo ""
echo "Done. Load merged.glb into https://gltf-viewer.donmccurdy.com/ to see "
echo "the avatar, garment, and hair together in one file (same skeleton/bind pose)."
