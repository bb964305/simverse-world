"""Tests for the Image-to-Pixel style pixelation post-processing."""
from io import BytesIO

import pytest
from PIL import Image, ImageDraw, ImageFilter

from app.services.pixelate_service import pixelate_image

MAGENTA = (255, 0, 255)


def _fake_ai_render(size: int = 512, bg=MAGENTA) -> bytes:
    """A smooth 'AI style' character-ish blob on a solid backdrop:
    antialiased shapes + blur, i.e. nothing aligned to any pixel grid."""
    img = Image.new("RGB", (size, size), bg)
    d = ImageDraw.Draw(img)
    c = size // 2
    d.ellipse((c - 90, 60, c + 90, 240), fill=(235, 195, 158))  # head
    d.rectangle((c - 70, 230, c + 70, 400), fill=(90, 100, 140))  # body
    d.ellipse((c - 45, 120, c - 15, 160), fill=(30, 30, 35))  # eyes
    d.ellipse((c + 15, 120, c + 45, 160), fill=(30, 30, 35))
    d.rectangle((c - 60, 390, c - 20, 470), fill=(60, 45, 35))  # legs
    d.rectangle((c + 20, 390, c + 60, 470), fill=(60, 45, 35))
    img = img.filter(ImageFilter.GaussianBlur(1.5))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_output_is_grid_aligned_rgba_png():
    out = pixelate_image(_fake_ai_render(), grid=64, colors=32, upscale=8)
    img = Image.open(BytesIO(out))
    assert img.format == "PNG"
    assert img.mode == "RGBA"
    assert img.size == (512, 512)
    # every 8x8 block must be a single flat colour (true grid alignment)
    px = img.load()
    for by in range(0, 512, 8):
        for bx in range(0, 512, 8):
            block = {px[bx + i, by + j] for i in range(8) for j in range(8)}
            assert len(block) == 1


def test_palette_is_limited():
    out = pixelate_image(_fake_ai_render(), grid=64, colors=32, upscale=1)
    img = Image.open(BytesIO(out)).convert("RGBA")
    px = img.load()
    opaque = {
        px[x, y] for y in range(img.height) for x in range(img.width)
        if px[x, y][3] > 0
    }
    assert 0 < len(opaque) <= 32


def test_backdrop_becomes_transparent_without_leftovers():
    out = pixelate_image(_fake_ai_render(), grid=64, colors=32, upscale=1)
    img = Image.open(BytesIO(out)).convert("RGBA")
    px = img.load()
    # corners transparent
    for x, y in ((0, 0), (63, 0), (0, 63), (63, 63)):
        assert px[x, y][3] == 0
    # no near-magenta opaque pixel anywhere (halo / trapped speck check)
    for y in range(64):
        for x in range(64):
            r, g, b, a = px[x, y]
            if a > 0:
                assert (r - 255) ** 2 + g ** 2 + (b - 255) ** 2 > 52 * 52
    # character still present
    assert any(px[x, y][3] > 0 for y in range(64) for x in range(64))


def test_character_fills_grid_after_crop():
    """Even a small character on a large backdrop should fill the grid."""
    out = pixelate_image(_fake_ai_render(1024), grid=64, colors=32, upscale=1)
    img = Image.open(BytesIO(out)).convert("RGBA")
    bbox = img.getbbox()
    assert bbox is not None
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    assert max(w, h) >= 48  # >= 75% of the grid on the long side


def test_opaque_backdrop_kept_when_transparent_false():
    out = pixelate_image(_fake_ai_render(), grid=64, colors=32, upscale=1,
                         transparent=False)
    img = Image.open(BytesIO(out)).convert("RGBA")
    px = img.load()
    assert px[0, 0][3] == 255


def test_garbage_input_raises():
    """Callers rely on an exception (they fall back to the raw image)."""
    with pytest.raises(Exception):
        pixelate_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
