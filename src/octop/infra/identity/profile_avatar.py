"""Decode retained profile inputs into a single metadata-free WebP raster."""

from __future__ import annotations

import base64
import binascii
import io
import math
import re

from PIL import Image, ImageOps, UnidentifiedImageError

from octop.infra.errors import ErrorCode, MossworkError

_MAX_BYTES = 2 * 1024 * 1024
_FORMATS = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}


def normalize_profile_avatar(value: object) -> str | None:
    """Preserve the original 2MiB/16M-pixel/single-frame/256-square contract.

    Uses the already required Pillow dependency. Encoded output is not promised
    to be byte-identical to the original sharp/libvips encoder. No filenames,
    remote URLs, original metadata or user-selected output format are accepted.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)
    if len(value) > math.ceil(_MAX_BYTES / 3) * 4 + 64:
        raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=413)
    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})", value)
    if match is None:
        raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400)
    try:
        # Buffer.from(base64) accepts the ordinary unpadded representation too.
        raw = base64.b64decode(match[2] + "=" * (-len(match[2]) % 4), validate=True)
        if len(raw) > _MAX_BYTES:
            raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=413)
        with Image.open(io.BytesIO(raw), formats=list(_FORMATS.values())) as source:
            if (
                source.format != _FORMATS[match[1]]
                or source.width * source.height > 16_000_000
                or getattr(source, "n_frames", 1) != 1
            ):
                raise ValueError("Unsupported avatar raster")
            source.verify()
        with Image.open(io.BytesIO(raw), formats=list(_FORMATS.values())) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
            image = image.convert(mode)
            image = ImageOps.fit(image, (256, 256), method=Image.Resampling.LANCZOS)
            # A new image carries no EXIF/ICC/XMP from the upload.
            clean = Image.new(mode, image.size)
            clean.paste(image)
            output = io.BytesIO()
            clean.save(output, format="WEBP", quality=85)
        return "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except MossworkError:
        raise
    except (
        ValueError,
        OSError,
        SyntaxError,
        binascii.Error,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ):
        raise MossworkError.localized(ErrorCode.IDENTITY_REQUEST_INVALID, status=400) from None
