"""Bounded JSON image uploads; translate the wire format to Strands content blocks."""

import base64
import binascii
import io
import warnings

from PIL import Image, UnidentifiedImageError
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_COUNT = 5
# Aggregate decoded image budget is also 10 MiB, plus text/JSON overhead on the wire.
MAX_CHAT_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 128 * 1024
IMAGE_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def decode_images(images):
    """Validate before inference, without storing image bytes in dashboard history."""
    if not isinstance(images, list) or len(images) > MAX_IMAGE_COUNT:
        raise BadRequest("images must be a list of at most 5 images")
    blocks = []
    total = 0
    for image in images:
        if not isinstance(image, dict) or image.get("type") != "image":
            raise BadRequest("Expected an image content block")
        source = image.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            raise BadRequest("Only base64 image sources are supported")
        mime = source.get("media_type")
        if not isinstance(mime, str) or mime not in IMAGE_FORMATS:
            raise BadRequest("Supported image formats: png, jpg, jpeg, gif, webp")
        encoded = source.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise BadRequest("Image data must be non-empty base64")
        if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise RequestEntityTooLarge("Images must be at most 10 MiB")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise BadRequest("Invalid base64 image data") from error
        total += len(raw)
        if total > MAX_IMAGE_BYTES:
            raise RequestEntityTooLarge("Images must total at most 10 MiB per message")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as opened:
                    if opened.format.lower() != IMAGE_FORMATS[mime]:
                        raise BadRequest("Image content does not match media_type")
                    opened.verify()
        except (
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
        ) as error:
            raise BadRequest("Invalid or unsafe image") from error
        # Strands/Bedrock uses bytes, NOT Anthropic's type/source base64 schema.
        blocks.append({"image": {"format": IMAGE_FORMATS[mime], "source": {"bytes": raw}}})
    return blocks
