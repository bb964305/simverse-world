"""Safe Skill archive parsing, format detection, and 3-layer conversion."""
import io
import json
import re
import time
import zipfile
from enum import Enum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.llm.client import get_client
from app.llm.metering import estimate_tokens, record_usage
from app.config import settings


IMPORT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024
# Multipart framing, the bounded name/slug fields, and ordinary client headers
# need a little room beyond the file itself.  The ASGI middleware applies this
# cap before Starlette parses/spools the upload.
IMPORT_MAX_MULTIPART_BODY_BYTES = IMPORT_MAX_UPLOAD_BYTES + 128 * 1024
IMPORT_MAX_ZIP_MEMBERS = 20
IMPORT_MAX_ZIP_MEMBER_BYTES = 512 * 1024
IMPORT_MAX_ZIP_TOTAL_BYTES = 1024 * 1024
IMPORT_MAX_ZIP_COMPRESSION_RATIO = 100
IMPORT_MAX_META_BYTES = 16 * 1024
IMPORT_MAX_NAME_CHARS = 100
IMPORT_MAX_SLUG_CHARS = 100
IMPORT_MAX_LAYER_CHARS = 32_000
IMPORT_MAX_TOTAL_LAYER_CHARS = 64_000
IMPORT_CONVERSION_MAX_INPUT_CHARS = 8_000

# None of these namespaces may cross the user-upload trust boundary.  Some are
# current privilege consumers; the rest prevent an import from forging server
# provenance or a precomputed personality block when SBTI is skipped.
_FORBIDDEN_META_KEYS = frozenset({
    "_server_privilege_grants",
    "creator_id",
    "duty",
    "lab",
    "mayor",
    "origin",
    "prompt_hint",
    "reputation",
    "resident_type",
    "sbti",
})


class SkillImportValidationError(ValueError):
    """A user-correctable import error with an HTTP-friendly status code."""

    def __init__(self, detail: str, *, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class _ImportProfile(BaseModel):
    """Compatibility shape used by older Skill packages."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    role: str | None = Field(default=None, max_length=100)


class _ImportMeta(BaseModel):
    """Only harmless display metadata is accepted from an uploaded package."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    role: str | None = Field(default=None, max_length=100)
    profile: _ImportProfile | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, tags: list[str]) -> list[str]:
        clean = []
        for tag in tags:
            if not isinstance(tag, str):
                raise ValueError("tags must contain strings")
            value = tag.strip()
            if len(value) > 50:
                raise ValueError("tag is too long")
            if value:
                clean.append(value)
        return clean


def validate_import_identity(name: str, slug: str) -> tuple[str, str]:
    """Validate values before any parsing, placement, or LLM work."""
    clean_name = (name or "").strip()
    clean_slug = (slug or "").strip()
    if not clean_name:
        raise SkillImportValidationError("Name is required")
    if len(clean_name) > IMPORT_MAX_NAME_CHARS:
        raise SkillImportValidationError("Name is too long")
    if any(ord(ch) < 32 for ch in clean_name):
        raise SkillImportValidationError("Name contains control characters")
    if not clean_slug:
        raise SkillImportValidationError("Slug is required")
    if len(clean_slug) > IMPORT_MAX_SLUG_CHARS:
        raise SkillImportValidationError("Slug is too long")
    if not all(ch.isalnum() or ch in "-_" for ch in clean_slug):
        raise SkillImportValidationError(
            "Slug may contain only letters, numbers, hyphens, and underscores"
        )
    if not any(ch.isalnum() for ch in clean_slug):
        raise SkillImportValidationError("Slug must contain a letter or number")
    return clean_name, clean_slug


def _find_forbidden_meta_key(value, path: str = "meta", depth: int = 0) -> str | None:
    if depth > 32:
        raise SkillImportValidationError("meta.json nesting is too deep")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            child_path = f"{path}.{key}"
            if normalized in _FORBIDDEN_META_KEYS:
                return child_path
            found = _find_forbidden_meta_key(child, child_path, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_forbidden_meta_key(child, f"{path}[{index}]", depth + 1)
            if found:
                return found
    return None


def sanitize_import_meta(value) -> dict:
    """Return the strict, harmless subset of uploaded ``meta.json``.

    Known privilege/provenance namespaces are rejected instead of silently
    discarded so package authors get a clear error and exploit attempts remain
    visible in API logs.  Unknown legacy keys are ignored for compatibility;
    only the explicit output whitelist below is persisted.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SkillImportValidationError("meta.json must contain a JSON object")
    forbidden_path = _find_forbidden_meta_key(value)
    if forbidden_path:
        raise SkillImportValidationError(
            f"meta.json contains forbidden field: {forbidden_path}"
        )
    try:
        parsed = _ImportMeta.model_validate(value)
    except ValidationError as exc:
        raise SkillImportValidationError("meta.json contains invalid display metadata") from exc

    result: dict = {}
    role = parsed.role or (parsed.profile.role if parsed.profile else None)
    if role:
        result["role"] = role
    if parsed.tags:
        result["tags"] = parsed.tags
    return result


def validate_import_layers(layers: dict[str, str]) -> dict[str, str]:
    """Reject empty or overlong generated/uploaded three-layer documents."""
    normalized = {
        "ability_md": layers.get("ability_md") or "",
        "persona_md": layers.get("persona_md") or "",
        "soul_md": layers.get("soul_md") or "",
    }
    if not any(text.strip() for text in normalized.values()):
        raise SkillImportValidationError("Imported Skill contains no usable layer content")
    if any(len(text) > IMPORT_MAX_LAYER_CHARS for text in normalized.values()):
        raise SkillImportValidationError("A Skill layer exceeds the size limit", status_code=413)
    if sum(len(text) for text in normalized.values()) > IMPORT_MAX_TOTAL_LAYER_CHARS:
        raise SkillImportValidationError("Skill layer content exceeds the total size limit", status_code=413)
    return normalized


def _safe_member_map(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > IMPORT_MAX_ZIP_MEMBERS:
        raise SkillImportValidationError("Zip contains too many members", status_code=413)

    total_size = 0
    members: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        if info.is_dir():
            continue
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise SkillImportValidationError("Zip contains an unsafe member path")
        if info.flag_bits & 0x1:
            raise SkillImportValidationError("Encrypted zip members are not supported")
        if info.file_size < 0 or info.compress_size < 0:
            raise SkillImportValidationError("Zip contains invalid member sizes")
        if info.file_size > IMPORT_MAX_ZIP_MEMBER_BYTES:
            raise SkillImportValidationError("Zip member exceeds the size limit", status_code=413)
        total_size += info.file_size
        if total_size > IMPORT_MAX_ZIP_TOTAL_BYTES:
            raise SkillImportValidationError("Zip expands beyond the total size limit", status_code=413)
        if info.file_size and info.file_size / max(info.compress_size, 1) > IMPORT_MAX_ZIP_COMPRESSION_RATIO:
            raise SkillImportValidationError("Zip compression ratio exceeds the safety limit", status_code=413)

        basename = path.name.lower()
        if basename in {
            "ability.md", "work.md", "persona.md", "soul.md", "skill.md", "meta.json"
        }:
            if basename in members:
                raise SkillImportValidationError(f"Zip contains duplicate {basename}")
            members[basename] = info
    return members


def _read_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        # Do not trust ``ZipInfo.file_size`` as an allocation bound.  A
        # malformed archive can lie in its central-directory header, so cap
        # the actual decompressor output as it is read.
        with zf.open(info, "r") as member:
            data = member.read(IMPORT_MAX_ZIP_MEMBER_BYTES + 1)
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, EOFError, OSError) as exc:
        raise SkillImportValidationError("Unable to read zip member") from exc
    if len(data) > IMPORT_MAX_ZIP_MEMBER_BYTES:
        raise SkillImportValidationError("Zip member exceeds the size limit", status_code=413)
    if len(data) != info.file_size:
        raise SkillImportValidationError("Zip member size does not match its header", status_code=413)
    return data


def parse_skill_zip(content: bytes) -> tuple[dict[str, str] | None, str | None, dict]:
    """Parse a bounded archive.

    Returns ``(layers, combined_text, safe_meta)``.  A package with explicit
    layer files needs no conversion; a package containing ``SKILL.md`` is sent
    through format detection/conversion by the router.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            members = _safe_member_map(zf)
            safe_meta: dict = {}
            meta_info = members.get("meta.json")
            if meta_info:
                if meta_info.file_size > IMPORT_MAX_META_BYTES:
                    raise SkillImportValidationError("meta.json exceeds the size limit", status_code=413)
                try:
                    raw_meta = json.loads(_read_zip_member(zf, meta_info).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                    raise SkillImportValidationError("meta.json is not valid UTF-8 JSON") from exc
                safe_meta = sanitize_import_meta(raw_meta)

            has_explicit_layers = any(
                key in members for key in ("ability.md", "work.md", "persona.md", "soul.md")
            )
            if has_explicit_layers:
                ability_info = members.get("ability.md") or members.get("work.md")
                layers = {
                    "ability_md": (
                        _read_zip_member(zf, ability_info).decode("utf-8", errors="replace")
                        if ability_info else ""
                    ),
                    "persona_md": (
                        _read_zip_member(zf, members["persona.md"]).decode("utf-8", errors="replace")
                        if "persona.md" in members else ""
                    ),
                    "soul_md": (
                        _read_zip_member(zf, members["soul.md"]).decode("utf-8", errors="replace")
                        if "soul.md" in members else ""
                    ),
                }
                return validate_import_layers(layers), None, safe_meta

            skill_info = members.get("skill.md")
            if skill_info:
                text = _read_zip_member(zf, skill_info).decode("utf-8", errors="replace")
                if not text.strip():
                    raise SkillImportValidationError("Imported Skill file is empty")
                return None, text, safe_meta

            raise SkillImportValidationError("Zip contains no supported Skill files")
    except zipfile.BadZipFile as exc:
        raise SkillImportValidationError("Invalid zip file") from exc


class SkillFormat(str, Enum):
    STANDARD_3LAYER = "standard_3layer"
    NUWA_11SECTION = "nuwa_11section"
    COLLEAGUE_2LAYER = "colleague_2layer"
    PLAIN_TEXT = "plain_text"


def detect_skill_format(text: str) -> SkillFormat:
    """Detect the format of imported Skill text using heuristic rules."""
    if not text.strip():
        return SkillFormat.PLAIN_TEXT

    # Standard 3-layer: Chinese and English packages use the same three
    # top-level concepts with different labels.
    has_ability = bool(re.search(r'^#\s*(?:能力|ability)', text, re.MULTILINE | re.IGNORECASE))
    has_persona = bool(re.search(r'^#\s*(?:人格|persona)', text, re.MULTILINE | re.IGNORECASE))
    has_soul = bool(re.search(r'^#\s*(?:灵魂|soul)', text, re.MULTILINE | re.IGNORECASE))
    if sum([has_ability, has_persona, has_soul]) >= 2:
        return SkillFormat.STANDARD_3LAYER

    # Nuwa-skill: numbered sections (at least 5 of "1." through "11.")
    numbered_sections = re.findall(r'^\d{1,2}\.\s+\S+', text, re.MULTILINE)
    if len(numbered_sections) >= 5:
        return SkillFormat.NUWA_11SECTION

    # Colleague-skill: has "System Prompt" and "User Prompt"
    has_system = bool(re.search(r'(?i)##?\s*system\s*prompt', text))
    has_user = bool(re.search(r'(?i)##?\s*user\s*prompt', text))
    if has_system and has_user:
        return SkillFormat.COLLEAGUE_2LAYER

    return SkillFormat.PLAIN_TEXT


CONVERSION_SYSTEM_PROMPT = """你是一个 Skill 格式转换专家。用户会给你一段非标准格式的 AI 角色描述，
你需要将其转换为标准三层结构（能力档案 / 人格档案 / 灵魂档案）。

输出格式要求：
1. 三段内容用 ===SPLIT=== 分隔
2. 第一段以 "# 能力档案" 开头，包含 ## 核心能力、## 工具与技术、## 工作流程
3. 第二段以 "# 人格档案" 开头，包含 ## Layer 0: 第一印象、## Layer 1: 性格特征、## Layer 2: 深层动机
4. 第三段以 "# 灵魂档案" 开头，包含 ## 内核、## 价值观、## 禁忌

严格按照格式输出，不要输出其他内容。"""

CONVERSION_USER_TEMPLATE = """原始格式类型: {format_type}

原始内容:
{raw_text}

请转换为标准三层结构（用 ===SPLIT=== 分隔三段）:"""


async def convert_to_standard(
    text: str,
    detected_format: SkillFormat,
    *,
    user_id: str | None = None,
) -> dict[str, str]:
    """Convert Skill text to standard 3-layer dict with ability_md/persona_md/soul_md."""

    # Standard format: parse directly without LLM
    if detected_format == SkillFormat.STANDARD_3LAYER:
        return _parse_standard_3layer(text)

    # All other formats: use LLM to convert. Keep the raw prompt bounded here
    # even when a future caller bypasses the upload-layer limits.
    model = settings.effective_model
    user_prompt = CONVERSION_USER_TEMPLATE.format(
        format_type=detected_format.value,
        raw_text=text[:IMPORT_CONVERSION_MAX_INPUT_CHARS],
    )
    response = None
    result_text = ""
    attempted = False
    parse_ok = False
    started = time.monotonic()
    try:
        client = get_client()
        attempted = True
        response = await client.messages.create(
            model=model,
            max_tokens=4096,
            system=CONVERSION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        for block in response.content:
            if hasattr(block, "text"):
                result_text = block.text
                break
        result = _parse_split_output(result_text)
        parse_ok = any(value.strip() for value in result.values())
        return result
    finally:
        if attempted:
            try:
                await record_usage(
                    "skill_import",
                    model=model,
                    owner="user" if user_id else "system",
                    response=response,
                    est_input_tokens=estimate_tokens(CONVERSION_SYSTEM_PROMPT + user_prompt),
                    est_output_tokens=estimate_tokens(result_text),
                    user_id=user_id,
                    parse_ok=parse_ok,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
            except Exception:
                # Central metering is fail-open; preserve conversion behavior if
                # a test double or future implementation violates that contract.
                pass


def _parse_standard_3layer(text: str) -> dict[str, str]:
    """Parse standard 3-layer text by top-level headers."""
    # Try ===SPLIT=== first
    if "===SPLIT===" in text:
        parts = [p.strip() for p in text.split("===SPLIT===")]
        return {
            "ability_md": parts[0] if len(parts) > 0 else "",
            "persona_md": parts[1] if len(parts) > 1 else "",
            "soul_md": parts[2] if len(parts) > 2 else "",
        }

    # Otherwise split by Chinese or English top-level headers. Line-oriented
    # parsing avoids a later ``##`` subsection accidentally matching a layer.
    result = {"ability_md": "", "persona_md": "", "soul_md": ""}
    current_key: str | None = None
    for line in text.splitlines(keepends=True):
        header = re.match(r'^#\s*(ability|能力|persona|人格|soul|灵魂)', line, re.IGNORECASE)
        if header:
            label = header.group(1).lower()
            if label in ("ability", "能力"):
                current_key = "ability_md"
            elif label in ("persona", "人格"):
                current_key = "persona_md"
            else:
                current_key = "soul_md"
        if current_key:
            result[current_key] += line
    return {key: value.strip() for key, value in result.items()}


def _parse_split_output(text: str) -> dict[str, str]:
    """Parse LLM output that uses ===SPLIT=== delimiters."""
    parts = [p.strip() for p in text.split("===SPLIT===")]
    result = {
        "ability_md": parts[0] if len(parts) > 0 else "",
        "persona_md": parts[1] if len(parts) > 1 else "",
        "soul_md": parts[2] if len(parts) > 2 else "",
    }

    # Fallback: if split didn't work, try header-based parsing
    if len(parts) < 3:
        result = _parse_standard_3layer(text)

    return result
