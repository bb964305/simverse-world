"""Deterministic, offline quality checks for final resident sprite atlases."""
from __future__ import annotations

from io import BytesIO
from typing import Literal, Sequence

from PIL import Image, UnidentifiedImageError

from app.services.resident_sprite_generation import QCFinding


FRAME_SIZE = 32
ATLAS_SIZE = (96, 128)
MAX_ATLAS_BYTES = 1024 * 1024
MAX_PALETTE_COLORS = 32
MAX_BASELINE_DRIFT = 1
MAX_FRAME_WIDTH_DRIFT = 6
MAX_FRAME_HEIGHT_DRIFT = 4
MAX_CENTER_DRIFT = 2.0

_DIRECTIONS = ("down", "left", "right", "up")


def _finding(code: str, detail: str) -> QCFinding:
    return QCFinding(code=code, detail=detail)


def _frame_label(index: int) -> str:
    row, column = divmod(index, 3)
    return f"{_DIRECTIONS[row]}[{column}]"


def _decode_atlas(atlas_png: bytes) -> tuple[Image.Image | None, list[QCFinding]]:
    if not isinstance(atlas_png, bytes):
        return None, [_finding("ATLAS_INPUT_INVALID", "atlas input must be PNG bytes")]
    if not atlas_png:
        return None, [_finding("ATLAS_INPUT_EMPTY", "atlas input is empty")]
    if len(atlas_png) > MAX_ATLAS_BYTES:
        return None, [_finding("ATLAS_INPUT_TOO_LARGE", "atlas input exceeds 1 MiB")]

    try:
        with Image.open(BytesIO(atlas_png)) as source:
            if source.format != "PNG":
                return None, [_finding("ATLAS_FORMAT_INVALID", "atlas image format must be PNG")]
            if getattr(source, "n_frames", 1) != 1:
                return None, [_finding("ATLAS_ANIMATED", "atlas PNG must contain one image")]
            if source.mode != "RGBA":
                return None, [_finding("ATLAS_MODE_INVALID", "atlas PNG mode must be RGBA")]
            if source.size != ATLAS_SIZE:
                return None, [
                    _finding(
                        "ATLAS_DIMENSIONS_INVALID",
                        f"atlas dimensions must be {ATLAS_SIZE[0]}x{ATLAS_SIZE[1]}",
                    )
                ]
            source.load()
            return source.copy(), []
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None, [_finding("ATLAS_DECODE_FAILED", "atlas input is not a valid PNG image")]


def _inspect_decoded_atlas(
    atlas: Image.Image, *, direction_policy: Literal["mirror_right", "generate_right"]
) -> list[QCFinding]:
    findings: list[QCFinding] = []
    frames = [
        atlas.crop(
            (
                column * FRAME_SIZE,
                row * FRAME_SIZE,
                (column + 1) * FRAME_SIZE,
                (row + 1) * FRAME_SIZE,
            )
        )
        for row in range(4)
        for column in range(3)
    ]
    bboxes = [frame.getchannel("A").getbbox() for frame in frames]
    empty_labels = [_frame_label(index) for index, bbox in enumerate(bboxes) if bbox is None]
    if empty_labels:
        findings.append(
            _finding("FRAME_EMPTY", f"empty frames: {', '.join(empty_labels)}")
        )

    edge_labels: list[str] = []
    populated: list[tuple[int, tuple[int, int, int, int]]] = []
    for index, bbox in enumerate(bboxes):
        if bbox is None:
            continue
        populated.append((index, bbox))
        left, top, right, bottom = bbox
        if left == 0 or top == 0 or right == FRAME_SIZE or bottom == FRAME_SIZE:
            edge_labels.append(_frame_label(index))
    if edge_labels:
        findings.append(
            _finding(
                "FRAME_EDGE_CONTACT",
                f"opaque content touches a frame edge: {', '.join(edge_labels)}",
            )
        )

    if len(populated) >= 2:
        baselines = [bbox[3] - 1 for _, bbox in populated]
        if max(baselines) - min(baselines) > MAX_BASELINE_DRIFT:
            findings.append(
                _finding(
                    "BASELINE_DRIFT",
                    f"frame foot baselines drift by more than {MAX_BASELINE_DRIFT}px",
                )
            )

        unstable_directions: list[str] = []
        for row, direction in enumerate(_DIRECTIONS):
            direction_boxes = [bbox for index, bbox in populated if index // 3 == row]
            if len(direction_boxes) < 2:
                continue
            widths = [bbox[2] - bbox[0] for bbox in direction_boxes]
            heights = [bbox[3] - bbox[1] for bbox in direction_boxes]
            if (
                max(widths) - min(widths) > MAX_FRAME_WIDTH_DRIFT
                or max(heights) - min(heights) > MAX_FRAME_HEIGHT_DRIFT
            ):
                unstable_directions.append(direction)
        if unstable_directions:
            findings.append(
                _finding(
                    "FRAME_SIZE_DRIFT",
                    "frame size is unstable within directions: "
                    + ", ".join(unstable_directions),
                )
            )

        center_x_twice = [bbox[0] + bbox[2] for _, bbox in populated]
        if max(center_x_twice) - min(center_x_twice) > MAX_CENTER_DRIFT * 2:
            findings.append(
                _finding(
                    "FRAME_CENTER_DRIFT",
                    f"frame horizontal centers drift by more than {MAX_CENTER_DRIFT:g}px",
                )
            )

    colors = atlas.getcolors(maxcolors=MAX_PALETTE_COLORS)
    if colors is None:
        findings.append(
            _finding(
                "PALETTE_LIMIT_EXCEEDED",
                f"atlas uses more than {MAX_PALETTE_COLORS} RGBA colors",
            )
        )

    if direction_policy == "mirror_right":
        mirror_mismatches: list[str] = []
        for column in range(3):
            left_frame = frames[3 + column]
            expected_right = left_frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if frames[6 + column].tobytes() != expected_right.tobytes():
                mirror_mismatches.append(str(column))
        if mirror_mismatches:
            findings.append(
                _finding(
                    "RIGHT_MIRROR_MISMATCH",
                    "right frames are not exact horizontal mirrors of left frames: "
                    + ", ".join(mirror_mismatches),
                )
            )

    return findings


def inspect_resident_sprite_atlas(
    atlas_png: bytes,
    *,
    direction_policy: Literal["mirror_right", "generate_right"] = "mirror_right",
) -> list[QCFinding]:
    """Return stable QC findings for an atlas; malformed input never escapes."""
    if direction_policy not in {"mirror_right", "generate_right"}:
        return [
            _finding(
                "DIRECTION_POLICY_INVALID",
                "direction policy must be mirror_right or generate_right",
            )
        ]
    try:
        atlas, findings = _decode_atlas(atlas_png)
        if atlas is None:
            return findings
        return _inspect_decoded_atlas(atlas, direction_policy=direction_policy)
    except Exception:
        return [_finding("ATLAS_QC_FAILED", "atlas quality checks could not be completed")]


def qc_passed(findings: Sequence[QCFinding]) -> bool:
    """Return whether a previously inspected atlas has no QC findings."""
    return len(findings) == 0
