from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

import app.services.resident_sprite_postprocess as postprocess
from app.services.resident_sprite_postprocess import (
    ATLAS_SIZE,
    FRAME_SIZE,
    MAX_ATLAS_COLORS,
    ResidentSpritePostprocessError,
    build_resident_sprite_atlas,
    derive_resident_portrait,
)
from app.services.resident_sprite_qc import inspect_resident_sprite_atlas


def _png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _strip(color: tuple[int, int, int, int], *, high_color: bool = False) -> bytes:
    image = Image.new("RGBA", (72, 24), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(3):
        left = index * 24 + 7 - index
        right = index * 24 + 16 + index
        draw.rectangle((left, 5, right, 21), fill=color)
        # An asymmetric marker makes horizontal mirroring observable.
        draw.point((left, 8), fill=(255, 255, 255, 255))
        if high_color:
            for y in range(6, 21):
                for x in range(left + 1, right + 1):
                    image.putpixel((x, y), ((x * 17) % 256, (y * 29) % 256, (x + y) % 256, 255))
    return _png(image)


def _open(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image


@pytest.fixture
def strips() -> tuple[bytes, bytes, bytes]:
    return (
        _strip((220, 30, 30, 255)),
        _strip((30, 180, 60, 255)),
        _strip((30, 80, 220, 255)),
    )


def test_atlas_has_fixed_rgba_png_layout_and_transparency(strips) -> None:
    atlas = _open(build_resident_sprite_atlas(*strips))

    assert atlas.format == "PNG"
    assert atlas.mode == "RGBA"
    assert atlas.size == ATLAS_SIZE
    assert atlas.getpixel((0, 0))[3] == 0
    assert atlas.getpixel((48, 16))[:3] == (220, 30, 30)
    assert atlas.getpixel((48, 48))[:3] == (30, 180, 60)
    assert atlas.getpixel((48, 112))[:3] == (30, 80, 220)

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
            assert bbox[3] == 31
            assert abs((bbox[0] + bbox[2]) - FRAME_SIZE) <= 1


def test_right_row_is_exact_horizontal_mirror_of_left(strips) -> None:
    atlas = _open(build_resident_sprite_atlas(*strips))
    left = atlas.crop((0, FRAME_SIZE, ATLAS_SIZE[0], FRAME_SIZE * 2))
    right = atlas.crop((0, FRAME_SIZE * 2, ATLAS_SIZE[0], FRAME_SIZE * 3))

    expected = Image.new("RGBA", left.size)
    for column in range(3):
        frame = left.crop(
            (column * FRAME_SIZE, 0, (column + 1) * FRAME_SIZE, FRAME_SIZE)
        ).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        expected.alpha_composite(frame, (column * FRAME_SIZE, 0))
    assert right.tobytes() == expected.tobytes()


def test_generated_right_strip_is_used_without_mirroring(strips) -> None:
    generated_right = _strip((190, 40, 210, 255))
    atlas = _open(
        build_resident_sprite_atlas(*strips, right_strip_png=generated_right)
    )
    left = atlas.crop((0, FRAME_SIZE, ATLAS_SIZE[0], FRAME_SIZE * 2))
    right = atlas.crop((0, FRAME_SIZE * 2, ATLAS_SIZE[0], FRAME_SIZE * 3))

    assert right.getpixel((48, 16))[:3] == (190, 40, 210)
    assert right.tobytes() != left.transpose(Image.Transpose.FLIP_LEFT_RIGHT).tobytes()


def test_generated_right_strip_must_match_other_dimensions(strips) -> None:
    mismatched = _png(Image.new("RGBA", (60, 20), (0, 0, 0, 0)))
    with pytest.raises(ResidentSpritePostprocessError) as error:
        build_resident_sprite_atlas(*strips, right_strip_png=mismatched)
    assert error.value.code == "STRIP_DIMENSIONS_MISMATCH"


def test_palette_has_at_most_32_colors_including_transparency() -> None:
    atlas = _open(
        build_resident_sprite_atlas(
            _strip((220, 30, 30, 255), high_color=True),
            _strip((30, 180, 60, 255), high_color=True),
            _strip((30, 80, 220, 255), high_color=True),
        )
    )
    colors = atlas.getcolors(maxcolors=ATLAS_SIZE[0] * ATLAS_SIZE[1])
    assert colors is not None
    assert len(colors) <= MAX_ATLAS_COLORS


def test_opaque_edge_background_is_made_transparent() -> None:
    def opaque_strip(subject: tuple[int, int, int]) -> bytes:
        image = Image.new("RGB", (72, 24), (255, 0, 255))
        draw = ImageDraw.Draw(image)
        for index in range(3):
            draw.rectangle((index * 24 + 8, 5, index * 24 + 15, 21), fill=subject)
        return _png(image)

    atlas = _open(
        build_resident_sprite_atlas(
            opaque_strip((220, 30, 30)),
            opaque_strip((30, 180, 60)),
            opaque_strip((30, 80, 220)),
        )
    )
    assert atlas.getpixel((0, 0)) == (0, 0, 0, 0)
    assert atlas.getchannel("A").getextrema()[1] == 255


def test_edge_connected_color_variation_cannot_distort_frame_alignment() -> None:
    def noisy_opaque_strip(subject: tuple[int, int, int]) -> bytes:
        image = Image.new("RGB", (72, 24), (246, 3, 232))
        draw = ImageDraw.Draw(image)
        for index in range(3):
            left = index * 24
            draw.rectangle((left + 8, 5, left + 15, 21), fill=subject)
            # These pixels are outside the background color tolerance but remain
            # connected to an edge, matching provider gradient remnants.
            image.putpixel((left, 23), (232, 34, 213))
        return _png(image)

    atlas = _open(
        build_resident_sprite_atlas(
            noisy_opaque_strip((220, 30, 30)),
            noisy_opaque_strip((30, 180, 60)),
            noisy_opaque_strip((30, 80, 220)),
        )
    )

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
            assert bbox[3] == 31
            assert abs((bbox[0] + bbox[2]) - FRAME_SIZE) <= 1
    assert inspect_resident_sprite_atlas(_png(atlas)) == []


def test_large_edge_connected_subject_is_not_silently_deleted() -> None:
    image = Image.new("RGBA", (72, 24), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for index in range(3):
        draw.rectangle((index * 24, 5, index * 24 + 8, 20), fill=(40, 120, 220, 255))

    atlas = _open(build_resident_sprite_atlas(*([_png(image)] * 3)))

    for column in range(3):
        frame = atlas.crop((column * FRAME_SIZE, 0, (column + 1) * FRAME_SIZE, FRAME_SIZE))
        assert frame.getchannel("A").getbbox() is not None


def test_empty_frame_is_rejected() -> None:
    empty = _png(Image.new("RGBA", (72, 24), (0, 0, 0, 0)))
    with pytest.raises(ResidentSpritePostprocessError) as error:
        build_resident_sprite_atlas(empty, empty, empty)
    assert error.value.code == "FRAME_EMPTY"


def test_oversized_input_is_rejected_before_decode(monkeypatch, strips) -> None:
    monkeypatch.setattr(postprocess, "MAX_INPUT_BYTES", 3)
    with pytest.raises(ResidentSpritePostprocessError) as error:
        build_resident_sprite_atlas(b"four", strips[1], strips[2])
    assert error.value.code == "INPUT_TOO_LARGE"


@pytest.mark.parametrize(
    ("invalid", "code"),
    [
        (b"not an image", "INPUT_NOT_PNG"),
        (_png(Image.new("RGBA", (71, 24))), "STRIP_DIMENSIONS_INVALID"),
        (_png(Image.new("RGBA", (72, 23))), "STRIP_DIMENSIONS_INVALID"),
    ],
)
def test_invalid_strip_is_rejected(invalid: bytes, code: str, strips) -> None:
    with pytest.raises(ResidentSpritePostprocessError) as error:
        build_resident_sprite_atlas(invalid, strips[1], strips[2])
    assert error.value.code == code


def test_output_is_byte_for_byte_deterministic(strips) -> None:
    first = build_resident_sprite_atlas(*strips)
    second = build_resident_sprite_atlas(*strips)
    assert first == second


def test_generated_right_output_is_byte_for_byte_deterministic(strips) -> None:
    generated_right = _strip((190, 40, 210, 255), high_color=True)
    first = build_resident_sprite_atlas(*strips, right_strip_png=generated_right)
    second = build_resident_sprite_atlas(*strips, right_strip_png=generated_right)
    assert first == second


def test_portrait_uses_down_facing_idle_frame(strips) -> None:
    atlas_bytes = build_resident_sprite_atlas(*strips)
    atlas = _open(atlas_bytes)
    portrait = _open(derive_resident_portrait(atlas_bytes, size=64))

    expected = atlas.crop((FRAME_SIZE, 0, FRAME_SIZE * 2, FRAME_SIZE)).resize(
        (64, 64), Image.Resampling.NEAREST
    )
    assert portrait.format == "PNG"
    assert portrait.mode == "RGBA"
    assert portrait.size == (64, 64)
    assert portrait.tobytes() == expected.tobytes()


def test_portrait_rejects_wrong_atlas_dimensions(strips) -> None:
    with pytest.raises(ResidentSpritePostprocessError) as error:
        derive_resident_portrait(strips[0])
    assert error.value.code == "ATLAS_DIMENSIONS_INVALID"
