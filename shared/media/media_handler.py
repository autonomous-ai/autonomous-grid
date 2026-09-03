"""Provider-side media handler for executing ComfyUI workflows.

VENDORED from Interns-Desktop-App/assets/scripts/additional_services_manager/media_handler.py.

Three explicit edits, each bracketed by `# --- vendored edit N: ... ---`:
  1. `_ensure_comfyui_running` - replace the desktop's HTTP POST to
     :8888 /comfyui/start (the desktop's FastAPI manager that this CLI does
     not run) with a direct call to engine.comfyui.ensure_running().
  2. `_get_output_dir` - replace the macOS / dev-only fallback paths with
     Grid paths rooted at ~/.grid/.
  3. `_stage_input_dir` - stage uploaded images under ComfyUI's own input/
     directory and reference them relatively, instead of writing them to an
     arbitrary temp dir and passing absolute paths.

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

# The generation models `media/image/generate` can serve, and where each graph keeps the four
# things a request sets. The ids are per-workflow — Krea 2 numbers its nodes 1..10, the Z-Image
# graph carries "34:*" subgraph ids — so they belong in a table beside the file name, not inlined
# at the call site where one model's ids would silently be used on the other's graph.
#
# Keys are the advertised capability names from `media_bundles.CAPABILITY_NAME`. A host serves
# whichever it joined with; a request names one, or gets `_DEFAULT_IMAGE_GEN`.
_DEFAULT_IMAGE_GEN = "comfyui:image_generation"

_KREA2_GRAPH = {
    "file": "image_generation_krea2_workflow.json",
    "text": "4", "size": "6", "steps": "7", "save": "10",
}

_IMAGE_GEN_WORKFLOWS: dict[str, dict[str, str]] = {
    # `comfyui:image_generation` (the task) and `comfyui:krea2` (the model) are the same graph —
    # see the note on GATES in media_gating.py for why both names exist.
    "comfyui:image_generation": _KREA2_GRAPH,
    "comfyui:krea2": _KREA2_GRAPH,
    "comfyui:z_image": {
        "file": "image_generation_zimage_workflow.json",
        "text": "34:27", "size": "34:13", "steps": "34:3", "save": "9",
    },
}


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

    # --- vendored edit 3: stage inputs inside ComfyUI's input/ tree. ---
    def _stage_input_dir(self) -> tuple[str, str]:
        """Make a per-request staging dir inside ComfyUI's own input/ directory.

        Returns ``(absolute_dir, token)``; workflows must name staged files as
        ``f"{token}/{filename}"``, i.e. relative to ComfyUI's input directory.

        The desktop wrote uploads to an arbitrary temp dir and handed LoadImage an
        absolute path. That only ever worked by accident: LoadImage validates via
        `folder_paths.exists_annotated_filepath`, which did
        `os.path.join(input_dir, name)` — and join silently discards the base when
        `name` is absolute. ComfyUI has since added a path-traversal guard that
        resolves the path and rejects anything outside input/, so absolute paths now
        fail validation and every edit/i2v request dies in `_submit_workflow`'s retry
        loop. Staging under input/ works on both old and new ComfyUI.
        """
        from shared.engine import comfyui as comfyui_engine

        token = str(uuid.uuid4())
        work_dir = os.path.join(str(comfyui_engine.comfyui_dir()), "input", token)
        os.makedirs(work_dir, exist_ok=True)
        return work_dir, token

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
        # No default here: each generation workflow already carries the step count its model was
        # distilled for (4 for both of ours), and forcing a number from this side would override a
        # graph that knows better the moment one of them differs. Only an explicit request wins.
        steps = body.get("steps")
        self._ensure_comfyui_running()
        # `model` is optional: the route already names the task, so omitting it keeps the
        # behaviour every existing caller has. Naming one picks between the generation models
        # this route serves — the proxy has already resolved the request to an engine that
        # advertises it, so anything unknown here just falls back to the default.
        workflow = self._build_image_gen_workflow(
            prompt_text, width, height, steps, model=body.get("model")
        )
        yield from self._submit_and_track(workflow, "image/png", "output_image")

    # ------------------------------------------------------------------
    # Image Editing
    # ------------------------------------------------------------------

    def _handle_image_editing(self, body: dict):
        prompt_text = body.get("prompt", "")
        steps = body.get("steps")  # absent -> the graph keeps its own count
        input_images = body.get("input_images", [])
        if not input_images:
            yield f'data: {json.dumps({"error": "No input images provided"})}'
            return
        work_dir, token = self._stage_input_dir()
        saved_paths = []
        try:
            for img_info in input_images:
                fname = img_info.get("filename", f"input_{len(saved_paths)}.png")
                content = base64.b64decode(img_info.get("content_base64", ""))
                with open(os.path.join(work_dir, fname), "wb") as f:
                    f.write(content)
                # relative to ComfyUI's input dir — see _stage_input_dir
                saved_paths.append(f"{token}/{fname}")
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
        work_dir, token = self._stage_input_dir()
        try:
            fname = input_image.get("filename", "input.png")
            content = base64.b64decode(input_image.get("content_base64", ""))
            with open(os.path.join(work_dir, fname), "wb") as f:
                f.write(content)
            self._ensure_comfyui_running()
            # relative to ComfyUI's input dir — see _stage_input_dir
            workflow = self._build_i2v_workflow(
                prompt_text, f"{token}/{fname}", duration, aspect_ratio
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
            ws_url = self._websocket_url(self.comfyui_url)
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
        output_files, collected = self._collect_output_files(
            output_dir, media_type, filename_prefix, prompt_id
        )
        if not output_files:
            yield f'data: {json.dumps({"error": "No output files produced by ComfyUI"})}'
            return
        yield f'data: {json.dumps({"type": "result", "output_files": output_files})}'
        yield "data: [DONE]"
        self._cleanup_files(collected)

    @staticmethod
    def _websocket_url(comfyui_url: str) -> str:
        base = comfyui_url.rstrip("/")
        if base.endswith("/api"):
            base = base[:-4]
        if base.startswith("https://"):
            base = f"wss://{base[8:]}"
        elif base.startswith("http://"):
            base = f"ws://{base[7:]}"
        return f"{base}/ws"

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
            str(paths.home() / "services" / "ComfyUI" / "output"),
            str(paths.home() / "public" / "temp_comfy_output"),
        ]
        for path in candidates:
            if os.path.isdir(path):
                return path
        return candidates[0]

    def _history_output_names(self, prompt_id: str) -> list[tuple[str, str]]:
        """`(subfolder, filename)` for exactly the files this prompt produced.

        ComfyUI's history records what each run wrote, which is the only way to tell
        one request's outputs from another's — the shared output directory cannot.
        """
        try:
            resp = httpx.get(f"{self.comfyui_url}/history/{prompt_id}", timeout=10)
            entry = resp.json().get(prompt_id) or {}
        except Exception as e:
            logger.warning(f"Could not read history for {prompt_id}: {e}")
            return []
        names: list[tuple[str, str]] = []
        for node_output in (entry.get("outputs") or {}).values():
            for value in node_output.values():
                if not isinstance(value, list):
                    continue
                for item in value:
                    if isinstance(item, dict) and item.get("filename"):
                        names.append((item.get("subfolder") or "", item["filename"]))
        return names

    def _collect_output_files(self, output_dir: str, media_type: str,
                              filename_prefix: str,
                              prompt_id: str | None = None) -> tuple[list[dict], list[str]]:
        """Return `(sse_payload_files, absolute_paths_collected)`.

        Prefer the filenames ComfyUI recorded for `prompt_id`. Falling back to scanning
        the directory by prefix is only safe when a run is alone in it: the output
        directory is shared, so a crashed run's leftovers — or a request running
        concurrently — would otherwise be returned as part of this response.
        """
        if not os.path.isdir(output_dir):
            return [], []

        candidates: list[tuple[str, str]] = []
        if prompt_id:
            candidates = [
                (os.path.join(output_dir, sub, name), name)
                for sub, name in self._history_output_names(prompt_id)
            ]
        if not candidates:
            if prompt_id:
                logger.warning(
                    f"History listed no outputs for {prompt_id}; falling back to a "
                    f"prefix scan of {output_dir}"
                )
            candidates = [
                (os.path.join(output_dir, name), name)
                for name in sorted(os.listdir(output_dir))
                if name.startswith(filename_prefix)
            ]

        files, paths_ = [], []
        for fpath, fname in candidates:
            if not os.path.isfile(fpath):
                continue
            with open(fpath, "rb") as f:
                content = f.read()
            files.append({
                "filename": fname,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "media_type": media_type,
            })
            paths_.append(fpath)
        return files, paths_

    def _cleanup_files(self, paths_: list[str]):
        """Delete only what this request returned, never the whole shared directory."""
        for fpath in paths_:
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
                                  steps: int | None = None, model: str | None = None) -> dict:
        """Text-to-image, on whichever generation model the request named.

        Both models serve the one `media/image/generate` route, so the node ids cannot be
        hard-coded here: each graph numbers its own nodes, and the Z-Image one uses "34:*"
        subgraph ids. An unknown (or absent) model falls back to the route's default.
        """
        spec = _IMAGE_GEN_WORKFLOWS.get(model or "") or _IMAGE_GEN_WORKFLOWS[_DEFAULT_IMAGE_GEN]
        workflow = _load_workflow(spec["file"])
        nodes = workflow["prompt"]
        nodes[spec["text"]]["inputs"]["text"] = prompt
        nodes[spec["size"]]["inputs"]["width"] = width
        nodes[spec["size"]]["inputs"]["height"] = height
        if steps is not None:
            nodes[spec["steps"]]["inputs"]["steps"] = steps
        nodes[spec["save"]]["inputs"]["filename_prefix"] = "output_image"
        return workflow

    def _build_image_edit_workflow(self, prompt: str, image_paths: list[str],
                                   steps: int | None = None) -> dict:
        num_images = len(image_paths)
        if num_images == 1:
            workflow = _load_workflow("one_image_editing_workflow.json")
            workflow["prompt"]["78"]["inputs"]["image"] = image_paths[0]
        elif num_images == 2:
            workflow = _load_workflow("two_images_editing_workflow.json")
            workflow["prompt"]["78"]["inputs"]["image"] = image_paths[0]
            workflow["prompt"]["106"]["inputs"]["image"] = image_paths[1]
        else:
            workflow = _load_workflow("three_images_editing_workflow.json")
            workflow["prompt"]["78"]["inputs"]["image"] = image_paths[0]
            workflow["prompt"]["106"]["inputs"]["image"] = image_paths[1]
            workflow["prompt"]["108"]["inputs"]["image"] = image_paths[2]
        # The three graphs differ only in how many images they take; the prompt, sampler and save
        # nodes are the same ids in all of them, so setting them once is the same work without the
        # chance of the branches drifting apart.
        workflow["prompt"]["111"]["inputs"]["prompt"] = prompt
        workflow["prompt"]["60"]["inputs"]["filename_prefix"] = "output_image"
        if steps is not None:  # otherwise the graph's own count stands — see _build_image_gen_workflow
            workflow["prompt"]["3"]["inputs"]["steps"] = steps
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
