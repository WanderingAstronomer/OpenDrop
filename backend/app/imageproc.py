"""Process an uploaded image: validate, downscale, and re-encode as JPEG — which also
strips all EXIF/metadata (privacy: removes embedded GPS/camera data). Returns (filename, mime)
or None if the bytes aren't a usable image."""
import io
import uuid
from pathlib import Path

from PIL import Image

from .config import settings

ALLOWED = {"image/jpeg", "image/png", "image/webp"}
MAX_DIM = 1600
Image.MAX_IMAGE_PIXELS = 50_000_000  # decompression-bomb guard


def process_and_save(raw: bytes, content_type: str) -> tuple[str, str] | None:
    if content_type not in ALLOWED:
        return None
    try:
        Image.open(io.BytesIO(raw)).verify()           # validate structure
        img = Image.open(io.BytesIO(raw)).convert("RGB")  # reopen (verify consumes it)
    except Exception:  # noqa: BLE001
        return None
    img.thumbnail((MAX_DIM, MAX_DIM))                   # downscale, keep aspect
    name = f"{uuid.uuid4().hex}.jpg"
    out_dir = Path(settings.media_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    img.save(out_dir / name, format="JPEG", quality=85, optimize=True)  # fresh JPEG => no EXIF
    return name, "image/jpeg"
