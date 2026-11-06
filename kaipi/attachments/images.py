"""Default image decoder, private to the replaceable attachment parser."""

from __future__ import annotations

import base64
import warnings
from io import BytesIO
from typing import Any

from PIL import EpsImagePlugin, Image, ImageCms, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener

from kaipi.i18n import tr

register_heif_opener(thumbnails=False)
EpsImagePlugin.gs_binary = False  # attachment decoding must not invoke external interpreters
MAX_PIXELS = 64_000_000
MAX_EDGE = 4096
MAX_FRAMES = 8
MAX_IMAGE = 5 * 1024 * 1024
MAX_IMAGES_TOTAL = 10 * 1024 * 1024


def block(raw: bytes, fmt: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": Image.MIME[fmt],
            "data": base64.b64encode(raw).decode(),
        },
    }


def images(raw: bytes) -> tuple[list[dict[str, Any]], str] | None:
    """Detect from bytes, correct orientation/colour, and keep every bounded frame."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            source = Image.open(BytesIO(raw))
    except UnidentifiedImageError:
        return None
    except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise ValueError(tr("图片损坏或像素数量过大")) from e
    with source:
        try:
            count = getattr(source, "n_frames", 1)
        except (OSError, EOFError) as e:
            raise ValueError(tr("图片帧信息损坏")) from e
        if count > MAX_FRAMES:
            raise ValueError(
                tr(
                    "图片包含 {count} 帧，最多支持 {MAX_FRAMES} 帧；请选取需要的帧",
                    count=count,
                    MAX_FRAMES=MAX_FRAMES,
                )
            )
        if (
            count == 1
            and source.format in {"PNG", "JPEG", "WEBP"}
            and max(source.size) <= MAX_EDGE
            and source.width * source.height <= MAX_PIXELS
            and source.getexif().get(274, 1) == 1
            and not source.info.get("icc_profile")
            and len(raw) <= MAX_IMAGE
        ):
            try:
                source.load()
            except (OSError, SyntaxError) as e:
                raise ValueError(tr("图片损坏，无法解码")) from e
            return [block(raw, source.format)], f"{source.format} · {source.width}×{source.height}"
        blocks = []
        total = 0
        formats: set[str] = set()
        durations: list[int] = []
        resized = False
        try:
            for index in range(count):
                source.seek(index)
                if source.width * source.height > MAX_PIXELS:
                    raise ValueError(tr("图片超过 6400 万像素，请缩小后添加"))
                frame = ImageOps.exif_transpose(source)
                durations.append(int(source.info.get("duration") or 0))
                try:
                    if frame.mode.startswith("I;16"):
                        frame = frame.convert("I").point(lambda v: v / 257).convert("L")
                    if frame.mode == "F":
                        raise ValueError(tr("浮点图像请先转换为普通 RGB 图片"))
                    mode = (
                        "RGBA" if "A" in frame.getbands() or "transparency" in frame.info else "RGB"
                    )
                    profile = frame.info.get("icc_profile")
                    frame = frame.convert(mode)
                    if profile:
                        converted = ImageCms.profileToProfile(
                            frame,
                            ImageCms.ImageCmsProfile(BytesIO(profile)),
                            ImageCms.createProfile("sRGB"),
                            outputMode=mode,
                        )
                        if converted is None:
                            raise ValueError(tr("图片色彩转换失败"))
                        frame = converted
                    original_size = frame.size
                    frame.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                    resized = resized or frame.size != original_size
                    fmt = "PNG"
                    encoded = BytesIO()
                    frame.save(encoded, format=fmt)
                    if encoded.tell() > MAX_IMAGE and mode == "RGB":
                        fmt = "JPEG"
                        encoded = BytesIO()
                        frame.save(encoded, format=fmt, quality=92, subsampling=0)
                    # Bound the actual payload, not just the compressed source file.
                    while encoded.tell() > MAX_IMAGE and max(frame.size) > 256:
                        resized = True
                        frame.thumbnail(
                            (max(1, frame.width * 3 // 4), max(1, frame.height * 3 // 4))
                        )
                        encoded = BytesIO()
                        frame.save(
                            encoded, format=fmt, **({"quality": 92} if fmt == "JPEG" else {})
                        )
                    data = encoded.getvalue()
                    total += len(data)
                    if len(data) > MAX_IMAGE or total > MAX_IMAGES_TOTAL:
                        raise ValueError(tr("转换后的图片过大，请缩小或减少帧数"))
                    formats.add(fmt)
                    blocks.append(block(data, fmt))
                finally:
                    frame.close()
        except (OSError, EOFError, SyntaxError, ImageCms.PyCMSError) as e:
            raise ValueError(tr("图片无法解码或色彩配置损坏")) from e
        label = f"{source.format} → {'/'.join(sorted(formats))}"
        if count > 1:
            label += f" · {count} 帧全部保留"
        if count > 1 and any(durations):
            label += " · 帧时长(ms): " + ",".join(str(d) for d in durations)
        if resized:
            label += f" · 已缩放，长边 {MAX_EDGE}px 以内"
        return blocks, label
