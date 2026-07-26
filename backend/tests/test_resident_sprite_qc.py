from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image, ImageDraw

import app.services.resident_sprite_qc as qc
from app.services.resident_sprite_qc import inspect_resident_sprite_atlas, qc_passed


def _encoded(image: Image.Image, *, format: str = "PNG") -> bytes:
    output = BytesIO()
    image.save(output, format=format)
    return output.getvalue()


def _clean_atlas() -> Image.Image:
    atlas = Image.new("RGBA", qc.ATLAS_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(atlas)
    colors = (
        (220, 30, 30, 255),
        (30, 180, 60, 255),
        (30, 80, 220, 255),
        (240, 210, 40, 255),
    )
    for row in (0, 1, 3):
        for column in range(3):
            x = column * qc.FRAME_SIZE
            y = row * qc.FRAME_SIZE
            draw.rectangle((x + 11, y + 10, x + 20, y + 29), fill=colors[row])
            draw.point((x + 11, y + 12), fill=(255, 255, 255, 255))

    for column in range(3):
        left = atlas.crop(
            (
                column * qc.FRAME_SIZE,
                qc.FRAME_SIZE,
                (column + 1) * qc.FRAME_SIZE,
                qc.FRAME_SIZE * 2,
            )
        )
        atlas.alpha_composite(
            left.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
            (column * qc.FRAME_SIZE, qc.FRAME_SIZE * 2),
        )
    return atlas


def _codes(image_or_bytes: Image.Image | bytes) -> list[str]:
    data = image_or_bytes if isinstance(image_or_bytes, bytes) else _encoded(image_or_bytes)
    return [finding.code for finding in inspect_resident_sprite_atlas(data)]


def _replace_frame(atlas: Image.Image, index: int, frame: Image.Image) -> None:
    row, column = divmod(index, 3)
    atlas.paste(frame, (column * qc.FRAME_SIZE, row * qc.FRAME_SIZE))


def test_clean_atlas_passes_deterministically() -> None:
    data = _encoded(_clean_atlas())

    first = inspect_resident_sprite_atlas(data)
    second = inspect_resident_sprite_atlas(data)

    assert first == second == []
    assert qc_passed(first)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", "ATLAS_INPUT_EMPTY"),
        (b"not an image", "ATLAS_DECODE_FAILED"),
        (_encoded(Image.new("RGB", qc.ATLAS_SIZE), format="JPEG"), "ATLAS_FORMAT_INVALID"),
        (_encoded(Image.new("RGB", qc.ATLAS_SIZE)), "ATLAS_MODE_INVALID"),
        (_encoded(Image.new("RGBA", (95, 128))), "ATLAS_DIMENSIONS_INVALID"),
    ],
)
def test_invalid_structure_returns_stable_finding(data: bytes, expected: str) -> None:
    findings = inspect_resident_sprite_atlas(data)

    assert [finding.code for finding in findings] == [expected]
    assert not qc_passed(findings)


def test_non_bytes_and_oversized_input_are_findings(monkeypatch) -> None:
    findings = inspect_resident_sprite_atlas(None)  # type: ignore[arg-type]
    assert [finding.code for finding in findings] == ["ATLAS_INPUT_INVALID"]
    monkeypatch.setattr(qc, "MAX_ATLAS_BYTES", 3)
    assert _codes(b"four") == ["ATLAS_INPUT_TOO_LARGE"]


def test_empty_frame_is_reported() -> None:
    atlas = _clean_atlas()
    _replace_frame(atlas, 0, Image.new("RGBA", (32, 32), (0, 0, 0, 0)))

    assert "FRAME_EMPTY" in _codes(atlas)


def test_frame_edge_contact_is_reported() -> None:
    atlas = _clean_atlas()
    atlas.putpixel((0, 15), (220, 30, 30, 255))

    assert "FRAME_EDGE_CONTACT" in _codes(atlas)


def test_baseline_drift_is_reported() -> None:
    atlas = _clean_atlas()
    frame = atlas.crop((0, 0, 32, 32))
    shifted = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    shifted.alpha_composite(frame, (0, -3))
    _replace_frame(atlas, 0, shifted)

    assert "BASELINE_DRIFT" in _codes(atlas)


def test_frame_size_drift_is_reported() -> None:
    atlas = _clean_atlas()
    frame = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rectangle((15, 10, 16, 29), fill=(220, 30, 30, 255))
    _replace_frame(atlas, 0, frame)

    assert "FRAME_SIZE_DRIFT" in _codes(atlas)


def test_direction_profile_and_natural_stride_widths_are_allowed() -> None:
    atlas = _clean_atlas()
    for column, width in enumerate((16, 10, 15)):
        left_frame = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        left = (32 - width) // 2
        ImageDraw.Draw(left_frame).rectangle(
            (left, 10, left + width - 1, 29), fill=(30, 180, 60, 255)
        )
        _replace_frame(atlas, 3 + column, left_frame)
        _replace_frame(
            atlas,
            6 + column,
            left_frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT),
        )

    assert "FRAME_SIZE_DRIFT" not in _codes(atlas)


def test_frame_center_drift_is_reported() -> None:
    atlas = _clean_atlas()
    frame = atlas.crop((0, 0, 32, 32))
    shifted = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    shifted.alpha_composite(frame, (4, 0))
    _replace_frame(atlas, 0, shifted)

    assert "FRAME_CENTER_DRIFT" in _codes(atlas)


def test_vertical_center_variation_with_aligned_baselines_is_not_center_drift() -> None:
    atlas = _clean_atlas()
    frame = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rectangle((11, 6, 20, 29), fill=(220, 30, 30, 255))
    _replace_frame(atlas, 0, frame)

    assert "FRAME_CENTER_DRIFT" not in _codes(atlas)


def test_palette_limit_is_reported() -> None:
    atlas = _clean_atlas()
    for index in range(qc.MAX_PALETTE_COLORS + 1):
        x = 11 + index % 10
        y = 10 + index // 10
        atlas.putpixel((x, y), (index, 100, 200, 255))

    assert "PALETTE_LIMIT_EXCEEDED" in _codes(atlas)


def test_right_frames_must_be_exact_left_mirrors() -> None:
    atlas = _clean_atlas()
    atlas.putpixel((20, 2 * qc.FRAME_SIZE + 12), (1, 2, 3, 255))

    assert "RIGHT_MIRROR_MISMATCH" in _codes(atlas)


def test_generate_right_allows_independent_right_frames_but_runs_other_checks() -> None:
    atlas = _clean_atlas()
    atlas.putpixel((20, 2 * qc.FRAME_SIZE + 12), (1, 2, 3, 255))
    atlas.putpixel((0, 2 * qc.FRAME_SIZE + 15), (30, 80, 220, 255))

    findings = inspect_resident_sprite_atlas(
        _encoded(atlas), direction_policy="generate_right"
    )
    codes = [finding.code for finding in findings]

    assert "RIGHT_MIRROR_MISMATCH" not in codes
    assert "FRAME_EDGE_CONTACT" in codes


def test_invalid_direction_policy_returns_stable_finding() -> None:
    findings = inspect_resident_sprite_atlas(
        _encoded(_clean_atlas()), direction_policy="unknown"  # type: ignore[arg-type]
    )

    assert [finding.code for finding in findings] == ["DIRECTION_POLICY_INVALID"]


def test_unexpected_pillow_failure_becomes_stable_finding(monkeypatch) -> None:
    monkeypatch.setattr(qc, "_inspect_decoded_atlas", lambda *_, **__: 1 / 0)

    assert _codes(_clean_atlas()) == ["ATLAS_QC_FAILED"]
