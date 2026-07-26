"""Deterministic, offline post-processing for generated resident sprite strips."""
from __future__ import annotations

from collections import Counter, deque
from io import BytesIO

from PIL import Image, UnidentifiedImageError


FRAME_SIZE = 32
ATLAS_SIZE = (FRAME_SIZE * 3, FRAME_SIZE * 4)
PORTRAIT_SIZE = 256
MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_INPUT_PIXELS = 8_294_400
MAX_ATLAS_COLORS = 32

_CONTENT_WIDTH = 28
_CONTENT_HEIGHT = 30
_FOOT_BASELINE = 30
_BACKGROUND_TOLERANCE_SQ = 24 * 24
_MAX_EDGE_ARTIFACT_PIXELS = 64


class ResidentSpritePostprocessError(ValueError):
    """Invalid post-processing input with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _fail(code: str, message: str) -> ResidentSpritePostprocessError:
    return ResidentSpritePostprocessError(code, message)


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
    except ResidentSpritePostprocessError:
        raise
    except Image.DecompressionBombError as exc:
        raise _fail("INPUT_PIXEL_LIMIT", f"{label} exceeds the pixel limit") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise _fail("INPUT_NOT_PNG", f"{label} is not a valid PNG") from exc


def _distance_sq(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return sum((a - b) ** 2 for a, b in zip(left, right, strict=True))


def _clear_edge_artifacts(frame: Image.Image) -> Image.Image:
    """Remove small opaque remnants still connected to the source frame edge."""
    result = frame.copy()
    width, height = result.size
    pixels = result.load()
    seen = bytearray(width * height)

    border = (
        [(x, 0) for x in range(width)]
        + [(x, height - 1) for x in range(width)]
        + [(0, y) for y in range(height)]
        + [(width - 1, y) for y in range(height)]
    )
    for start_x, start_y in border:
        start_index = start_y * width + start_x
        if seen[start_index] or pixels[start_x, start_y][3] == 0:
            continue
        queue: deque[tuple[int, int]] = deque([(start_x, start_y)])
        seen[start_index] = 1
        component: list[tuple[int, int]] = []
        while queue:
            x, y = queue.popleft()
            component.append((x, y))
            for next_x, next_y in (
                (x - 1, y),
                (x + 1, y),
                (x, y - 1),
                (x, y + 1),
            ):
                if not (0 <= next_x < width and 0 <= next_y < height):
                    continue
                index = next_y * width + next_x
                if seen[index] or pixels[next_x, next_y][3] == 0:
                    continue
                seen[index] = 1
                queue.append((next_x, next_y))
        if len(component) <= _MAX_EDGE_ARTIFACT_PIXELS:
            for x, y in component:
                pixels[x, y] = (0, 0, 0, 0)
    return result


def _clear_edge_background(frame: Image.Image) -> Image.Image:
    """Make an opaque, near-solid edge backdrop transparent by flood fill."""
    result = frame.copy()
    alpha = result.getchannel("A")
    if alpha.getextrema()[0] < 255:
        # The provider already supplied alpha; preserve its silhouette and holes.
        alpha = alpha.point(lambda value: 255 if value >= 128 else 0)
        result.putalpha(alpha)
        return _clear_edge_artifacts(result)

    width, height = result.size
    pixels = result.load()
    border = Counter(
        [pixels[x, 0][:3] for x in range(width)]
        + [pixels[x, height - 1][:3] for x in range(width)]
        + [pixels[0, y][:3] for y in range(height)]
        + [pixels[width - 1, y][:3] for y in range(height)]
    )
    background = border.most_common(1)[0][0]
    queue: deque[tuple[int, int]] = deque()
    seen = bytearray(width * height)

    def enqueue(x: int, y: int) -> None:
        index = y * width + x
        if seen[index]:
            return
        seen[index] = 1
        if _distance_sq(pixels[x, y][:3], background) <= _BACKGROUND_TOLERANCE_SQ:
            queue.append((x, y))

    for x in range(width):
        enqueue(x, 0)
        enqueue(x, height - 1)
    for y in range(height):
        enqueue(0, y)
        enqueue(width - 1, y)

    while queue:
        x, y = queue.popleft()
        pixels[x, y] = (0, 0, 0, 0)
        for next_x, next_y in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= next_x < width and 0 <= next_y < height:
                enqueue(next_x, next_y)
    return _clear_edge_artifacts(result)


def _split_strip(strip: Image.Image, *, label: str) -> list[Image.Image]:
    if strip.width % 3 != 0:
        raise _fail(
            "STRIP_DIMENSIONS_INVALID",
            f"{label} width must be divisible by three",
        )
    frame_width = strip.width // 3
    if strip.height not in {frame_width, frame_width * 2}:
        raise _fail(
            "STRIP_DIMENSIONS_INVALID",
            f"{label} must contain three square or 1:2 portrait frames",
        )
    return [
        _clear_edge_background(
            strip.crop((index * frame_width, 0, (index + 1) * frame_width, strip.height))
        )
        for index in range(3)
    ]


def _content_bbox(frame: Image.Image) -> tuple[int, int, int, int]:
    bbox = frame.getchannel("A").getbbox()
    if bbox is None:
        raise _fail("FRAME_EMPTY", "a strip contains an empty frame")
    return bbox


def _normalize_frames(frames: list[Image.Image]) -> list[Image.Image]:
    bboxes = [_content_bbox(frame) for frame in frames]
    max_width = max(right - left for left, _, right, _ in bboxes)
    max_height = max(bottom - top for _, top, _, bottom in bboxes)
    scale = min(_CONTENT_WIDTH / max_width, _CONTENT_HEIGHT / max_height)

    normalized: list[Image.Image] = []
    for frame, bbox in zip(frames, bboxes, strict=True):
        cropped = frame.crop(bbox)
        width = max(1, min(_CONTENT_WIDTH, round(cropped.width * scale)))
        height = max(1, min(_CONTENT_HEIGHT, round(cropped.height * scale)))
        resized = cropped.resize((width, height), Image.Resampling.NEAREST)
        resized_bbox = _content_bbox(resized)
        actual_width = resized_bbox[2] - resized_bbox[0]
        canvas = Image.new("RGBA", (FRAME_SIZE, FRAME_SIZE), (0, 0, 0, 0))
        x = (FRAME_SIZE - actual_width) // 2 - resized_bbox[0]
        y = _FOOT_BASELINE + 1 - resized_bbox[3]
        canvas.alpha_composite(resized, (x, y))
        normalized.append(canvas)
    return normalized


def _limit_atlas_palette(atlas: Image.Image) -> Image.Image:
    """Use at most 31 opaque RGB colours plus transparent."""
    pixels = [
        atlas.getpixel((x, y))
        for y in range(atlas.height)
        for x in range(atlas.width)
    ]
    opaque_colors = [pixel[:3] for pixel in pixels if pixel[3] > 0]
    if not opaque_colors:
        raise _fail("ATLAS_EMPTY", "the assembled atlas is empty")

    if len(set(opaque_colors)) > MAX_ATLAS_COLORS - 1:
        samples = Image.new("RGB", (len(opaque_colors), 1))
        samples.putdata(opaque_colors)
        quantized = samples.quantize(
            colors=MAX_ATLAS_COLORS - 1,
            method=Image.Quantize.MEDIANCUT,
            dither=Image.Dither.NONE,
        ).convert("RGB")
        opaque_colors = [quantized.getpixel((x, 0)) for x in range(quantized.width)]

    color_iter = iter(opaque_colors)
    limited = Image.new("RGBA", atlas.size, (0, 0, 0, 0))
    limited.putdata(
        [(*next(color_iter), 255) if pixel[3] > 0 else (0, 0, 0, 0) for pixel in pixels]
    )
    return limited


def build_resident_sprite_atlas(
    down_strip_png: bytes,
    left_strip_png: bytes,
    up_strip_png: bytes,
    *,
    right_strip_png: bytes | None = None,
) -> bytes:
    """Normalize direction strips into the fixed Phaser resident atlas.

    Each input is a horizontal PNG containing three equally-sized square or
    1:2 portrait frames.
    The output is a 96x128 RGBA PNG with rows down, left, right, up. By default
    right mirrors left; an explicit right strip is preserved instead. All source
    frames share one scale, are horizontally centered, and use pixel row 30 as
    their opaque foot baseline.
    """
    strip_inputs = [
        ("down strip", down_strip_png),
        ("left strip", left_strip_png),
        ("up strip", up_strip_png),
    ]
    if right_strip_png is not None:
        strip_inputs.append(("right strip", right_strip_png))
    strips = [_open_png(data, label=label) for label, data in strip_inputs]
    split_directions = [
        _split_strip(strip, label=label)
        for strip, (label, _) in zip(strips, strip_inputs, strict=True)
    ]
    if len({strip.size for strip in strips}) != 1:
        raise _fail("STRIP_DIMENSIONS_MISMATCH", "all direction strips must match")

    source_frames = [frame for direction in split_directions for frame in direction]
    normalized = _normalize_frames(source_frames)
    down, left, up = normalized[:3], normalized[3:6], normalized[6:9]
    right = (
        [frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT) for frame in left]
        if right_strip_png is None
        else normalized[9:12]
    )

    atlas = Image.new("RGBA", ATLAS_SIZE, (0, 0, 0, 0))
    for row, direction in enumerate((down, left, right, up)):
        for column, frame in enumerate(direction):
            atlas.alpha_composite(frame, (column * FRAME_SIZE, row * FRAME_SIZE))
    return _png_bytes(_limit_atlas_palette(atlas))


def derive_resident_portrait(atlas_png: bytes, *, size: int = PORTRAIT_SIZE) -> bytes:
    """Derive a crisp square portrait PNG from the atlas down-facing idle frame."""
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > 2048:
        raise _fail("PORTRAIT_SIZE_INVALID", "portrait size must be between 1 and 2048")
    atlas = _open_png(atlas_png, label="atlas")
    if atlas.size != ATLAS_SIZE:
        raise _fail("ATLAS_DIMENSIONS_INVALID", "atlas must be 96x128")
    idle = atlas.crop((FRAME_SIZE, 0, FRAME_SIZE * 2, FRAME_SIZE))
    if idle.getchannel("A").getbbox() is None:
        raise _fail("FRAME_EMPTY", "the down-facing idle frame is empty")
    portrait = idle.resize((size, size), Image.Resampling.NEAREST)
    return _png_bytes(portrait)
