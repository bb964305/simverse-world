"""Byte-versioned prompt templates for resident-sprite-v1."""
from __future__ import annotations

from html import escape


PROMPT_VERSION = "resident-sprite-v1"

ANCHOR_TEMPLATE = """TASK
Create one original full-body resident character to serve as the immutable identity reference for a top-down village walking sprite.

RESIDENT DESCRIPTION
<resident_description>
{appearance}
</resident_description>

MUST KEEP
- Exactly one character, fully visible from hair/headwear through both feet.
- Original 16-bit-inspired chibi pixel-art design: large readable head, compact body, simple high-contrast shapes, restrained detail that remains legible at 32x32.
- Neutral front/down-facing standing pose, arms relaxed, feet separated enough to read.
- Define a distinctive but simple face, hair/headwear, outfit, accessory, silhouette, and limited color palette. These are immutable identity features for later edits.
- Flat, uniform, opaque #FF00FF background touching every image edge.

MUST NOT INCLUDE
- No second character, alternate pose, panel, grid, border, caption, letters, watermark, scenery, floor, cast shadow, glow, or cropped body part.
- Do not imitate a named artist, franchise, game, celebrity, or supplied third-party resident asset.

OUTPUT
One centered square image with generous clear space around the character."""

DIRECTION_TEMPLATE = """REFERENCE AUTHORITY
Input image 1 is the immutable identity and style anchor. Keep the exact same character identity, face, hair/headwear, outfit, accessory, body proportions, silhouette language, and color palette in every panel.

TASK
Create exactly one horizontal strip of three equal portrait panels showing the same character walking {direction_label}. The full output is 1536x1024, so each panel occupies exactly 512x1024 with no gaps or borders.

PANEL ORDER
1. Left foot forward, opposite arm forward.
2. Neutral passing/idle pose with both feet under the body.
3. Right foot forward, opposite arm forward.

MUST KEEP
- Only pose and the requested facing direction may change from the reference.
- Exactly one complete character per panel, centered at identical scale.
- Head height, body height, outfit geometry, accessory placement, and apparent camera angle remain consistent.
- All six feet positions share one visual ground baseline across the strip.
- Original 16-bit-inspired chibi pixel-art shapes readable after reduction to 32x32.
- Every panel has the same flat, uniform, opaque #FF00FF background touching all outer and panel edges.

MUST NOT INCLUDE
- No extra poses, duplicate body parts, panel border, gutter, grid line, caption, letters, watermark, scenery, floor, cast shadow, glow, motion trail, or cropped body part.
- Do not redesign, recolor, simplify, elaborate, or reinterpret the character.

OUTPUT
One clean three-panel strip only. Panel boundaries are at x=512 and x=1024."""

ONESHOT_DRAFT_TEMPLATE = """TASK
Create one original top-down village resident as an exact 3-column by 4-row walking sprite sheet. The full output is 1024x1536 with no gaps or borders.

RESIDENT DESCRIPTION
<resident_description>
{appearance}
</resident_description>

ROW AND COLUMN ORDER
- Row 1: facing DOWN. Row 2: facing LEFT. Row 3: facing RIGHT. Row 4: facing UP.
- Column 1: left foot forward. Column 2: neutral passing/idle. Column 3: right foot forward.

MUST KEEP
- Exactly the same single character identity, face, hair/headwear, outfit, accessory, proportions, silhouette language, and limited color palette in all twelve cells.
- Exactly one complete centered character per cell at identical scale and camera angle.
- Every pose shares one ground baseline within its cell.
- Original 16-bit-inspired chibi pixel-art shapes readable after reduction to 32x32.
- Every cell has the same flat, uniform, opaque #FF00FF background.

MUST NOT INCLUDE
- No extra pose, duplicate body part, grid line, border, gutter, caption, letters, watermark, scenery, floor, shadow, glow, motion trail, or cropped body part.
- Do not imitate a named artist, franchise, game, celebrity, or supplied third-party resident asset.

OUTPUT
One 1024x1536 sheet only. Arrange evenly spaced three columns and four rows with consistent outer margins and no visible grid lines."""

DIRECTION_LABELS = {"DOWN": "down", "LEFT": "left", "RIGHT": "right", "UP": "up"}

CORRECTION_CLAUSES = {
    "SRC_DIMENSIONS": "source dimensions were incorrect; return exactly 1536x1024.",
    "SRC_ALPHA": "the source contained transparency; use a fully opaque #FF00FF background.",
    "CELL_EMPTY": "a panel contained no character; include one complete character in every panel.",
    "CELL_EDGE_TOUCH": "a pose touched its cell edge; preserve every invariant and add clear space around that pose.",
    "COMPONENT_FRAGMENT": "detached fragments were excessive; keep each character and accessory visibly connected.",
    "AREA_LOW": "a pose was too small; preserve identity and increase it to the requested consistent scale.",
    "AREA_HIGH": "a pose was too large; preserve identity and add clear space around it.",
    "ROW_AREA_DRIFT": "pose sizes varied too much; keep all three panels at identical scale.",
    "SILHOUETTE_IOU_LOW": "a walking pose changed silhouette excessively; preserve proportions and change only the gait.",
    "HEIGHT_DRIFT": "character heights varied across panels; keep exact body proportions and scale.",
    "BG_RESIDUE": "background residue remained; use one flat uniform opaque #FF00FF background.",
}


def _escaped_appearance(appearance: str) -> str:
    return escape(appearance, quote=True).replace("&#x27;", "&#39;")


def render_anchor_prompt(appearance: str) -> str:
    return ANCHOR_TEMPLATE.format(appearance=_escaped_appearance(appearance))


def render_direction_prompt(direction: str, correction_code: str | None = None) -> str:
    try:
        rendered = DIRECTION_TEMPLATE.format(direction_label=DIRECTION_LABELS[direction])
    except KeyError as exc:
        raise ValueError("direction must be DOWN, LEFT, RIGHT, or UP") from exc
    if correction_code is not None:
        try:
            clause = CORRECTION_CLAUSES[correction_code]
        except KeyError as exc:
            raise ValueError("correction code is not allowlisted") from exc
        rendered += f"\n\nCORRECTION: {clause}"
    return rendered


def render_qualification_oneshot_prompt(appearance: str) -> str:
    """Render the draft-only prompt; normal generation has no call path here."""
    return ONESHOT_DRAFT_TEMPLATE.format(appearance=_escaped_appearance(appearance))
