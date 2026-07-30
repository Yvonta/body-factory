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
    -F "gender=0.0" -F "age=0.5" -F "weight=0.5" \
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
CLOTHING_HTTP=$(curl -sS -o clothing_1.glb -w "%{http_code}" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/clothing/${CLOTHES_NAME}")
echo "HTTP ${CLOTHING_HTTP}"
echo "clothing_1.glb size: $(wc -c < clothing_1.glb) bytes"
if [ "$CLOTHING_HTTP" != "200" ]; then
    echo "WARNING: clothing fit FAILED (HTTP ${CLOTHING_HTTP}) -- clothing_1.glb "
    echo "is an error response, not a real model. See server logs for the "
    echo "actual Blender error. Skipping the clothing cache-check and merge "
    echo "steps below for clothing."
fi
echo ""
if [ "$CLOTHING_HTTP" == "200" ]; then
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
fi
echo "=== Step 2b: fit hair (cache MISS expected -- real Blender run) ==="
HAIR_HTTP=$(curl -sS -o hair_1.glb -w "%{http_code}" \
    "${BASE_URL}/v1/avatar/${AVATAR_ID}/hair/${HAIR_NAME}")
echo "HTTP ${HAIR_HTTP}"
echo "hair_1.glb size: $(wc -c < hair_1.glb) bytes"
if [ "$HAIR_HTTP" != "200" ]; then
    echo "WARNING: hair fit FAILED (HTTP ${HAIR_HTTP}) -- hair_1.glb is an "
    echo "error response, not a real model. See server logs for the actual "
    echo "Blender error. Skipping the hair cache-check and merge steps "
    echo "below for hair."
fi
echo ""
if [ "$HAIR_HTTP" == "200" ]; then
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
fi
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
# FIX: previously always attempted to merge clothing_1.glb/hair_1.glb
# regardless of whether their fit actually succeeded -- if either had
# failed (HTTP 500), that file is an error-response JSON body, not a
# real GLB, and pygltflib would crash with a confusing raw Python
# traceback ("Header does not appear to be valid glb format") instead
# of a clear message pointing at the real problem. Now gated on the
# HTTP status actually captured above: only merge in an asset whose fit
# genuinely returned 200, and explain clearly what's being skipped and
# why otherwise.
MERGE_SCRIPT="$(dirname "$0")/merge_glbs_python.py"
if [ ! -f "$MERGE_SCRIPT" ]; then
    echo "WARNING: merge_glbs_python.py not found next to this script -- "
    echo "skipping merge. Place it in the same directory to enable this step."
elif [ "$CLOTHING_HTTP" != "200" ] && [ "$HAIR_HTTP" != "200" ]; then
    echo "SKIPPED: neither clothing nor hair fit succeeded -- nothing to "
    echo "merge beyond the plain avatar.glb. Fix the underlying fit "
    echo "failures (see Step 2/2b warnings above) before expecting a merge."
else
    CURRENT="avatar.glb"
    if [ "$CLOTHING_HTTP" == "200" ]; then
        if python3 "$MERGE_SCRIPT" "$CURRENT" clothing_1.glb merged_body_clothing.glb; then
            CURRENT="merged_body_clothing.glb"
        else
            echo "WARNING: merging clothing failed unexpectedly (HTTP was 200, "
            echo "so this is a real merge_glbs_python.py bug, not a skipped "
            echo "failed fit) -- see error above."
        fi
    else
        echo "SKIPPED merging clothing: fit failed (HTTP ${CLOTHING_HTTP})."
    fi
    if [ "$HAIR_HTTP" == "200" ]; then
        if python3 "$MERGE_SCRIPT" "$CURRENT" hair_1.glb merged.glb; then
            CURRENT="merged.glb"
        else
            echo "WARNING: merging hair failed unexpectedly (HTTP was 200, "
            echo "so this is a real merge_glbs_python.py bug, not a skipped "
            echo "failed fit) -- see error above."
        fi
    else
        echo "SKIPPED merging hair: fit failed (HTTP ${HAIR_HTTP})."
        # Still produce a "merged.glb" if only clothing succeeded, so the
        # rest of this script's messaging below stays accurate either way.
        if [ "$CURRENT" != "avatar.glb" ] && [ "$CURRENT" != "merged.glb" ]; then
            cp "$CURRENT" merged.glb
            CURRENT="merged.glb"
        fi
    fi
    if [ -f "merged.glb" ] && [ "$CURRENT" == "merged.glb" ]; then
        echo "merged.glb size: $(wc -c < merged.glb) bytes (includes: avatar$( [ "$CLOTHING_HTTP" == "200" ] && echo " + clothing" )$( [ "$HAIR_HTTP" == "200" ] && echo " + hair" ))"
    fi
fi
echo ""
echo "Done. Load merged.glb (if produced above) into "
echo "https://gltf-viewer.donmccurdy.com/ to see the results together in "
echo "one file (same skeleton/bind pose)."
