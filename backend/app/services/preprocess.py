"""Image pre-processing utilities: resize, EXIF rotation fix, JPEG conversion."""
from __future__ import annotations
import io
import logging
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

MAX_DIMENSION = 1600  # px


def preprocess_image(image_bytes: bytes) -> bytes:
    """
    Take raw uploaded image bytes and return cleaned JPEG bytes:
    - Apply EXIF orientation correction.
    - Resize so the longest edge ≤ MAX_DIMENSION (preserving aspect ratio).
    - Convert to RGB JPEG.
    """
    img = Image.open(io.BytesIO(image_bytes))
    # Fix EXIF rotation
    img = ImageOps.exif_transpose(img)
    # Resize if needed
    img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
    # Ensure RGB (drop alpha channel if PNG)
    if img.mode != "RGB":
        img = img.convert("RGB")
    output = io.BytesIO()
    img.save(output, format="JPEG", quality=90)
    logger.debug("Preprocessed image: %dx%d", img.width, img.height)
    return output.getvalue()
