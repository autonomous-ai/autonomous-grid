"""Provider-side media handler for executing ComfyUI workflows.

VENDORED from Interns-Desktop-App/assets/scripts/additional_services_manager/media_handler.py.

Two explicit edits, each bracketed by `# --- vendored edit N: ... ---`:
  1. `_ensure_comfyui_running` - replace the desktop's HTTP POST to
     :8888 /comfyui/start (the desktop's FastAPI manager that this CLI does
     not run) with a direct call to engine.comfyui.ensure_running().
  2. `_get_output_dir` - replace the macOS / dev-only fallback paths with
     Grid paths rooted at ~/.grid/.

Receives media requests from the poll worker, submits workflows to ComfyUI,
tracks progress via WebSocket + HTTP polling, and yields SSE events back.

Usage (from poll_worker.py):
    handler = MediaHandler(comfyui_url)
    for sse_line in handler.handle_request(endpoint_path, body):
        # stream sse_line to relay
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid

import httpx

from shared import paths

logger = logging.getLogger(__name__)

_COMFYUI_RETRY_INTERVAL = 3
_COMFYUI_MAX_RETRIES = 10

_WORKFLOW_DIR = os.path.join(os.path.dirname(__file__), "workflows")
_workflow_cache: dict[str, dict] = {}


def _load_workflow(name: str) -> dict:
    """Load a workflow JSON and cache it. Returns a deep copy."""
    if name not in _workflow_cache:
        path = os.path.join(_WORKFLOW_DIR, name)
        with open(path, "r") as f:
            _workflow_cache[name] = json.load(f)
    return copy.deepcopy(_workflow_cache[name])


class MediaHandler:
    """Handles media generation requests by driving ComfyUI."""

    def __init__(self, comfyui_url: str = "http://localhost:8188/api"):
        self.comfyui_url = comfyui_url
        self._temp_base = tempfile.mkdtemp(prefix="p2p_media_")

    def handle_request(self, endpoint_path: str, body: dict):
        handlers = {
            "media/image/generate": self._handle_image_generation,
            "media/image/edit": self._handle_image_editing,
            "media/video/i2v": self._handle_i2v,
        }
        handler = handlers.get(endpoint_path)
        if not handler:
            yield f'data: {json.dumps({"error": f"Unknown media endpoint: {endpoint_path}"})}'
            return
        try:
            yield from handler(body)
        except Exception as exc:
            logger.error(f"Media handler error for {endpoint_path}: {exc}")
            yield f'data: {json.dumps({"error": str(exc)})}'

    # ------------------------------------------------------------------
    # Image Generation
    # ------------------------------------------------------------------

    def _handle_image_generation(self, body: dict):
        prompt_text = body.get("prompt", "")
        width = body.get("width", 720)
        height = body.get("height", 720)
        steps = body.get("steps", 4)
        self._ensure_comfyui_running()
        workflow = self._build_image_gen_workflow(prompt_text, width, height, steps)
        yield from self._submit_and_track(workflow, "image/png", "output_image")

    # ------------------------------------------------------------------
    # Image Editing
    # ------------------------------------------------------------------

    def _handle_image_editing(self, body: dict):
        prompt_text = body.get("prompt", "")
        steps = body.get("steps", 4)
        input_images = body.get("input_images", [])
        if not input_images:
            yield f'data: {json.dumps({"error": "No input images provided"})}'
            return
        work_dir = os.path.join(self._temp_base, str(uuid.uuid4()))
        os.makedirs(work_dir, exist_ok=True)
        saved_paths = []
        try:
            for img_info in input_images:
                fname = img_info.get("filename", f"input_{len(saved_paths)}.png")
                content = base64.b64decode(img_info.get("content_base64", ""))
                path = os.path.join(work_dir, fname)
                with open(path, "wb") as f:
                    f.write(content)
                saved_paths.append(path)
            self._ensure_comfyui_running()
            workflow = self._build_image_edit_workflow(prompt_text, saved_paths, steps)
            yield from self._submit_and_track(workflow, "image/png", "output_image")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Image-to-Video
    # ------------------------------------------------------------------

    def _handle_i2v(self, body: dict):
        prompt_text = body.get("prompt", "")
        duration = body.get("duration", "5s")
        aspect_ratio = body.get("aspect_ratio", "2:3")
        input_image = body.get("input_image", {})
        if not input_image:
            yield f'data: {json.dumps({"error": "No input image provided"})}'
            return
        work_dir = os.path.join(self._temp_base, str(uuid.uuid4()))
        os.makedirs(work_dir, exist_ok=True)
        try:
            fname = input_image.get("filename", "input.png")
            content = base64.b64decode(input_image.get("content_base64", ""))
            image_path = os.path.join(work_dir, fname)
            with open(image_path, "wb") as f:
                f.write(content)
            self._ensure_comfyui_running()
            workflow = self._build_i2v_workflow(
                prompt_text, image_path, duration, aspect_ratio
            )
            yield from self._submit_and_track(workflow, "video/mp4", "output_video")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # ComfyUI interaction
    # ------------------------------------------------------------------

    def _ensure_comfyui_running(self):
        """Verify ComfyUI is running, start it if not.

        --- vendored edit 1: desktop's POST /comfyui/start (to its :8888
        helper) replaced with a direct call into engine.comfyui.ensure_running.
        ---
        """
        try:
            resp = httpx.get(f"{self.comfyui_url}/system_stats", timeout=5)
            if resp.status_code == 200:
                return
        except Exception:
            pass
        # Lazy import: engine.comfyui depends on filesystem layout that
        # only matters when media is actually enabled.
        from shared.engine import comfyui as comfyui_engine
        comfyui_engine.ensure_running(comfyui_url=self.comfyui_url)

    def _submit_workflow(self, workflow: dict) -> str:
        for attempt in range(_COMFYUI_MAX_RETRIES):
            try:
                resp = httpx.post(
                    f"{self.comfyui_url}/prompt", json=workflow, timeout=30
                )
                if resp.status_code == 200:
                    data = resp.json()
                    error = data.get("error")
                    if error:
                        raise RuntimeError(f"ComfyUI workflow error: {error}")
                    return data["prompt_id"]
            except httpx.ConnectError:
                logger.warning(f"ComfyUI connect error, retry {attempt + 1}")
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"ComfyUI submit error: {e}, retry {attempt + 1}")
            time.sleep(_COMFYUI_RETRY_INTERVAL)
        raise RuntimeError(f"Failed to submit workflow after {_COMFYUI_MAX_RETRIES} attempts")

    def _submit_and_track(self, workflow: dict, media_type: str, filename_prefix: str):
        prompt_id = self._submit_workflow(workflow)
        total_steps = self._count_sampler_steps(workflow)
        yield f'data: {json.dumps({"type": "progress", "progress": 0.0, "status": "running"})}'
        done_event = threading.Event()
        progress_events: list = []

        def _ws_listener():
            completed_node_steps = 0
            last_node = None
            last_max = 0
            ws_url = self.comfyui_url.replace("http://", "ws://").replace("/api", "/ws")
            try:
                import websocket
                ws = websocket.create_connection(ws_url, timeout=600)
                logger.info(f"ComfyUI WebSocket connected: {ws_url}")
            except Exception as e:
                logger.warning(f"ComfyUI WebSocket failed ({ws_url}): {e}; falling back to HTTP polling")
                return
            try:
                while not done_event.is_set():
                    try:
                        result = ws.recv()
                    except Exception:
                        break
                    if isinstance(result, bytes):
                        continue
                    try:
                        msg = json.loads(result)
                    except json.JSONDecodeError:
                        continue
                    msg_type = msg.get("type")
                    data = msg.get("data", {})
                    if data.get("prompt_id") != prompt_id:
                        continue
                    if msg_type == "progress":
                        node = data.get("node")
                        value = data.get("value", 0)
                        max_val = data.get("max", 0)
                        if node != last_node and last_node is not None:
                            completed_node_steps += last_max
                        last_node = node
                        last_max = max_val
                        if total_steps > 0:
                            pct = min((completed_node_steps + value) / total_steps * 95.0, 95.0)
                        else:
                            pct = 0.0
                        logger.info(f"ComfyUI progress: {pct:.1f}% (step {value}/{max_val}, total_steps={total_steps})")
                        progress_events.append(pct)
                    elif msg_type == "executing" and data.get("node") is None:
                        logger.info("ComfyUI execution complete")
                        progress_events.append(100.0)
                        done_event.set()
                        break
                    elif msg_type == "execution_error":
                        logger.error(f"ComfyUI execution error: {data}")
                        done_event.set()
                        break
            finally:
                ws.close()

        def _fallback_poll():
            while not done_event.is_set():
                try:
                    resp = httpx.get(f"{self.comfyui_url}/history/{prompt_id}", timeout=10)
                    history = resp.json()
                    if prompt_id in history:
                        progress_events.append(100.0)
                        done_event.set()
                        return
                except Exception:
                    pass
                time.sleep(2)

        ws_thread = threading.Thread(target=_ws_listener, daemon=True)
        poll_thread = threading.Thread(target=_fallback_poll, daemon=True)
        ws_thread.start()
        poll_thread.start()

        last_reported = 0.0
        while not done_event.is_set():
            done_event.wait(timeout=2.0)
            if progress_events:
                while progress_events:
                    pct = progress_events.pop(0)
                    if pct > last_reported:
                        last_reported = pct
                        yield f'data: {json.dumps({"type": "progress", "progress": round(pct, 1), "status": "running"})}'
            else:
                yield ": keepalive"

        while progress_events:
            pct = progress_events.pop(0)
            if pct > last_reported:
                last_reported = pct

        output_dir = self._get_output_dir()
        output_files = self._collect_output_files(output_dir, media_type, filename_prefix)
        if not output_files:
            yield f'data: {json.dumps({"error": "No output files produced by ComfyUI"})}'
            return
        yield f'data: {json.dumps({"type": "result", "output_files": output_files})}'
        yield "data: [DONE]"
        self._cleanup_output_dir(output_dir)

    def _get_output_dir(self) -> str:
        """Locate ComfyUI's output directory.

        --- vendored edit 2: fallback list rooted at ~/.grid/. ---
        """
        try:
            resp = httpx.get(f"{self.comfyui_url}/system_stats", timeout=5)
            if resp.status_code == 200:
                argv = resp.json().get("system", {}).get("argv", [])
                for i, arg in enumerate(argv):
                    if arg == "--output-directory" and i + 1 < len(argv):
                        output_dir = argv[i + 1]
                        if os.path.isdir(output_dir):
                            return output_dir
        except Exception:
            pass
        candidates = [
            str(paths.home() / "public" / "temp_comfy_output"),
            str(paths.home() / "services" / "ComfyUI" / "output"),
        ]
        for path in candidates:
            if os.path.isdir(path):
                return path
        return candidates[0]

    def _collect_output_files(self, output_dir: str, media_type: str,
                              filename_prefix: str) -> list[dict]:
        if not os.path.isdir(output_dir):
            return []
        files = []
        for fname in os.listdir(output_dir):
            if not fname.startswith(filename_prefix):
                continue
            fpath = os.path.join(output_dir, fname)
            if not os.path.isfile(fpath):
                continue
            with open(fpath, "rb") as f:
                content = f.read()
            files.append({
                "filename": fname,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "media_type": media_type,
            })
        return files

    def _cleanup_output_dir(self, output_dir: str):
        if not os.path.isdir(output_dir):
            return
        for fname in os.listdir(output_dir):
            fpath = os.path.join(output_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.unlink(fpath)
            except Exception as e:
                logger.warning(f"Failed to clean up {fpath}: {e}")

    @staticmethod
    def _count_sampler_steps(workflow: dict) -> int:
        prompt = workflow.get("prompt", {})
        total = 0
        for node in prompt.values():
            class_type = node.get("class_type", "")
            inputs = node.get("inputs", {})
            if class_type == "KSampler":
                total += inputs.get("steps", 0)
            elif class_type == "KSamplerAdvanced":
                total += inputs.get("end_at_step", 0) - inputs.get("start_at_step", 0)
        return total

    # ------------------------------------------------------------------
    # Workflow builders
    # ------------------------------------------------------------------

    def _build_image_gen_workflow(self, prompt: str, width: int, height: int,
                                  steps: int) -> dict:
        workflow = _load_workflow("image_generation_v2_workflow.json")
        workflow["prompt"]["34:27"]["inputs"]["text"] = prompt
        workflow["prompt"]["34:13"]["inputs"]["width"] = width
        workflow["prompt"]["34:13"]["inputs"]["height"] = height
        workflow["prompt"]["34:3"]["inputs"]["steps"] = steps
        workflow["prompt"]["9"]["inputs"]["filename_prefix"] = "output_image"
        return workflow

    def _build_image_edit_workflow(self, prompt: str, image_paths: list[str],
                                   steps: int) -> dict:
        num_images = len(image_paths)
        if num_images == 1:
            workflow = _load_workflow("one_image_editing_workflow.json")
            workflow["prompt"]["111"]["inputs"]["prompt"] = prompt
            workflow["prompt"]["3"]["inputs"]["steps"] = steps
            workflow["prompt"]["78"]["inputs"]["image"] = image_paths[0]
            workflow["prompt"]["60"]["inputs"]["filename_prefix"] = "output_image"
        elif num_images == 2:
            workflow = _load_workflow("two_images_editing_workflow.json")
            workflow["prompt"]["111"]["inputs"]["prompt"] = prompt
            workflow["prompt"]["3"]["inputs"]["steps"] = steps
            workflow["prompt"]["78"]["inputs"]["image"] = image_paths[0]
            workflow["prompt"]["106"]["inputs"]["image"] = image_paths[1]
            workflow["prompt"]["60"]["inputs"]["filename_prefix"] = "output_image"
        else:
            workflow = _load_workflow("three_images_editing_workflow.json")
            workflow["prompt"]["111"]["inputs"]["prompt"] = prompt
            workflow["prompt"]["3"]["inputs"]["steps"] = steps
            workflow["prompt"]["78"]["inputs"]["image"] = image_paths[0]
            workflow["prompt"]["106"]["inputs"]["image"] = image_paths[1]
            workflow["prompt"]["108"]["inputs"]["image"] = image_paths[2]
            workflow["prompt"]["60"]["inputs"]["filename_prefix"] = "output_image"
        return workflow

    def _build_i2v_workflow(self, prompt: str, image_path: str, duration: str,
                             aspect_ratio: str) -> dict:
        length_map = {"5s": 81, "8s": 129}
        ratio_map = {
            "2:3": {"width": 320, "height": 480},
            "3:2": {"width": 480, "height": 320},
            "1:1": {"width": 320, "height": 320},
        }
        workflow = _load_workflow("i2v_workflow.json")
        workflow["prompt"]["93"]["inputs"]["text"] = prompt
        workflow["prompt"]["97"]["inputs"]["image"] = image_path
        workflow["prompt"]["108"]["inputs"]["filename_prefix"] = "output_video"
        if "98" in workflow["prompt"]:
            node_98 = workflow["prompt"]["98"]["inputs"]
            node_98["length"] = length_map.get(duration, 81)
            dims = ratio_map.get(aspect_ratio, {"width": 320, "height": 480})
            node_98["width"] = dims["width"]
            node_98["height"] = dims["height"]
        return workflow
