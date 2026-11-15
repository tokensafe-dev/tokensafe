"""
TokenSafe Vision Image Downscaler & Optimizer (v3.3.0).

Optimizes oversized screenshots and UI diagrams before LLM vision ingestion.
Downscales images exceeding max_edge (default: 1024px) while preserving aspect ratio,
text crispness, and leaving the original image file intact on disk.

Engines:
1. Pillow (PIL) - high-quality Lanczos resampling
2. macOS sips - zero-dependency built-in OS tool
3. ImageMagick (convert/magick) - headless Linux fallback
"""

import os
import shutil
import hashlib
import subprocess
from typing import Dict, Any, Tuple, Optional
from src.paths import resolve_data_dir

# Supported extensions
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def estimate_vision_tokens(width: int, height: int) -> int:
    """
    Estimates LLM vision token cost based on standard tile-based pricing
    (Anthropic Claude 3.5 Sonnet / OpenAI GPT-4o tile calculation).
    """
    if width <= 0 or height <= 0:
        return 0
    # Base tile calculation: 512x512 grid
    tiles_w = (width + 511) // 512
    tiles_h = (height + 511) // 512
    total_tiles = max(1, tiles_w * tiles_h)
    # 85 base tokens + 170 tokens per tile
    return 85 + (total_tiles * 170)


def _get_cache_dir() -> str:
    cache_dir = os.path.join(resolve_data_dir(), "cache", "images")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _read_dimensions_pillow(path: str) -> Optional[Tuple[int, int]]:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size  # (width, height)
    except Exception:
        return None


def _read_dimensions_sips(path: str) -> Optional[Tuple[int, int]]:
    try:
        res = subprocess.run(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            w, h = 0, 0
            for line in res.stdout.splitlines():
                if "pixelWidth:" in line:
                    w = int(line.split(":")[-1].strip())
                elif "pixelHeight:" in line:
                    h = int(line.split(":")[-1].strip())
            if w > 0 and h > 0:
                return (w, h)
    except Exception:
        pass
    return None


def get_image_dimensions(path: str) -> Optional[Tuple[int, int]]:
    dims = _read_dimensions_pillow(path)
    if dims:
        return dims
    if shutil.which("sips"):
        dims = _read_dimensions_sips(path)
        if dims:
            return dims
    return None


class ImageDownscaler:
    """Downscales images to save vision tokens without mutating original files."""

    @classmethod
    def optimize(
        cls,
        image_path: str,
        max_edge: int = 1024,
        cache_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Downscales an image if max(width, height) > max_edge.
        Returns metadata with optimized_path, saved_tokens, etc.
        """
        if not os.path.exists(image_path):
            return {
                "success": False,
                "error": f"Image file not found: {image_path}",
                "was_downscaled": False
            }

        ext = os.path.splitext(image_path)[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            return {
                "success": False,
                "error": f"Unsupported image format: {ext}",
                "was_downscaled": False
            }

        dims = get_image_dimensions(image_path)
        if not dims:
            return {
                "success": True,
                "was_downscaled": False,
                "optimized_path": image_path,
                "note": "Could not inspect dimensions; using original file"
            }

        w, h = dims
        orig_bytes = os.path.getsize(image_path)
        orig_tokens = estimate_vision_tokens(w, h)

        if max(w, h) <= max_edge:
            return {
                "success": True,
                "was_downscaled": False,
                "original_path": image_path,
                "optimized_path": image_path,
                "dimensions": (w, h),
                "bytes": orig_bytes,
                "estimated_tokens": orig_tokens,
                "saved_tokens": 0
            }

        # Compute new dimensions maintaining aspect ratio
        if w >= h:
            new_w = max_edge
            new_h = max(1, int(round(h * (max_edge / float(w)))))
        else:
            new_h = max_edge
            new_w = max(1, int(round(w * (max_edge / float(h)))))

        # Compute output cache path based on content hash and max_edge
        target_dir = cache_dir or _get_cache_dir()
        hasher = hashlib.sha256()
        with open(image_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        hasher.update(str(max_edge).encode("utf-8"))
        cache_key = hasher.hexdigest()[:16]
        optimized_path = os.path.join(target_dir, f"{cache_key}{ext}")

        engine_used = None

        # 1. Try Pillow
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                resample_filter = getattr(Image.Resampling, "LANCZOS", Image.LANCZOS if hasattr(Image, "LANCZOS") else 1)
                resized = img.resize((new_w, new_h), resample=resample_filter)
                resized.save(optimized_path)
                engine_used = "pillow"
        except Exception:
            engine_used = None

        # 2. Try macOS sips if Pillow failed or was unavailable
        if not engine_used and shutil.which("sips"):
            try:
                res = subprocess.run(
                    ["/usr/bin/sips", "-Z", str(max_edge), image_path, "--out", optimized_path],
                    capture_output=True, timeout=10
                )
                if res.returncode == 0 and os.path.exists(optimized_path):
                    engine_used = "sips"
            except Exception:
                engine_used = None

        # 3. Try ImageMagick
        if not engine_used:
            magick_cmd = shutil.which("magick") or shutil.which("convert")
            if magick_cmd:
                try:
                    res = subprocess.run(
                        [magick_cmd, image_path, "-resize", f"{max_edge}x{max_edge}>", optimized_path],
                        capture_output=True, timeout=10
                    )
                    if res.returncode == 0 and os.path.exists(optimized_path):
                        engine_used = "imagemagick"
                except Exception:
                    engine_used = None

        if not engine_used or not os.path.exists(optimized_path):
            # Graceful fallback: return original image
            return {
                "success": True,
                "was_downscaled": False,
                "original_path": image_path,
                "optimized_path": image_path,
                "dimensions": (w, h),
                "bytes": orig_bytes,
                "estimated_tokens": orig_tokens,
                "saved_tokens": 0,
                "note": "Downscaling engines unavailable; using original file"
            }

        new_bytes = os.path.getsize(optimized_path)
        new_tokens = estimate_vision_tokens(new_w, new_h)
        saved_tokens = max(0, orig_tokens - new_tokens)

        return {
            "success": True,
            "was_downscaled": True,
            "engine": engine_used,
            "original_path": image_path,
            "optimized_path": optimized_path,
            "orig_dimensions": (w, h),
            "new_dimensions": (new_w, new_h),
            "orig_bytes": orig_bytes,
            "new_bytes": new_bytes,
            "orig_tokens": orig_tokens,
            "new_tokens": new_tokens,
            "saved_tokens": saved_tokens,
        }
