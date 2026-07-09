"""
ComfyUI custom nodes for the Doggi media-gen client.

Three nodes that just submit a generation request and wait for the result:
  - DoggiTextToImage   (t2i)
  - DoggiImageToImage  (i2i)
  - DoggiImageToVideo  (i2v)

Configure credentials either via the node inputs or via env vars:
    DOGGI_BASE_URL
    DOGGI_API_KEY
"""

import os
import io
import base64
import requests
import numpy as np
from PIL import Image

import folder_paths
from doggi import DoggiClient

import torch

# MIME types for the most common image formats; falls back to image/jpeg.
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

# Default models used by each node (matching test.py).
DEFAULT_T2I_MODEL = "hunyuan-image-3-t2i"
DEFAULT_I2I_MODEL = "hunyuan-image-3-i2i"
DEFAULT_I2V_MODEL = "Wan-AI/Wan2.2-I2V-A14B-Lightning"

DEFAULT_BASE_URL = os.environ.get("DOGGI_BASE_URL", "https://2x4090-9091.eternalai.org")
DEFAULT_API_KEY = os.environ.get("DOGGI_API_KEY", "YOUR_API_KEY")

# Aspect ratios (w/h) for the image_size enum accepted by t2i.
# square_hd and square are both 1:1; square_hd is the higher-res variant.
_IMAGE_SIZE_ASPECTS = {
    "square_hd": 1.0,
    "square": 1.0,
    "portrait_4_3": 3.0 / 4.0,    # 0.75
    "portrait_16_9": 9.0 / 16.0,  # 0.5625
    "landscape_4_3": 4.0 / 3.0,   # 1.333
    "landscape_16_9": 16.0 / 9.0, # 1.778
}


def _client(base_url, api_key):
    base_url = base_url or DEFAULT_BASE_URL
    api_key = api_key or DEFAULT_API_KEY
    return DoggiClient(base_url=base_url, api_key=api_key)


def _hw_to_image_size(h, w):
    """Convert (h, w) into the closest supported image_size enum. (0,0) -> auto."""
    if not w or not h:
        return "auto"
    target = float(w) / float(h)
    best, best_dist = "auto", None
    for name, ratio in _IMAGE_SIZE_ASPECTS.items():
        d = abs(ratio - target)
        if best_dist is None or d < best_dist:
            best, best_dist = name, d
    return best


def _path_to_b64_uri(path):
    """Read the file at `path` as raw bytes and wrap it in a data URI.

    No decoding / re-encoding / normalization — the file bytes go straight
    into base64, preserving the original format and dimensions.
    """
    path = path.strip()
    if not path:
        raise ValueError("image path is empty")
    with open(path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(path)[1].lower()
    mime = _MIME_BY_EXT.get(ext, "image/jpeg")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _paths_to_b64_uris(image_paths):
    """Split a comma/newline-separated list of paths into a list of data URIs."""
    paths = [p.strip() for p in image_paths.replace("\n", ",").split(",") if p.strip()]
    if not paths:
        raise ValueError("no image paths provided")
    return [_path_to_b64_uri(p) for p in paths]


def _url_to_image_tensor(url):
    """Download an image URL and return a ComfyUI IMAGE tensor [1,H,W,3] float 0-1."""
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content)).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr).unsqueeze(0)


def _download_video(url, prefix="doggi_i2v"):
    """Download the generated video into ComfyUI's output dir; return the filepath."""
    out_dir = folder_paths.get_output_directory()
    ext = os.path.splitext(url.split("?", 1)[0])[1] or ".mp4"
    # Counter to avoid collisions.
    i = 0
    while True:
        name = f"{prefix}_{i:05d}{ext}" if i else f"{prefix}{ext}"
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            break
        i += 1
    resp = requests.get(url, timeout=300, stream=True)
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if chunk:
                f.write(chunk)
    return path


class DoggiTextToImage:
    """Text-to-image via the Doggi API."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": DEFAULT_T2I_MODEL}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "w": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1}),
                "h": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1}),
            },
            "optional": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "api_key": ("STRING", {"default": DEFAULT_API_KEY}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "url")
    FUNCTION = "generate"
    CATEGORY = "Doggi"

    def generate(self, model, prompt, w, h,
                 base_url=DEFAULT_BASE_URL, api_key=DEFAULT_API_KEY):
        image_size = _hw_to_image_size(h, w)
        client = _client(base_url, api_key)
        receipt = client.t2i.submit(
            model,
            prompt=prompt,
            image_size=image_size,
        )
        result = receipt.wait()
        url = result["result_files"][0]["file_url"]
        image = _url_to_image_tensor(url)
        return (image, url)


class DoggiImageToImage:
    """Image-to-image via the Doggi API. Input images are converted to base64 data URIs."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": DEFAULT_I2I_MODEL}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "image_paths": ("STRING", {"multiline": True, "default": ""}),
                "aspect_ratio": (["9:16", "16:9", "4:3", "3:4", "1:1", "auto"], {"default": "9:16"}),
            },
            "optional": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "api_key": ("STRING", {"default": DEFAULT_API_KEY}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("image", "url")
    FUNCTION = "generate"
    CATEGORY = "Doggi"

    def generate(self, model, prompt, image_paths,
                 aspect_ratio="9:16",
                 base_url=DEFAULT_BASE_URL, api_key=DEFAULT_API_KEY):
        # image_paths is a comma/newline-separated list of file paths -> data URIs.
        uris = _paths_to_b64_uris(image_paths)

        client = _client(base_url, api_key)
        # The i2i endpoint takes a single image_url; pass the first data URI.
        receipt = client.i2i.submit(
            model,
            image_url=uris[0],
            prompt=prompt,
            aspect_ratio=aspect_ratio,
        )
        result = receipt.wait()
        url = result["result_files"][0]["file_url"]
        image = _url_to_image_tensor(url)
        return (image, url)


class DoggiImageToVideo:
    """Image-to-video via the Doggi API. Input image is converted to a base64 data URI."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("STRING", {"default": DEFAULT_I2V_MODEL}),
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "image_path": ("STRING", {"default": ""}),
                "duration": ("INT", {"default": 5, "min": 2, "max": 10, "step": 1}),
                "aspect_ratio": (["2:3", "3:2", "1:1"], {"default": "1:1"}),
            },
            "optional": {
                "base_url": ("STRING", {"default": DEFAULT_BASE_URL}),
                "api_key": ("STRING", {"default": DEFAULT_API_KEY}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("video_path", "url")
    FUNCTION = "generate"
    CATEGORY = "Doggi"

    def generate(self, model, prompt, image_path, duration, aspect_ratio,
                 base_url=DEFAULT_BASE_URL, api_key=DEFAULT_API_KEY):
        # image_path is a file path -> a single data URI (raw bytes, no transform).
        uri = _path_to_b64_uri(image_path)

        client = _client(base_url, api_key)
        receipt = client.i2v.submit(
            model,
            image_url=uri,
            prompt=prompt,
            duration=str(duration),
            aspect_ratio=aspect_ratio,
        )
        result = receipt.wait()
        url = result["result_files"][0]["file_url"]
        path = _download_video(url)
        return (path, url)


NODE_CLASS_MAPPINGS = {
    "DoggiTextToImage": DoggiTextToImage,
    "DoggiImageToImage": DoggiImageToImage,
    "DoggiImageToVideo": DoggiImageToVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "DoggiTextToImage": "Doggi Text-to-Image",
    "DoggiImageToImage": "Doggi Image-to-Image",
    "DoggiImageToVideo": "Doggi Image-to-Video",
}
