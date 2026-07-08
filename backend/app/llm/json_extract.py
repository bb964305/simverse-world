"""Robust JSON-object extraction from LLM output (P1-1, E-05).

Models wrap JSON in prose and markdown code fences, and emit nested objects and
arrays. The old ad-hoc extractors (`re.search(r'\\{[^{}]+\\}')`,
`re.search(r'\\{[^}]+\\}')`, `find('{')..rfind('}')`) each fail on a different
class of real output: the `[^{}]` variants cannot match any nested object at
all, and the greedy/outermost variants swallow trailing prose braces. E-05
showed this causes silently-mispaid calls that fall back to defaults.

`extract_json_object` unifies the strategy for every call site:
1. strip markdown code fences,
2. scan from the first `{` counting brace depth while respecting string
   literals + escapes, to isolate the first *balanced* object,
3. json.loads it, retrying once after stripping trailing commas.

Returns the parsed dict, or None if no balanced JSON object is present.
"""
from __future__ import annotations

import json
import re

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_FENCE_LINE = re.compile(r"^\s*```[^\n]*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code-fence lines (```json, ```, ...) but keep the body."""
    return _FENCE_LINE.sub("", text)


def _first_balanced_object(text: str) -> str | None:
    """Return the substring of the first brace-balanced ``{...}`` object.

    Braces inside string literals do not count; backslash escapes inside strings
    are honoured. Returns None if there is no `{` or it never balances.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def extract_json_object(text: str | None) -> dict | None:
    """Extract the first balanced JSON object from LLM output, or None.

    Tolerant of surrounding prose, markdown code fences and trailing commas.
    Only returns a value when the parsed result is a JSON object (dict).
    """
    if not text:
        return None
    candidate = _first_balanced_object(_strip_code_fences(text))
    if candidate is None:
        return None
    for attempt in (candidate, _TRAILING_COMMA.sub(r"\1", candidate)):
        try:
            parsed = json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None
