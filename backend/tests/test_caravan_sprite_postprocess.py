from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from app.services.caravan_sprite_postprocess import (
    ATLAS_SIZE,
    FRAME_SIZE,
    CaravanSpritePostprocessError,
    build_caravan_sprite_atlas,
    build_caravan_stall_sprite,
)


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGBA")


def _strip(*, width: int, height: int, color: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    boundaries = [round(index * width / 3) for index in range(4)]
    for index in range(3):
        left, right = boundaries[index], boundaries[index + 1]
        inset = 10 + index
        draw.rectangle(
            (left + inset, 12, right - inset - 1, height - 13 - index),
            fill=color,
        )
        draw.point((left + inset + index, 12), fill=(index * 70, 40, 220, 255))
    return _png(image)


def test_build_caravan_atlas_has_fixed_transparent_contract() -> None:
    atlas = _open(
        build_caravan_sprite_atlas(
            _strip(width=301, height=180, color=(30, 80, 180, 255)),
            _strip(width=360, height=120, color=(170, 100, 30, 255)),
            _strip(width=299, height=181, color=(30, 140, 100, 255)),
        )
    )

    assert atlas.size == ATLAS_SIZE
    assert atlas.mode == "RGBA"
    assert atlas.getpixel((0, 0)) == (0, 0, 0, 0)
    assert atlas.getchannel("A").getextrema() == (0, 255)
    assert len(atlas.getcolors(maxcolors=ATLAS_SIZE[0] * ATLAS_SIZE[1]) or []) <= 32

    for row in range(4):
        for column in range(3):
            frame = atlas.crop(
                (
                    column * FRAME_SIZE,
                    row * FRAME_SIZE,
                    (column + 1) * FRAME_SIZE,
                    (row + 1) * FRAME_SIZE,
                )
            )
            bbox = frame.getchannel("A").getbbox()
            assert bbox is not None
            assert bbox[3] == 62
            assert bbox[0] > 0 and bbox[2] < FRAME_SIZE

    for column in range(3):
        left = atlas.crop(
            (
                column * FRAME_SIZE,
                FRAME_SIZE,
                (column + 1) * FRAME_SIZE,
                FRAME_SIZE * 2,
            )
        )
        right = atlas.crop(
            (
                column * FRAME_SIZE,
                FRAME_SIZE * 2,
                (column + 1) * FRAME_SIZE,
                FRAME_SIZE * 3,
            )
        )
        assert right.tobytes() == left.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes()


def test_build_stall_sprite_uses_same_palette_and_baseline() -> None:
    source = Image.new("RGBA", (240, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(source)
    for index in range(50):
        draw.rectangle(
            (20 + index, 30, 210 - index, 145),
            outline=(index * 5, 120, 220 - index * 3, 255),
        )

    sprite = _open(build_caravan_stall_sprite(_png(source)))
    bbox = sprite.getchannel("A").getbbox()
    assert sprite.size == (FRAME_SIZE, FRAME_SIZE)
    assert bbox is not None and bbox[3] == 62
    assert sprite.getpixel((0, 0)) == (0, 0, 0, 0)
    assert len(sprite.getcolors(maxcolors=FRAME_SIZE * FRAME_SIZE) or []) <= 32


def test_postprocess_rejects_invalid_png() -> None:
    with pytest.raises(CaravanSpritePostprocessError) as exc_info:
        build_caravan_stall_sprite(b"not-png")
    assert exc_info.value.code == "INPUT_NOT_PNG"
