"""Process an uploaded image: validate, downscale, and re-encode as JPEG — which also
strips all EXIF/metadata (privacy: removes embedded GPS/camera data). Returns (filename, mime)
or None if the bytes aren't a usable image. Also owns media-directory accounting (the global
disk cap) and safe deletion (operator photo takedown)."""
import io
import os
import time
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from .config import settings

ALLOWED = {"image/jpeg", "image/png", "image/webp"}
MAX_DIM = 1600
# Decompression-bomb guard. The output is downscaled to MAX_DIM (~2.6 Mpix), so the source never
# needs to be large; cap it well below the prod container's 512 MB limit. A 24 Mpix RGB buffer is
# ~72 MB, versus ~150 MB at the old 50 Mpix ceiling. PIL raises DecompressionBombError above this,
# and we ALSO check the header dimensions before convert('RGB') so an oversized image is rejected
# before the full-resolution buffer is ever allocated.
MAX_SRC_PIXELS = 24_000_000
Image.MAX_IMAGE_PIXELS = MAX_SRC_PIXELS

# Brief TTL cache of the media-directory byte total, so a burst of uploads doesn't os.scandir the
# whole directory on every request. Invalidated on any save/unlink.
_MEDIA_SIZE_TTL = 30.0
_media_size = {"at": -1e9, "bytes": 0}


def media_total_bytes() -> int:
    """Sum of bytes stored directly under media_dir (files are flat: '<uuid>.jpg'). Cached briefly."""
    now = time.monotonic()
    if now - _media_size["at"] < _MEDIA_SIZE_TTL:
        return _media_size["bytes"]
    total = 0
    try:
        with os.scandir(settings.media_dir) as it:
            for entry in it:
                if entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        pass
    except FileNotFoundError:
        total = 0
    _media_size["at"], _media_size["bytes"] = now, total
    return total


def unlink_media(name: str) -> bool:
    """Delete a stored media file by its stored name (the `path` column). True if a file was
    removed; a missing file counts as already-gone (False). Refuses anything that isn't a bare
    filename, so a poisoned `path` can't escape media_dir."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return False
    try:
        (Path(settings.media_dir) / name).unlink()
        _media_size["at"] = -1e9  # invalidate the size cache
        return True
    except (FileNotFoundError, OSError):
        return False


def process_and_save(raw: bytes, content_type: str, name: str | None = None) -> tuple[str, str] | None:
    """Validate/downscale/re-encode `raw` and write it under media_dir. `name` may be supplied by the
    caller so it knows the target filename up front (the async upload handler predetermines it, so it
    can clean the file up even if the request is cancelled mid-encode); when None we mint one."""
    if content_type not in ALLOWED:
        return None
    try:
        Image.open(io.BytesIO(raw)).verify()           # validate structure
        img = Image.open(io.BytesIO(raw))              # reopen (verify consumes it)
        # Reject an oversized source from its header BEFORE convert('RGB') allocates the full-res
        # buffer — a highly-compressible bomb can be tiny on disk yet decode to a huge bitmap.
        if img.width * img.height > MAX_SRC_PIXELS:
            return None
        # Bake the camera's EXIF orientation into the PIXELS before we re-encode (which strips EXIF for
        # privacy). A portrait phone photo is stored as landscape pixels + an orientation flag; drop the
        # flag without applying it and the photo displays sideways. exif_transpose rotates it upright,
        # then the fresh JPEG save drops the now-redundant metadata.
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
    img.thumbnail((MAX_DIM, MAX_DIM))                   # downscale, keep aspect
    name = name or f"{uuid.uuid4().hex}.jpg"
    out_dir = Path(settings.media_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    img.save(out_dir / name, format="JPEG", quality=85, optimize=True)  # fresh JPEG => no EXIF
    _media_size["at"] = -1e9  # invalidate the size cache
    return name, "image/jpeg"
