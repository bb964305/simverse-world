"""`.env.example` <-> `app.config.Settings` consistency gate (PLAN_P3 批次 3).

Two invariants:
1. Every key in .env.example maps to a real Settings field **or to a sidecar
   container that reads it directly from the environment** (see
   ``SIDECAR_ONLY_KEYS``) — a renamed or removed setting must not leave a
   stale example line behind (ops copy this file; a dead key silently does
   nothing).
2. Every Settings field is either documented in .env.example or explicitly
   listed in UNDOCUMENTED_OK — new config should ship with an example line,
   or a conscious decision not to.

**Why invariant 1 needed the sidecar carve-out** (07-27B G4'): the original
form assumed every key in this file is consumed by the *API process*. That
stopped being true when the lab services landed — `deploy/backend/docker-compose.yml`
runs `lab-egress`, `lab-executor`, `lab-runtime`, `artifact-ingest` and
`artifact-scanner` as **separate containers**, and they read their own knobs
with `os.getenv` rather than through `app.config.Settings` (e.g.
`app/lab/egress_service/config.py:230`, `client.py:66`). Some of them
(`LAB_ARTIFACT_SCANNER_*`) have zero readers anywhere under `backend/app/`
because the consuming image is built elsewhere entirely. This file is the
env reference for the **whole deployment**, not for one process — so the gate
has to know the difference instead of demanding a dead `Settings` field for
every sidecar knob.

The carve-out is an explicit key list and deliberately narrow: it does not weaken the
check for anything the API process actually reads. A typo'd or renamed *API*
setting still fails, which is what this invariant exists to catch.

Pure static check: no app boot, no DB.
"""
import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

#: `.env.example` 里由 **sidecar 容器**消费、API 进程不读的键。用精确键名而
#: 不是前缀——第一版按前缀写，被下面 `test_sidecar_carve_out_does_not_shadow_api_settings`
#: 当场抓出：`lab_executor_` / `lab_artifact_scanner_` 下有 33 个字段是 API
#: 进程真的在读的 `Settings`，前缀会把它们一起放过，invariant 1 对它们直接失效。
#: 新增条目前先确认 `grep -rn <KEY> backend/app/` 确实零命中或只命中 os.getenv。
SIDECAR_ONLY_KEYS: frozenset[str] = frozenset({
    # artifact-scanner（镜像在本仓之外构建，backend/app 下零读点）
    "lab_artifact_scanner_max_archive_depth", "lab_artifact_scanner_max_archive_expanded_bytes",
    "lab_artifact_scanner_max_archive_files", "lab_artifact_scanner_max_archive_ratio",
    "lab_artifact_scanner_max_csv_columns", "lab_artifact_scanner_max_image_decoded_bytes",
    "lab_artifact_scanner_max_image_pixels", "lab_artifact_scanner_max_nested_archive_bytes",
    "lab_artifact_scanner_max_text_field_bytes", "lab_artifact_scanner_parser_max_memory_bytes",
    "lab_artifact_scanner_parser_timeout_seconds",
    # lab-egress（app/lab/egress_service/* 用 os.getenv 直读，见 config.py:230 / client.py:66）
    "lab_egress_action_lease_s", "lab_egress_action_timeout_s", "lab_egress_allowed_ports",
    "lab_egress_api_key", "lab_egress_base_url", "lab_egress_connect_timeout_s",
    "lab_egress_database_path", "lab_egress_enabled", "lab_egress_max_attempts",
    "lab_egress_max_header_bytes", "lab_egress_max_links", "lab_egress_max_query_chars",
    "lab_egress_max_redirects", "lab_egress_max_response_bytes", "lab_egress_max_search_results",
    "lab_egress_max_text_chars", "lab_egress_max_url_chars", "lab_egress_poll_interval_s",
    "lab_egress_read_timeout_s", "lab_egress_request_timeout_s", "lab_egress_search_endpoint",
    "lab_egress_search_provider", "lab_egress_total_timeout_s", "lab_egress_user_agent",
    # lab-executor
    "lab_executor_artifact_spool_path", "lab_executor_artifact_upload_timeout_seconds",
    "lab_executor_ingest_base_url",
    # lab-runtime
    "lab_runtime_base_url",
})


def _is_sidecar_only(key: str) -> bool:
    return key in SIDECAR_ONLY_KEYS


# Settings fields intentionally NOT in .env.example (internal knobs with safe
# defaults / derived values). Add here only with a reason.
UNDOCUMENTED_OK: dict[str, str] = {
    # Operator-supplied task content blocklist; empty by default and a list type
    # that does not fit the scalar .env format — configured in code/secrets when a
    # real content policy exists, not via the plain .env template.
    "lab_task_blocklist": "operator content policy; empty default, list type",
}


def _example_keys() -> set[str]:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            keys.add(m.group(1).lower())
    return keys


def test_every_example_key_is_a_settings_field():
    fields = set(Settings.model_fields)
    stale = sorted(k for k in _example_keys() - fields if not _is_sidecar_only(k))
    assert not stale, f".env.example has keys with no Settings field: {stale}"


def test_sidecar_carve_out_stays_narrow():
    """白名单里的每个键都必须真的还在 `.env.example` 里。

    一个键不再出现，说明那个 sidecar 的旋钮已经改名或删掉了——此时应当把它从
    白名单里删掉，而不是留着继续放宽 invariant 1。这道断言防止 carve-out 悄悄
    长成一张「什么都放过」的清单。
    """
    orphaned = sorted(SIDECAR_ONLY_KEYS - _example_keys())
    assert not orphaned, (
        "SIDECAR_ONLY_KEYS 里有键已不在 .env.example 中，"
        f"删掉它们而不是留着放宽检查: {orphaned}")


def test_sidecar_carve_out_does_not_shadow_api_settings():
    """carve-out 不得盖住 API 进程真的在用的设置。

    若某个 `Settings` 字段进了白名单，invariant 1 对它就失效了——改名/删除不会
    再被抓到。第一版按前缀写正是栽在这里（33 个字段被误盖），所以这道断言是
    白名单形态的存在理由，不是装饰。
    """
    shadowed = sorted(f for f in Settings.model_fields if _is_sidecar_only(f))
    assert not shadowed, (
        "这些 Settings 字段被 SIDECAR_ONLY_KEYS 盖住了，invariant 1 对它们"
        f"已失效，请把它们移出白名单: {shadowed}")


def test_every_settings_field_is_documented_or_allowlisted():
    documented = _example_keys() | set(UNDOCUMENTED_OK)
    missing = sorted(set(Settings.model_fields) - documented)
    assert not missing, (
        "Settings fields missing from .env.example (document them or add to "
        f"UNDOCUMENTED_OK with a reason): {missing}"
    )
