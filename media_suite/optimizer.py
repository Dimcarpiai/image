"""Pure image processing. No Saleor knowledge here so it is easy to unit test."""
from io import BytesIO
from typing import Literal, Optional

from PIL import Image, ImageOps, features
from pydantic import BaseModel, Field

OutputFormat = Literal["webp", "avif", "jpeg", "png", "keep"]

PIL_FORMAT = {"webp": "WEBP", "avif": "AVIF", "jpeg": "JPEG", "png": "PNG"}
CONTENT_TYPE = {
    "webp": "image/webp",
    "avif": "image/avif",
    "jpeg": "image/jpeg",
    "png": "image/png",
}
EXTENSION = {"webp": "webp", "avif": "avif", "jpeg": "jpg", "png": "png"}

# Formats we are willing to read and rewrite. Anything else (SVG, GIF animations,
# oEmbed videos) is skipped rather than mangled.
SOURCE_FORMATS = {"JPEG", "PNG", "WEBP", "AVIF", "TIFF", "BMP", "GIF", "MPO"}


class OptimizeSettings(BaseModel):
    format: OutputFormat = "webp"
    quality: int = Field(82, ge=1, le=100)
    max_width: int = Field(2000, ge=64, le=10000)
    max_height: int = Field(2000, ge=64, le=10000)
    strip_metadata: bool = True
    replace_original: bool = True
    skip_if_not_smaller: bool = True
    auto_optimize_new_uploads: bool = False


class OptimizeResult(BaseModel):
    data: bytes
    format: str  # webp | avif | jpeg | png
    content_type: str
    extension: str
    original_bytes: int
    optimized_bytes: int
    original_size: tuple
    new_size: tuple
    skipped_reason: Optional[str] = None

    @property
    def saved_bytes(self) -> int:
        return self.original_bytes - self.optimized_bytes

    @property
    def saved_percent(self) -> float:
        if not self.original_bytes:
            return 0.0
        return round(100 * self.saved_bytes / self.original_bytes, 1)


def avif_supported() -> bool:
    return bool(features.check("avif"))


def _resolve_format(requested: OutputFormat, source_pil_format: str) -> str:
    if requested == "keep":
        mapping = {"JPEG": "jpeg", "MPO": "jpeg", "PNG": "png", "WEBP": "webp", "AVIF": "avif"}
        return mapping.get(source_pil_format, "webp")
    if requested == "avif" and not avif_supported():
        return "webp"
    return requested


def _skip_result(data, source_format, original_bytes, original_size, reason) -> OptimizeResult:
    return OptimizeResult(
        data=data,
        format=source_format.lower() or "unknown",
        content_type=Image.MIME.get(source_format, "application/octet-stream"),
        extension=source_format.lower(),
        original_bytes=original_bytes,
        optimized_bytes=original_bytes,
        original_size=original_size,
        new_size=original_size,
        skipped_reason=reason,
    )


def optimize_image(data: bytes, cfg: OptimizeSettings) -> OptimizeResult:
    """Resize, convert and compress a single image.

    Returns the original bytes untouched (with ``skipped_reason`` set) when the
    file is not something we should touch or when the result would be larger
    and ``skip_if_not_smaller`` is on.
    """
    original_bytes = len(data)
    with Image.open(BytesIO(data)) as img:
        source_format = (img.format or "").upper()
        original_size = img.size

        if source_format not in SOURCE_FORMATS:
            return _skip_result(data, source_format, original_bytes, original_size,
                                f"unsupported source format {source_format or 'unknown'}")
        if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
            return _skip_result(data, source_format, original_bytes, original_size, "animated image")

        out_format = _resolve_format(cfg.format, source_format)

        # Apply EXIF orientation before we strip EXIF, otherwise photos rotate.
        img = ImageOps.exif_transpose(img)
        icc = None if cfg.strip_metadata else img.info.get("icc_profile")

        has_alpha = img.mode in ("RGBA", "LA", "P") and (
            img.mode != "P" or "transparency" in img.info
        )
        if out_format == "jpeg":
            if has_alpha:
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")
        else:
            if has_alpha:
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
            elif img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

        if img.width > cfg.max_width or img.height > cfg.max_height:
            img.thumbnail((cfg.max_width, cfg.max_height), Image.Resampling.LANCZOS)

        save_kwargs = {}
        if out_format == "webp":
            save_kwargs = {"quality": cfg.quality, "method": 6}
        elif out_format == "avif":
            save_kwargs = {"quality": cfg.quality, "speed": 6}
        elif out_format == "jpeg":
            save_kwargs = {"quality": cfg.quality, "optimize": True, "progressive": True}
        elif out_format == "png":
            save_kwargs = {"optimize": True, "compress_level": 9}
        if icc:
            save_kwargs["icc_profile"] = icc

        buffer = BytesIO()
        img.save(buffer, PIL_FORMAT[out_format], **save_kwargs)
        out = buffer.getvalue()
        new_size = img.size

    if cfg.skip_if_not_smaller and len(out) >= original_bytes and new_size == original_size:
        return _skip_result(
            data, source_format, original_bytes, original_size,
            "already smaller than the optimized version",
        )

    return OptimizeResult(
        data=out,
        format=out_format,
        content_type=CONTENT_TYPE[out_format],
        extension=EXTENSION[out_format],
        original_bytes=original_bytes,
        optimized_bytes=len(out),
        original_size=original_size,
        new_size=new_size,
    )

