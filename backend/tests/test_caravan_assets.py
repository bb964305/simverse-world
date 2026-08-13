from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = REPO_ROOT / "frontend/public/assets/village/caravan"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _provenance(directory: Path) -> dict:
    return json.loads((directory / "generation-provenance.json").read_text())


def test_committed_caravan_rasters_are_true_transparent_limited_palette_pngs() -> None:
    expected = {
        "merchant/texture.png": (96, 128),
        "merchant/portrait.png": (256, 256),
        "convoy/texture.png": (192, 256),
        "stall/texture.png": (64, 64),
    }
    for relative, size in expected.items():
        path = ASSET_ROOT / relative
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.mode == "RGBA"
            assert image.size == size
            alpha = image.getchannel("A")
            assert set(alpha.get_flattened_data()) == {0, 255}
            assert image.getpixel((0, 0))[3] == 0
            colors = image.getcolors(maxcolors=image.width * image.height)
            assert colors is not None and len(colors) <= 32
            assert not any(
                red > 180 and blue > 120 and green < 80 and alpha
                for _, (red, green, blue, alpha) in colors
            ), f"{relative} still contains opaque chroma-key spill"


def test_caravan_provenance_matches_every_committed_raster() -> None:
    for directory_name in ("merchant", "convoy", "stall"):
        directory = ASSET_ROOT / directory_name
        provenance = _provenance(directory)
        assert provenance["generator"] == "Codex built-in ImageGen"
        assert provenance["rights_basis"] == "first_party_generated"
        assert provenance["prompt_contract"]
        assert provenance["generated_sources"]
        for record in provenance["files"]:
            path = directory / record["file"]
            assert path.stat().st_size == record["bytes"]
            assert _sha256(path) == record["sha256"]
            with Image.open(path) as image:
                assert image.size == (record["width"], record["height"])
                assert image.mode == record["mode"]
