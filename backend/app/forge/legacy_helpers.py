"""Pure text helpers for the legacy forge pipeline (no I/O, no LLM calls).

Moved verbatim from app/services/forge_service.py (P1-6 file split).
"""

import re


def _extract_text(response) -> str:
    """Extract text from LLM response, skipping ThinkingBlocks (extended thinking)."""
    for block in response.content:
        if hasattr(block, "text"):
            return block.text
    return ""


def _parse_combined_output(session: dict, text: str) -> None:
    """Fallback parser if ===SPLIT=== didn't work — split by top-level headers."""
    ability_start = text.find("# 能力")
    persona_start = text.find("# 人格")
    soul_start = text.find("# 灵魂")

    if ability_start >= 0 and persona_start > ability_start:
        session["ability_md"] = text[ability_start:persona_start].strip()
    if persona_start >= 0 and soul_start > persona_start:
        session["persona_md"] = text[persona_start:soul_start].strip()
    if soul_start >= 0:
        session["soul_md"] = text[soul_start:].strip()


def _compute_star_rating_fallback(ability_md: str, persona_md: str, soul_md: str) -> int:
    total_len = len(ability_md) + len(persona_md) + len(soul_md)
    sections = 0
    for md in [ability_md, persona_md, soul_md]:
        headers = re.findall(r'^##\s+.+', md, re.MULTILINE)
        for h in headers:
            idx = md.index(h)
            after = md[idx + len(h):idx + len(h) + 200]
            if after.strip() and "暂无" not in after[:100] and "待补充" not in after[:100]:
                sections += 1
    empty_markers = sum(md.count("暂无") + md.count("待补充") for md in [ability_md, persona_md, soul_md])

    if sections < 3 or empty_markers > 5 or (total_len < 300 and sections < 2):
        return 1
    elif sections >= 10 and empty_markers <= 1:
        return 3
    else:
        return 2


def _extract_role(ability_md: str) -> str:
    match = re.search(r'#\s*能力概览\s*\n+(.+)', ability_md)
    if match:
        return match.group(1).strip()[:50]
    for line in ability_md.split('\n'):
        line = line.strip()
        if line and not line.startswith('#'):
            return line[:50]
    return "居民"


def _extract_impression(persona_md: str) -> str:
    match = re.search(r'Layer\s*0[^\n]*\n+([\s\S]*?)(?=\n##|\Z)', persona_md)
    if match:
        text = match.group(1).strip()
        bullet = re.search(r'-\s*\*\*(.+?)\*\*', text)
        if bullet:
            return bullet.group(1).strip()[:50]
        lines = [l.strip() for l in text.split('\n') if l.strip() and not l.strip().startswith('#')]
        if lines:
            return lines[0][:50]
    return "新入住的居民"
