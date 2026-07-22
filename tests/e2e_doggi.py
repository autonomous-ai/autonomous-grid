#!/usr/bin/env python3
"""End-to-end sanity test for Doggi media integration.

Requires:
  - DOGGI_API_KEY in environment (or pass --api-key to grid join)
  - A running relay (grid up)
  - A joined Doggi engine (grid join --api doggi --at <url>)

Run (env var):
  export DOGGI_API_KEY=<secret>
  grid join --api doggi --at http://localhost:8000 \
    -m doggi:hunyuan-image-3-t2i -m doggi:hunyuan-image-3-i2i \
    -m doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning
  python tests/e2e_doggi.py

Run (--api-key flag):
  grid join --api doggi --at http://localhost:8000 \
    --api-key <secret> \
    -m doggi:hunyuan-image-3-t2i -m doggi:hunyuan-image-3-i2i \
    -m doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning
  python tests/e2e_doggi.py

Note: Invalid API keys are rejected at join time (HTTP 401/403), so you'll see
an error immediately if the key is wrong — no need to wait for a generation request.

This is a manual smoke test — not automated in CI.
"""
import json
import os
import sys
from pathlib import Path

import httpx

RELAY_URL = os.environ.get("GRID_RELAY_URL", "http://127.0.0.1:8000")
ACCESS_TOKEN = os.environ.get("GRID_ACCESS_TOKEN")
OUTPUT_DIR = Path(os.environ.get("GRID_OUTPUT_DIR", "/tmp/doggi-test-output"))


def get_consumer_client():
    """Build an authenticated httpx client for the relay."""
    if not ACCESS_TOKEN:
        print("ERROR: GRID_ACCESS_TOKEN not set. Run `grid login` first.", file=sys.stderr)
        sys.exit(1)
    return httpx.Client(
        base_url=RELAY_URL,
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        timeout=600,
    )


def consume_sse(resp, output_dir):
    """Consume SSE response and write output files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    wrote_any = False
    for line in resp.iter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            print(line)
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            print(data)
            continue
        if "error" in event:
            print(f"Error: {event['error']}", file=sys.stderr)
            continue
        if event.get("type") == "progress":
            progress = event.get("progress")
            status = event.get("status", "running")
            print(f"  progress={progress}% status={status}", file=sys.stderr)
            continue
        if event.get("type") == "result":
            for item in event.get("output_files", []):
                filename = item.get("filename", "output")
                content_b64 = item.get("content_base64")
                if not content_b64:
                    continue
                import base64
                out_path = output_dir / filename
                out_path.write_bytes(base64.b64decode(content_b64))
                print(f"  Wrote: {out_path}")
                wrote_any = True
            continue
        print(json.dumps(event, sort_keys=True))
    return wrote_any


def test_t2i():
    """Submit a text-to-image request and verify we get a result."""
    print("\n=== Testing text-to-image ===")
    client = get_consumer_client()
    print("  Submitting: doggi:hunyuan-image-3-t2i, prompt='a photo of a cat'")
    resp = client.post(
        "/relay/v1/media/image/generate",
        json={
            "model": "doggi:hunyuan-image-3-t2i",
            "prompt": "a photo of a cat sitting on a chair",
            "width": 720,
            "height": 720,
            "steps": 4,
        },
    )
    if resp.status_code != 200:
        print(f"  FAILED: HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    wrote = consume_sse(resp, OUTPUT_DIR)
    if wrote:
        print("  OK: t2i returned a result")
        return True
    print("  FAILED: no output files written")
    return False


def test_i2i():
    """Submit an image-to-image request (requires a local image file)."""
    print("\n=== Testing image-to-image ===")
    # Create a tiny test image
    # 1x1 PNG (smallest valid PNG)
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    client = get_consumer_client()
    print("  Submitting: doggi:hunyuan-image-3-i2i")
    resp = client.post(
        "/relay/v1/media/image/edit",
        json={
            "model": "doggi:hunyuan-image-3-i2i",
            "prompt": "make it look like a painting",
            "steps": 4,
            "input_images": [{
                "filename": "input.png",
                "content_base64": png_b64,
            }],
            "aspect_ratio": "1:1",
        },
    )
    if resp.status_code != 200:
        print(f"  FAILED: HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    wrote = consume_sse(resp, OUTPUT_DIR)
    if wrote:
        print("  OK: i2i returned a result")
        return True
    print("  FAILED: no output files written")
    return False


def test_i2v():
    """Submit an image-to-video request."""
    print("\n=== Testing image-to-video ===")
    # 1x1 PNG
    png_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    client = get_consumer_client()
    print("  Submitting: doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning (this may take minutes)")
    resp = client.post(
        "/relay/v1/media/video/i2v",
        json={
            "model": "doggi:Wan-AI/Wan2.2-I2V-A14B-Lightning",
            "prompt": "the image comes to life",
            "input_image": {
                "filename": "input.png",
                "content_base64": png_b64,
            },
            "duration": 5,
            "aspect_ratio": "1:1",
        },
    )
    if resp.status_code != 200:
        print(f"  FAILED: HTTP {resp.status_code}: {resp.text[:200]}")
        return False
    wrote = consume_sse(resp, OUTPUT_DIR)
    if wrote:
        print("  OK: i2v returned a result")
        return True
    print("  FAILED: no output files written")
    return False


if __name__ == "__main__":
    print("Doggi E2E Test")
    print(f"Relay: {RELAY_URL}")
    print(f"Output: {OUTPUT_DIR}")

    results = {}
    results["t2i"] = test_t2i()
    results["i2i"] = test_i2i()
    results["i2v"] = test_i2v()

    print("\n=== Summary ===")
    for test_name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        print(f"  {test_name}: {status}")

    if all(results.values()):
        print("\nAll tests passed!")
        sys.exit(0)
    else:
        print("\nSome tests failed.")
        sys.exit(1)