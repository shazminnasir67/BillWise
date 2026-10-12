"""Validate an uploaded bill image by its magic bytes (never by the file name)."""
from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

MAX_BYTES = 10 * 1024 * 1024


def check_image(data: bytes) -> str:
    """Return the file extension for a valid PNG, JPEG or WEBP image of at most 10 MB; raise ValueError otherwise."""
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MAX_BYTES:
        raise ValueError("the image is larger than 10 MB")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = ".png"
    elif data.startswith(b"\xff\xd8\xff"):
        ext = ".jpg"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    else:
        raise ValueError("not a PNG, JPEG or WEBP image")
    try:
        Image.open(io.BytesIO(data)).verify()
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        raise ValueError("the image file is damaged") from e
    return ext
