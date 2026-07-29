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

# End-to-end test for /v1/avatar/generate and /v1/avatar/{id}/clothing/{name}
#
# Usage:
#   ./test.sh path/to/face.jpg [clothes_name]
#
# Env vars:
#   BASE_URL   default: http://localhost:8000
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8090}"
IMAGE_PATH="${1:?Usage: ./test_api.sh path/to/face.jpg [clothes_name]}"
CLOTHES_NAME="${2:-toigo_basic_tucked_t-shirt}"

if [ ! -f "$IMAGE_PATH" ]; then
    echo "ERROR: no such file: $IMAGE_PATH"
    exit 1
fi

echo "=== Step 1: generate avatar ==="
HEADERS_FILE=$(mktemp)
curl -sS -D "$HEADERS_FILE" -o avatar.glb \
    -F "gender=0.0" -F "age=0.4" -F "weight=0.5" \
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

echo ""
echo "Done. Load avatar.glb and clothing_1.glb together into "
echo "https://gltf-viewer.donmccurdy.com/ to check the garment lines up "
echo "visually with the body (same skeleton/bind pose)."
