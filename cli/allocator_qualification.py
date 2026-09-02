"""Physical runtime lifecycle qualification command."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from shared import paths
from shared.allocator.orchestrator import ComfyUIBackend, OllamaBackend, VllmBackend
from shared.allocator.qualification import qualify_runtime, save_report
from shared.media.media_handler import MediaHandler


def cmd_allocator_qualify(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65_535:
        raise SystemExit("--port must be in [1, 65535]")
    if args.timeout <= 0 or args.artifact_size_mb < 0:
        raise SystemExit("--timeout must be positive and --artifact-size-mb must be non-negative")
    if args.tensor_parallel_size < 1 or args.max_tokens < 1:
        raise SystemExit("tensor parallel size and max tokens must be positive")
    if args.image_size < 64 or args.steps < 1:
        raise SystemExit("ComfyUI image size must be at least 64 and steps must be positive")
    backend = _backend(args)
    report = qualify_runtime(
        runtime=args.runtime,
        backend=backend,
        model_id=args.model,
        port=args.port,
        inference=_inference(args),
        artifact_source=args.artifact_source,
        artifact_sha256=args.artifact_sha256,
        artifact_size_mb=args.artifact_size_mb,
        cleanup_artifact=args.cleanup_artifact,
    )
    report_path = _report_path(args, report.started_at)
    save_report(report_path, report)
    if args.json:
        print(json.dumps({**report.to_dict(), "report_path": str(report_path)}, indent=2))
    else:
        verdict = "PASS" if report.passed else "FAIL"
        print(f"Runtime qualification {verdict}: {report.runtime} · {report.model_id}")
        for item in report.steps:
            marker = "✓" if item.passed else "✗"
            print(f"  {marker} {item.name:<20} {item.duration_seconds:7.2f}s  {item.detail}")
        print(f"  report  {report_path}")
    return 0 if report.passed else 1


def _backend(args: argparse.Namespace) -> Any:
    endpoint = _endpoint(args)
    if args.runtime == "ollama":
        return OllamaBackend(endpoint, timeout=args.timeout)
    if args.runtime == "comfyui":
        bundle = args.model.removeprefix("comfyui:")
        return ComfyUIBackend(endpoint, bundles=(bundle,))
    cache = (
        Path(args.cache_dir).expanduser()
        if args.cache_dir
        else paths.grid_home() / "allocator-qualification" / "vllm"
    )
    return VllmBackend(
        cache,
        tensor_parallel_size=args.tensor_parallel_size,
        readiness_timeout=args.timeout,
    )


def _inference(args: argparse.Namespace):
    endpoint = _endpoint(args)
    if args.runtime == "ollama":

        def ollama(_handle):
            with httpx.Client(timeout=args.timeout, trust_env=False) as client:
                response = client.post(
                    f"{endpoint}/api/generate",
                    json={
                        "model": args.model,
                        "prompt": args.prompt,
                        "stream": False,
                        "keep_alive": -1,
                    },
                )
            response.raise_for_status()
            text = str(response.json().get("response") or "")
            return len(text), "Ollama /api/generate"

        return ollama
    if args.runtime == "vllm":

        def vllm(handle):
            with httpx.Client(timeout=args.timeout, trust_env=False) as client:
                response = client.post(
                    f"http://127.0.0.1:{handle.port}/v1/chat/completions",
                    json={
                        "model": args.model,
                        "messages": [{"role": "user", "content": args.prompt}],
                        "max_tokens": args.max_tokens,
                    },
                )
            response.raise_for_status()
            text = str(response.json()["choices"][0]["message"]["content"])
            return len(text), "vLLM OpenAI-compatible chat"

        return vllm

    def comfyui(_handle):
        if args.model not in (
            "comfyui:image_generation",
            "comfyui:krea2",
            "comfyui:z_image",
        ):
            raise RuntimeError(
                "physical ComfyUI qualification currently requires an image-generation bundle"
            )
        handler = MediaHandler(comfyui_url=endpoint)
        output_units = 0
        for line in handler.handle_request(
            "media/image/generate",
            {
                "model": args.model,
                "prompt": args.prompt,
                "width": args.image_size,
                "height": args.image_size,
                "steps": args.steps,
            },
        ):
            terminal = str(line)
            if '"error"' in terminal:
                raise RuntimeError(terminal[:500])
            output_units += len(terminal)
        return output_units, "ComfyUI completed workflow output"

    return comfyui


def _endpoint(args: argparse.Namespace) -> str:
    if args.endpoint:
        return str(args.endpoint).rstrip("/")
    return "http://127.0.0.1:11434" if args.runtime == "ollama" else "http://127.0.0.1:8188"


def _report_path(args: argparse.Namespace, started_at: float) -> Path:
    if args.report:
        return Path(args.report).expanduser()
    scope = hashlib.sha256(f"{args.runtime}\0{args.model}".encode()).hexdigest()[:16]
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(started_at))
    return paths.grid_home() / "allocator-qualification" / "reports" / f"{stamp}-{scope}.json"
