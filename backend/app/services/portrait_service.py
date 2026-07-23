"""AI portrait generation via an OpenAI-compatible image model, then pixel-art
post-processing (generate the image first, then snap it onto a pixel grid)."""
import base64
import logging
from pathlib import Path

from app.http import get_client

from app.config import settings

logger = logging.getLogger(__name__)


def _portrait_dir() -> Path:
    """Portraits live under the served static root (see app.main /static mount)."""
    return Path(settings.static_dir) / "portraits"


# Kept as a module-level alias for backwards compatibility with tests/imports.
PORTRAIT_DIR = _portrait_dir()


def build_portrait_prompt(name: str, persona_md: str) -> str:
    """Build image generation prompt from character name and persona description."""
    # Extract appearance hints from persona
    appearance_hints = ""
    if persona_md:
        lines = persona_md.split("\n")
        for line in lines:
            lower = line.lower()
            if any(kw in lower for kw in ["外貌", "appearance", "穿", "wear", "发", "hair", "眼", "eye"]):
                appearance_hints += line.strip() + " "
        appearance_hints = appearance_hints[:300]  # cap length

    if not appearance_hints:
        appearance_hints = "a cyberpunk city character with distinct personality"

    return (
        f"Pixel art game sprite of a character named '{name}'. "
        f"Character traits: {appearance_hints}. "
        "STYLE: authentic retro 2D pixel art, like a 16-bit SNES RPG character sprite. "
        "Full-body chibi character, standing, facing the viewer, about 2.5 heads tall "
        "with a big head and small body. Large chunky visible square pixels, flat "
        "colors with simple two-tone shading, crisp 1-pixel dark outlines, a limited "
        "palette of at most 16 colors, subtle cyberpunk neon accents. "
        "COMPOSITION: exactly one character, centered, filling about 90% of the "
        "image height. "
        "BACKGROUND: solid uniform magenta (#FF00FF), completely flat, no gradient, "
        "no floor, no ground shadow, no border. "
        "Do not include any text, watermark, logo, frame, UI, or extra objects."
    )


def save_portrait_image(resident_id: str, image_bytes: bytes) -> str:
    """Save portrait image to disk and return URL path."""
    portrait_dir = _portrait_dir()
    portrait_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{resident_id}.png"
    filepath = portrait_dir / filename
    filepath.write_bytes(image_bytes)
    return f"/static/portraits/{filename}"


async def generate_portrait(
    resident_id: str,
    name: str,
    persona_md: str,
) -> str | None:
    """Generate AI portrait via Gemini. Returns URL path or None on failure."""
    prompt = build_portrait_prompt(name, persona_md)

    base_url = settings.portrait_llm_base_url
    api_key = settings.portrait_llm_api_key
    model = settings.portrait_llm_model or "gpt-image-2"
    timeout = settings.portrait_llm_timeout or 60

    if not base_url or not api_key:
        logger.warning("Portrait LLM not configured (PORTRAIT_LLM_BASE_URL / PORTRAIT_LLM_API_KEY)")
        return None

    try:
        # gpt-image-2 (and the other image models on this OpenAI-compatible
        # endpoint) are native image models — they answer the Images
        # Generations API, not chat/completions. Generate the raw image with
        # the configured `model` here; the pixel-grid conversion is the
        # separate post-processing step below ("先生成图像，再转像素图").
        response = await get_client().post(
            f"{base_url}/images/generations",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "prompt": prompt,
                "n": 1,
                "size": "1024x1024",
            },
            timeout=timeout,
        )

        if response.status_code != 200:
            logger.error(
                "Portrait image API returned %d: %s",
                response.status_code,
                response.text[:300],
            )
            return None

        data = response.json()

        # OpenAI Images API shape: {"data": [{"b64_json": ...}]} or
        # {"data": [{"url": ...}]}. Prefer inline base64; fall back to fetching
        # the URL when the proxy returns a link instead of bytes.
        image_data = None
        items = data.get("data") or []
        if items:
            item = items[0]
            b64 = item.get("b64_json")
            if b64:
                image_data = base64.b64decode(b64)
            elif item.get("url"):
                img_resp = await get_client().get(item["url"], timeout=timeout)
                if img_resp.status_code == 200:
                    image_data = img_resp.content

        if not image_data:
            logger.error(
                "Could not extract image from portrait response: %s",
                str(data)[:300],
            )
            return None

        # Snap the raw AI render onto a true pixel grid (Image-to-Pixel style:
        # downsample + palette quantization + transparent backdrop). On any
        # failure keep the raw image rather than losing the portrait.
        try:
            from app.services.pixelate_service import pixelate_image

            image_data = pixelate_image(
                image_data,
                grid=settings.portrait_pixel_grid,
                colors=settings.portrait_pixel_colors,
            )
        except Exception:
            logger.warning(
                "Pixelation failed for %s; saving raw portrait", resident_id,
                exc_info=True,
            )

        return save_portrait_image(resident_id, image_data)

    except Exception as e:
        logger.error("Portrait generation failed for %s: %s", resident_id, e, exc_info=True)
        return None
