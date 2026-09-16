"""Load user-provided reference images from a fixed folder.

Used by ``node_spec_planner`` (extract geometric features from user's visual
reference) and ``node_judge_qa`` (compare user's intended design vs current
rendered model). The folder ``user_input_images/`` (configurable via
``USER_IMAGES_DIR`` in config.py) is scanned alphabetically by filename —
deterministic across platforms / Git sync / cloud sync.

Empty folder or missing folder → empty list → caller falls back to text-only
path. No error raised in either case (that's the "no images" path).

Each image is resized to max ``USER_IMAGE_MAX_SIZE`` (preserves aspect ratio)
and re-encoded as JPEG quality=``USER_IMAGE_JPEG_QUALITY`` (uniform compression
regardless of input format — avoids huge PNGs inflating multimodal token cost).

Usage::

    from pathlib import Path
    from multi_agent_cad.image_preprocess import _load_user_images, _encode_jpeg_data_url
    imgs = _load_user_images(Path.cwd() / "user_input_images")
    if imgs:
        data_url = _encode_jpeg_data_url(imgs[0])
        # → "data:image/jpeg;base64,..."
"""
from __future__ import annotations

import base64
import io
from pathlib import Path


# Supported image file extensions (case-insensitive).
# PIL also handles BMP/GIF natively, but we restrict to common CAD-reference
# formats to avoid surprising the user.
_SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def _repo_root() -> Path:
    """Return the repository root (parent of the multi_agent_cad package).

    Used as fallback for finding ``user_input_images/`` when cwd doesn't have
    it — e.g. under Web UI, ``web_runner.py`` chdir's to per-job tempdir, so
    the user's shared image folder (kept at repo root) isn't visible from cwd.
    Scanning repo root too lets users keep one shared image folder regardless
    of CLI or Web UI mode.
    """
    try:
        import multi_agent_cad
        return Path(multi_agent_cad.__file__).resolve().parent.parent
    except Exception:
        return Path.cwd()


def _load_user_images(
    images_dir: Path,
    max_size: int = 1024,
    jpeg_quality: int = 85,
) -> list[bytes]:
    """Load all images from a folder, resized + JPEG-encoded.

    **Two-path scan**: scans ``images_dir`` first (cwd-relative), then falls
    back to ``<repo_root>/user_input_images/`` if cwd's folder is missing or
    empty. This lets users keep one shared image folder at the project root
    regardless of how they run the pipeline:

    - CLI mode: ``python -m multi_agent_cad.graph`` from repo root → cwd is
      repo root → ``user_input_images/`` found via cwd directly
    - Web UI mode: ``web_runner.py`` chdir's to per-job tempdir → cwd's
      ``user_input_images/`` is empty (or doesn't exist) → fallback scans
      repo root's ``user_input_images/`` and finds the user's images

    Images are de-duplicated by filename — if the same file exists in both
    paths, only the cwd-relative one is used (cwd takes precedence).

    Parameters
    ----------
    images_dir : Path
        Directory to scan first (typically ``Path.cwd() / "user_input_images"``).
        Missing or empty → falls back to repo root's ``user_input_images/``.
    max_size : int
        Maximum edge length (width or height). Larger images are downscaled
        preserving aspect ratio. Smaller images are NOT upscaled (no benefit,
        wastes tokens).
    jpeg_quality : int
        JPEG re-encode quality (1-95). Default 85 = good visual quality at
        ~50-150KB per 1024×1024 image.

    Returns
    -------
    list[bytes]
        List of JPEG-encoded image bytes, **sorted alphabetically by filename**
        (case-insensitive). Empty list if no images found in either path.

        Determinism note: alphabetical sort is chosen over mtime sort because
        mtime is fragile across platforms / Git sync / cloud sync — copy
        operations can stamp identical or near-identical mtimes, causing read
        order to flap unpredictably between runs. This would cause CADBrief to
        drift deterministically (Spec Planner might pick "front view" as
        ``user_image[0]`` on one run, "side view" on the next), seeding
        hallucinations downstream. Alphabetical sort locks the order per file
        set — user names files ``1.jpg``, ``2.jpg`` etc. and every run reads
        them in the same order.
    """
    # Build candidate scan paths: cwd-relative first, then repo-root fallback.
    candidates: list[Path] = []
    if images_dir:
        candidates.append(images_dir)
    repo_fallback = _repo_root() / (images_dir.name if images_dir else "user_input_images")
    if repo_fallback not in candidates:
        candidates.append(repo_fallback)

    # Collect supported image files from all candidate dirs, dedupe by name.
    seen_names: set[str] = set()
    image_files: list[Path] = []
    for d in candidates:
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if not p.is_file() or p.suffix.lower() not in _SUPPORTED_EXTENSIONS:
                continue
            # Dedupe by filename — cwd's version takes precedence (added first).
            name_key = p.name.lower()
            if name_key in seen_names:
                continue
            seen_names.add(name_key)
            image_files.append(p)

    # Alphabetical sort for deterministic load order across runs.
    image_files = sorted(image_files, key=lambda p: p.name.lower())

    if not image_files:
        return []

    try:
        from PIL import Image
    except ImportError as exc:
        print(f"[image_preprocess] PIL/Pillow not available: {exc}")
        return []

    images_bytes: list[bytes] = []
    for img_path in image_files:
        try:
            img = Image.open(img_path)
            # Convert to RGB (drop alpha channel / palette mode → JPEG-compatible)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Downscale if exceeds max_size (preserves aspect ratio).
            # Smaller images are NOT upscaled.
            w, h = img.size
            longest = max(w, h)
            if longest > max_size:
                scale = max_size / longest
                new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
                img = img.resize(new_size, Image.LANCZOS)

            # Re-encode as JPEG quality=jpeg_quality.
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)
            jpeg_bytes = buf.getvalue()
            images_bytes.append(jpeg_bytes)
        except Exception as exc:
            # Skip broken images but don't fail the whole batch.
            print(f"[image_preprocess] failed to load {img_path.name}: {exc}")
            continue

    return images_bytes


def _encode_jpeg_data_url(jpeg_bytes: bytes) -> str:
    """Base64-encode JPEG bytes and wrap as a data URL.

    Returns ``"data:image/jpeg;base64,<base64-encoded-bytes>"``.
    Note: ``jpeg`` (not ``png``) since image_preprocess always re-encodes to
    JPEG for uniform compression.
    """
    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"
