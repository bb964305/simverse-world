"""Pixel-art post-processing: snap AI-generated images onto a true pixel grid.

Python port of the core pipeline of Tezumie/Image-to-Pixel
(https://github.com/Tezumie/Image-to-Pixel):

  1. nearest-neighbour downsample to a coarse pixel grid
     (equivalent to canvas drawImage with imageSmoothingEnabled=false)
  2. palette quantization to a limited colour count
  3. nearest-neighbour upscale so the result stays crisp everywhere

Extras for the avatar use case:
  - content cropping (so the character fills the grid)
  - edge flood-fill background removal (solid backdrop -> transparent PNG)

Pure Pillow, no numpy. Designed to run in the async portrait pipeline, so it
must never raise on odd inputs at the call site — callers wrap it in
try/except and fall back to the raw image.
"""
from __future__ import annotations

import logging
from collections import Counter, deque
from io import BytesIO

from PIL import Image, ImageChops

logger = logging.getLogger(__name__)

# Colour distance (squared, RGB) under which a pixel counts as "background".
_BG_TOLERANCE_SQ = 52 * 52
# Margin (in output grid cells) kept around the cropped character.
_CROP_MARGIN_CELLS = 1


def _dominant_border_color(img: Image.Image) -> tuple[int, int, int]:
    """Most common colour along the image border — assumed to be the backdrop."""
    w, h = img.size
    px = img.load()
    border: Counter = Counter()
    for x in range(w):
        border[px[x, 0]] += 1
        border[px[x, h - 1]] += 1
    for y in range(h):
        border[px[0, y]] += 1
        border[px[w - 1, y]] += 1
    return border.most_common(1)[0][0]


def _dist_sq(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2


def _content_bbox(
    img: Image.Image, bg: tuple[int, int, int], threshold: int = 52
) -> tuple[int, int, int, int] | None:
    """Bounding box of everything that is not background-coloured."""
    solid = Image.new("RGB", img.size, bg)
    diff = ImageChops.difference(img, solid).convert("L")
    mask = diff.point(lambda v: 255 if v > threshold // 2 else 0)
    return mask.getbbox()


def _remove_background(img: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
    """Flood-fill transparent from the borders, eating pixels close to `bg`.

    BFS from the border only, so background-coloured details *inside* the
    character (eyes, buckles...) survive.
    """
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()

    queue: deque[tuple[int, int]] = deque()
    seen = bytearray(w * h)

    def try_seed(x: int, y: int) -> None:
        if not seen[y * w + x] and _dist_sq(px[x, y][:3], bg) <= _BG_TOLERANCE_SQ:
            seen[y * w + x] = 1
            queue.append((x, y))

    for x in range(w):
        try_seed(x, 0)
        try_seed(x, h - 1)
    for y in range(h):
        try_seed(0, y)
        try_seed(w - 1, y)

    while queue:
        x, y = queue.popleft()
        px[x, y] = (0, 0, 0, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx]:
                if _dist_sq(px[nx, ny][:3], bg) <= _BG_TOLERANCE_SQ:
                    seen[ny * w + nx] = 1
                    queue.append((nx, ny))
    return rgba


def _defringe(
    rgba: Image.Image, bg: tuple[int, int, int], iterations: int = 2
) -> Image.Image:
    """Strip silhouette pixels still tinted toward the backdrop colour.

    Antialiased source edges blend character and backdrop; after quantization
    a few of those blend colours can survive on the outline as a halo. Any
    opaque pixel that touches transparency AND sits within ~2x the background
    tolerance gets removed.
    """
    tol = _BG_TOLERANCE_SQ * 4
    w, h = rgba.size
    px = rgba.load()
    for _ in range(iterations):
        kill = []
        for y in range(h):
            for x in range(w):
                p = px[x, y]
                if p[3] == 0 or _dist_sq(p[:3], bg) > tol:
                    continue
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and px[nx, ny][3] == 0:
                        kill.append((x, y))
                        break
        if not kill:
            break
        for x, y in kill:
            px[x, y] = (0, 0, 0, 0)
    return rgba


def _absorb_stray_bg(rgba: Image.Image, bg: tuple[int, int, int]) -> Image.Image:
    """Recolour isolated backdrop-coloured pixels trapped inside the character.

    Border flood fill can't reach enclosed pockets; repaint them with the most
    common opaque non-backdrop neighbour colour (or clear them if there is
    none) so no backdrop speck survives inside the sprite.
    """
    w, h = rgba.size
    px = rgba.load()
    # Colour frequency over opaque pixels: a near-backdrop colour that only
    # covers a couple of cells is a blend artifact; a widespread one is a
    # legitimate character colour (e.g. pink hair) and must be kept.
    freq: Counter = Counter(
        px[x, y][:3] for y in range(h) for x in range(w) if px[x, y][3] > 0
    )
    loose_tol = _BG_TOLERANCE_SQ * 4
    fixes = []
    for y in range(h):
        for x in range(w):
            p = px[x, y]
            if p[3] == 0:
                continue
            d = _dist_sq(p[:3], bg)
            if d > _BG_TOLERANCE_SQ and not (d <= loose_tol and freq[p[:3]] <= 3):
                continue
            counts: Counter = Counter()
            for nx, ny in (
                (x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1),
                (x + 1, y + 1), (x - 1, y - 1), (x + 1, y - 1), (x - 1, y + 1),
            ):
                if 0 <= nx < w and 0 <= ny < h:
                    q = px[nx, ny]
                    if q[3] > 0 and _dist_sq(q[:3], bg) > _BG_TOLERANCE_SQ:
                        counts[q] += 1
            fixes.append((x, y, counts.most_common(1)[0][0] if counts else (0, 0, 0, 0)))
    for x, y, c in fixes:
        px[x, y] = c
    return rgba


def pixelate_image(
    data: bytes,
    grid: int = 64,
    colors: int = 32,
    upscale: int = 8,
    transparent: bool = True,
) -> bytes:
    """Convert a high-res pseudo-pixel-art image into true grid-aligned pixel art.

    Args:
        data: source image bytes (any format Pillow can open).
        grid: output resolution — the character fits in a grid x grid canvas.
        colors: max palette size after quantization.
        upscale: nearest-neighbour upscale factor for the stored PNG
                 (grid*upscale square), so it renders crisp without CSS hints.
        transparent: remove the solid backdrop via edge flood fill.

    Returns:
        PNG bytes, RGBA, (grid*upscale) x (grid*upscale).
    """
    img = Image.open(BytesIO(data)).convert("RGB")

    # 1) Detect backdrop and crop to the character so it fills the grid.
    bg = _dominant_border_color(img)
    bbox = _content_bbox(img, bg)
    if bbox:
        cell = max(img.size) / grid  # source pixels per output cell
        margin = int(cell * _CROP_MARGIN_CELLS)
        left = max(0, bbox[0] - margin)
        top = max(0, bbox[1] - margin)
        right = min(img.width, bbox[2] + margin)
        bottom = min(img.height, bbox[3] + margin)
        if right > left and bottom > top:
            img = img.crop((left, top, right, bottom))

    # 2) Palette quantization at source resolution — every pixel snaps to a
    #    shared limited palette (flat clusters, no dithering) BEFORE the grid
    #    snap, so antialiased seams collapse into real palette colours.
    quant = img.quantize(
        colors=colors, method=Image.MEDIANCUT, dither=Image.Dither.NONE
    )
    palette = quant.getpalette()
    idx = quant.load()
    w, h = quant.size

    # 3) Downsample onto the pixel grid by per-cell colour mode. This is
    #    Image-to-Pixel's grid snap hardened against edge blends: the majority
    #    colour wins each cell instead of whatever single pixel the nearest-
    #    neighbour sample happens to hit.
    scale = grid / max(w, h)
    small_w = max(1, round(w * scale))
    small_h = max(1, round(h * scale))
    small = Image.new("RGB", (small_w, small_h))
    spx = small.load()
    for cy in range(small_h):
        y0 = cy * h // small_h
        y1 = max(y0 + 1, (cy + 1) * h // small_h)
        ys = max(1, (y1 - y0) // 8)
        for cx in range(small_w):
            x0 = cx * w // small_w
            x1 = max(x0 + 1, (cx + 1) * w // small_w)
            xs = max(1, (x1 - x0) // 8)
            counts: Counter = Counter()
            for yy in range(y0, y1, ys):
                for xx in range(x0, x1, xs):
                    counts[idx[xx, yy]] += 1
            i = counts.most_common(1)[0][0]
            spx[cx, cy] = (palette[3 * i], palette[3 * i + 1], palette[3 * i + 2])

    # 4) Backdrop -> transparency (flood fill from the borders only),
    #    then strip any leftover backdrop-tinted halo on the silhouette.
    if transparent:
        result = _remove_background(small, bg)
        result = _defringe(result, bg)
        result = _absorb_stray_bg(result, bg)
    else:
        result = small.convert("RGBA")

    # 5) Centre on a square grid canvas, then integer-upscale for storage.
    fill = (0, 0, 0, 0) if transparent else (*bg, 255)
    canvas = Image.new("RGBA", (grid, grid), fill)
    canvas.paste(
        result, ((grid - result.width) // 2, (grid - result.height) // 2), result
    )
    out = canvas.resize((grid * upscale, grid * upscale), Image.NEAREST)

    buf = BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
