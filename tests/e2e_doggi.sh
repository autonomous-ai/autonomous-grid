#!/bin/bash
# End-to-end test for Doggi media integration.
#
# Prerequisites:
#   - DOGGI_API_KEY set in environment (or pass --api-key)
#   - grid login completed (remote mode)
#   - A running relay (grid up)
#
# Usage (env var):
#   export DOGGI_API_KEY=<secret>
#   bash tests/e2e_doggi.sh
#
# Usage (--api-key flag):
#   bash tests/e2e_doggi.sh --api-key <secret>
#
# Note: Invalid API keys are rejected at join time (HTTP 401/403), so you'll see
# an error immediately if the key is wrong — no need to wait for a generation request.

set -e

API_KEY_ARG=""
if [ -n "$1" ] && [ "$1" = "--api-key" ] && [ -n "$2" ]; then
  API_KEY_ARG="--api-key $2"
fi

echo "=== Step 1: Join grid with Doggi API ==="
grid join --api doggi --at http://localhost:8000 $API_KEY_ARG \
  -m doggi:hunyuan-image-3-t2i \
  -m doggi:hunyuan-image-3-i2i \
  -m doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning

echo ""
echo "=== Step 2: Test text-to-image ==="
grid image \
  "a photo of a cat sitting on a chair" \
  -m doggi:hunyuan-image-3-t2i \
  --width 720 \
  --height 720 \
  --steps 4 \
  --output-dir /tmp/doggi-test-output

echo ""
echo "=== Step 3: Check output ==="
ls -la /tmp/doggi-test-output/

echo ""
echo "=== E2E test complete ==="