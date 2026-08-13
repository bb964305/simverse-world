"""Deterministic post-processing for the travelling caravan sprite assets."""
from __future__ import annotations

from io import BytesIO

from PIL import Image, UnidentifiedImageError


FRAME_SIZE = 64
ATLAS_SIZE = (FRAME_SIZE * 3, FRAME_SIZE * 4)
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_INPUT_PIXELS = 8_294_400
MAX_ATLAS_COLORS = 32

_CONTENT_SIZE = 60
_BASELINE_EXCLUSIVE = 62
_ALPHA_THRESHOLD = 128


class CaravanSpritePostprocessError(ValueError):
    """Invalid post-processing input with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> CaravanSpritePostprocessError:
    return CaravanSpritePostprocessError(code, message)


def _png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True, compress_level=9)
    return output.getvalue()


def _open_png(data: bytes, *, label: str) -> Image.Image:
    if not isinstance(data, bytes):
        raise _fail("INPUT_TYPE_INVALID", f"{label} must be bytes")
    if not data:
        raise _fail("INPUT_EMPTY", f"{label} is empty")
    if len(data) > MAX_INPUT_BYTES:
        raise _fail("INPUT_TOO_LARGE", f"{label} exceeds the byte limit")
    try:
        with Image.open(BytesIO(data)) as source:
            if source.format != "PNG":
                raise _fail("INPUT_NOT_PNG", f"{label} must be a PNG")
            if getattr(source, "n_frames", 1) != 1:
                raise _fail("INPUT_ANIMATED", f"{label} must contain one image")
            width, height = source.size
            if width <= 0 or height <= 0:
                raise _fail("INPUT_DIMENSIONS_INVALID", f"{label} has invalid dimensions")
            if width * height > MAX_INPUT_PIXELS:
                raise _fail("INPUT_PIXEL_LIMIT", f"{label} exceeds the pixel limit")
            source.load()
            return source.convert("RGBA")
    except CaravanSpritePostprocessError:
        raise
    except Image.DecompressionBombError as exc:
        raise _fail("INPUT_PIXEL_LIMIT", f"{label} exceeds the pixel limit") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise _fail("INPUT_NOT_PNG", f"{label} is not a valid PNG") from exc


def _binary_alpha(image: Image.Image) -> Image.Image:
    result = image.copy()
    alpha = result.getchannel("A").point(
        lambda value: 255 if value >= _ALPHA_THRESHOLD else 0
    )
    result.putalpha(alpha)
    pixels = [
        pixel if pixel[3] else (0, 0, 0, 0)
        for pixel in result.get_flattened_data()
    ]
    result.putdata(pixels)
    return result


def _split_strip(strip: Image.Image, *, label: str) -> list[Image.Image]:
    if strip.width < 3 or strip.height <= 0:
        raise _fail("STRIP_DIMENSIONS_INVALID", f"{label} is too small")
    boundaries = [round(index * strip.width / 3) for index in range(4)]
    frames = [
        _binary_alpha(
            strip.crop((boundaries[index], 0, boundaries[index + 1], strip.height))
        )
        for index in range(3)
    ]
    if any(frame.getchannel("A").getbbox() is None for frame in frames):
        raise _fail("FRAME_EMPTY", f"{label} contains an empty frame")
    return frames


def _normalize_frames(frames: list[Image.Image]) -> list[Image.Image]:
    bboxes = [frame.getchannel("A").getbbox() for frame in frames]
    if any(bbox is None for bbox in bboxes):
        raise _fail("FRAME_EMPTY", "an input contains an empty frame")
    typed_bboxes = [bbox for bbox in bboxes if bbox is not None]
    max_width = max(right - left for left, _, right, _ in typed_bboxes)
    max_height = max(bottom - top for _, top, _, bottom in typed_bboxes)
    scale = min(_CONTENT_SIZE / max_width, _CONTENT_SIZE / max_height)

    normalized: list[Image.Image] = []
    for frame, bbox in zip(frames, typed_bboxes, strict=True):
        cropped = frame.crop(bbox)
        width = max(1, min(_CONTENT_SIZE, round(cropped.width * scale)))
        height = max(1, min(_CONTENT_SIZE, round(cropped.height * scale)))
        resized = cropped.resize((width, height), Image.Resampling.NEAREST)
        canvas = Image.new("RGBA", (FRAME_SIZE, FRAME_SIZE), (0, 0, 0, 0))
        canvas.alpha_composite(
            resized,
            ((FRAME_SIZE - width) // 2, _BASELINE_EXCLUSIVE - height),
        )
        normalized.append(canvas)
    return normalized


def _limit_palette(image: Image.Image) -> Image.Image:
    pixels = list(image.get_flattened_data())
    opaque = [pixel[:3] for pixel in pixels if pixel[3] > 0]
    if not opaque:
        raise _fail("SPRITE_EMPTY", "the assembled sprite is empty")
    if len(set(opaque)) > MAX_ATLAS_COLORS - 1:
        samples = Image.new("RGB", (len(opaque), 1))
        samples.putdata(opaque)
        reduced = samples.quantize(
            colors=MAX_ATLAS_COLORS - 1,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        ).convert("RGB")
        opaque = list(reduced.get_flattened_data())

    colors = iter(opaque)
    limited = Image.new("RGBA", image.size, (0, 0, 0, 0))
    limited.putdata(
        [(*next(colors), 255) if pixel[3] else (0, 0, 0, 0) for pixel in pixels]
    )
    return limited


def build_caravan_sprite_atlas(
    down_strip_png: bytes,
    left_strip_png: bytes,
    up_strip_png: bytes,
) -> bytes:
    """Build a 192x256 RGBA atlas with down/left/right/up rows.

    Each source strip contains three equal horizontal animation panels. The
    right-facing row is a deterministic mirror of the left-facing row.
    """
    strips = [
        _open_png(down_strip_png, label="down strip"),
        _open_png(left_strip_png, label="left strip"),
        _open_png(up_strip_png, label="up strip"),
    ]
    directions = [
        _split_strip(strip, label=label)
        for strip, label in zip(strips, ("down strip", "left strip", "up strip"), strict=True)
    ]
    normalized = _normalize_frames([frame for row in directions for frame in row])
    down, left, up = normalized[:3], normalized[3:6], normalized[6:9]
    right = [frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT) for frame in left]

    atlas = Image.new("RGBA", ATLAS_SIZE, (0, 0, 0, 0))
    for row_index, row in enumerate((down, left, right, up)):
        for column_index, frame in enumerate(row):
            atlas.alpha_composite(
                frame,
                (column_index * FRAME_SIZE, row_index * FRAME_SIZE),
            )
    return _png_bytes(_limit_palette(atlas))


def build_caravan_stall_sprite(source_png: bytes) -> bytes:
    """Normalize the parked, unfolded wagon into one transparent 64px sprite."""
    source = _binary_alpha(_open_png(source_png, label="stall source"))
    if source.getchannel("A").getbbox() is None:
        raise _fail("FRAME_EMPTY", "stall source is empty")
    normalized = _normalize_frames([source])[0]
    return _png_bytes(_limit_palette(normalized))
