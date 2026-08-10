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


#: API 进程自己读、但**刻意不做成 `Settings` 字段**的旋钮（调用时读 os.environ）。
#:
#: ROADMAP #5 收口后 F2 的 12 个 CIVIC_ 键已注册进 `Settings`（app/config.py），
#: 从本清单移除——注意这不是把读点搬进 Settings：civic_membership 的 reader
#: 仍在**调用时**先读进程 env（近百条 monkeypatch.setenv 测试赖此成立），env
#: 未设时经 `_settings_default` 落到 Settings 同名字段。两份默认值的一致性由
#: tests/test_civic_settings_knobs.py 钉住。
#:
#: 本清单保留为机制：将来真有「只能运行时读」的键，登记进来即可——
#: `test_runtime_env_keys_are_actually_read` 会继续要求每个键在 backend/app/
#: 下确有读点。
RUNTIME_ENV_KEYS: frozenset[str] = frozenset()

APP_DIR = Path(__file__).resolve().parents[1] / "app"


def _is_sidecar_only(key: str) -> bool:
    return key in SIDECAR_ONLY_KEYS or key in RUNTIME_ENV_KEYS


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


def test_runtime_env_keys_are_actually_read():
    """白名单里的每个键都必须在 `backend/app/` 下确有读点。

    没有这一条，`RUNTIME_ENV_KEYS` 就是 invariant 1 的后门：往里加个名字就能
    让任意 `.env.example` 死键蒙混过关。有了它，改名/删读点会立刻红——这正是
    invariant 1 本来要抓的那类漂移，只是换了个地方抓。
    """
    sources = "\n".join(
        p.read_text(encoding="utf-8")
        for p in APP_DIR.rglob("*.py") if p.is_file())
    unread = sorted(k for k in RUNTIME_ENV_KEYS if f'"{k.upper()}"' not in sources)
    assert not unread, (
        "RUNTIME_ENV_KEYS 里这些键在 backend/app/ 下找不到读点——要么读点被"
        f"改名/删掉了（同步改这里），要么这个键本来就不该在白名单里: {unread}")


def test_runtime_env_keys_do_not_shadow_api_settings():
    """同 sidecar 那条：白名单不得盖住真的 `Settings` 字段。"""
    shadowed = sorted(f for f in Settings.model_fields if f in RUNTIME_ENV_KEYS)
    assert not shadowed, (
        "这些字段既在 Settings 里又在 RUNTIME_ENV_KEYS 里，两份真值必然漂移，"
        f"请二选一: {shadowed}")


def test_every_settings_field_is_documented_or_allowlisted():
    documented = _example_keys() | set(UNDOCUMENTED_OK)
    missing = sorted(set(Settings.model_fields) - documented)
    assert not missing, (
        "Settings fields missing from .env.example (document them or add to "
        f"UNDOCUMENTED_OK with a reason): {missing}"
    )


# ── ROADMAP #5 收口: 治理旋钮的双 env 模板 parity ──────────────────────

DEPLOY_ENV_EXAMPLE = (
    ENV_EXAMPLE.parents[1] / "deploy" / "backend" / ".env.example")

#: 只对政治层三条线的前缀做窄校验——两份模板整体并不同构（deploy 版只含
#: 部署面），全量 parity 会立刻误报 200+ 键。
GOVERNANCE_PREFIXES = ("CIVIC_", "REP_", "POLIS_OFFICE_")


def _raw_keys(path) -> set[str]:
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line.strip())
        if m:
            keys.add(m.group(1))
    return keys


def _deploy_env_text() -> str:
    return DEPLOY_ENV_EXAMPLE.read_text(encoding="utf-8")


def test_retrievability_gate_states_the_real_pool_depth():
    """可检索性硬门里那个「30」必须等于代码真正的候选池深度。

    ``_retrieve_events`` 用的是 ``max(max_events * 3, 30)``
    (app/memory/service.py:364),``max_events`` 是 ``retrieve_context`` 的默认值
    (chat 那条链就是不带参数调它的)。运维照这份模板核对开闸效果,数字一漂就是
    照着一个不存在的池深在判「进没进得去」。
    """
    import inspect

    from app.memory.service import MemoryService

    limit = inspect.signature(
        MemoryService.retrieve_context).parameters["max_events"].default
    depth = max(limit * 3, 30)
    assert f"候选池深度 = {depth}" in _deploy_env_text(), \
        f"deploy/backend/.env.example 里没写出真实池深 {depth}"


def test_retrievability_gate_criterion_is_a_count_not_a_strict_min():
    """判据必须是「排在它前面的不足 N 条」,不是「importance 高于第 N 名」。

    候选池的排序是 ``ORDER BY importance DESC, created_at DESC LIMIT 30``
    (``app/memory/service.py:308-320``)—— importance **并列**时新记忆靠
    ``created_at`` 排在前面,所以「等于第 30 名」是进得去的。写成严格「高于」的
    话,一个恰好与第 30 名同分、实际已经进池的结果档记忆会被判成不达标,运维照
    着它把闸关回去 = 假红退闸。
    """
    text = _deploy_env_text()
    assert "x.importance > m.importance" in text, "缺少「比它更高的有几条」这个 count 判据"
    assert "ahead_of_it 必须 < 30" in text, "缺少「不足池深条」这句结论"
    assert "必须高于该居民 top-30 候选池第 30 名" not in text, \
        "严格「高于第 30 名」的旧判据还在,它会假红"


# ── 部署 pre-flight:名册守卫 SQL 必须与实现的 or_() 同构 ────────────────

def test_roster_guard_sql_covers_both_deletion_branches():
    """守卫 SQL 的两支必须与 ``reset_builtin_residents.find_targets`` 完全同构。

    删除判据是 ``resident_type≠'player' AND (slug ∈ LEGACY_BUILTIN_SLUGS OR
    creator_id == SYSTEM_USER_ID) AND slug ∉ NEW_ROSTER_SLUGS``
    (``seed/reset_builtin_residents.py:82-94``),而 pre-flight 守卫原先只查了
    ``creator_id`` 那一支。

    今天生产无人命中 legacy slug,所以这是**补网不是救火**。但那 19 个 slug 里有
    ``isabella`` / ``klaus`` / ``adam`` / ``mei`` / ``tamara`` 这种老 demo NPC 的
    通名 —— 玩家造一位重名的 UGC 居民完全不稀奇,而 ``docker compose up -d`` 每次
    都重跑 bootstrap 的 ``reset_builtin_residents``,会跨 13 张表把他连同记忆一起
    级联删掉,pre-flight 却安静地返回 0 行。
    """
    from seed.preset_characters import SYSTEM_USER_ID
    from seed.reset_builtin_residents import LEGACY_BUILTIN_SLUGS, NEW_ROSTER_SLUGS

    text = _deploy_env_text()
    assert f"'{SYSTEM_USER_ID}'" in text, "守卫 SQL 里的 SYSTEM_USER_ID 与实现漂了"

    missing_legacy = sorted(s for s in LEGACY_BUILTIN_SLUGS if f"'{s}'" not in text)
    assert not missing_legacy, (
        "守卫 SQL 漏了 legacy 分支的 slug（漏一个，那位同名居民就查不出来）: "
        f"{missing_legacy}")

    missing_roster = sorted(s for s in NEW_ROSTER_SLUGS if f"'{s}'" not in text)
    assert not missing_roster, (
        "守卫 SQL 的 NOT IN 名单漏了在册 preset（漏一个，他会被守卫误报成待删）: "
        f"{missing_roster}")


#: world_event 记忆分档的旋钮前缀。**不挂到 `GOVERNANCE_PREFIXES` 上**——那个元组
#: 的语义是「政治层三条线」,而这批是拟真层(`REALISM_INFO_*` 的同族);但「两份模板
#: 别漂」这件事两边一样需要,所以这里单开一条同形状的断言。
#:
#: 没有它,本批就落进 07-27B 审计 H2 那个事故级问题类:`REALISM_` 不在
#: `GOVERNANCE_PREFIXES` 里 → 下面那条 parity **扫不到**本批的键 → 运维照 deploy
#: 模板起的环境读不到它们。
EVENT_MEMORY_TIER_PREFIX = "REALISM_EVENT_MEMORY_"


def test_event_memory_tier_knobs_exist_in_deploy_env_example_too():
    """world_event 记忆分档的旋钮必须同时出现在两份 env 参考里。"""
    backend_keys = {k for k in _raw_keys(ENV_EXAMPLE)
                    if k.startswith(EVENT_MEMORY_TIER_PREFIX)}
    assert backend_keys, "backend/.env.example 里没有任何分档旋钮?基线认知错误"
    missing = sorted(backend_keys - _raw_keys(DEPLOY_ENV_EXAMPLE))
    assert not missing, (
        f"deploy/backend/.env.example 缺 world_event 记忆分档旋钮(补上并保持默认关): {missing}")


#: 候选池保留位的旋钮前缀。和 ``EVENT_MEMORY_TIER_PREFIX`` 一个道理:``REALISM_``
#: **不在** ``GOVERNANCE_PREFIXES`` 里,下面那条 parity 扫不到本批的键 —— 没有这
#: 条断言,运维照 deploy 模板起的环境里根本不存在这个旋钮(07-27B 审计 H2 把「多份
#: env 真值互相漂移」定为事故级问题类)。
POOL_RESERVE_PREFIX = "REALISM_POOL_CIVIC_"


def test_pool_reserved_slot_knob_exists_in_deploy_env_example_too():
    """候选池保留位的旋钮必须同时出现在两份 env 参考里。"""
    backend_keys = {k for k in _raw_keys(ENV_EXAMPLE)
                    if k.startswith(POOL_RESERVE_PREFIX)}
    assert backend_keys, "backend/.env.example 里没有候选池保留位旋钮?基线认知错误"
    missing = sorted(backend_keys - _raw_keys(DEPLOY_ENV_EXAMPLE))
    assert not missing, (
        f"deploy/backend/.env.example 缺候选池保留位旋钮(补上并保持默认 0): {missing}")


def test_pool_reserved_slot_defaults_to_zero_everywhere():
    """``0 = 逐字节旧行为``,所以三处默认必须都是 0。

    这个旋钮一个数同时表达「开没开」与「几个坑」—— 任何一处模板写成非 0,
    运维照它起的环境就是**默认开闸**,而开闸会改写每位居民的候选池组成。
    """
    assert Settings.model_fields["realism_pool_civic_reserve"].default == 0, \
        "Settings 里的默认不是 0 —— 保留位必须默认关"
    for path in (ENV_EXAMPLE, DEPLOY_ENV_EXAMPLE):
        assert "REALISM_POOL_CIVIC_RESERVE=0" in path.read_text(encoding="utf-8"), \
            f"{path} 里的保留位默认不是 0"


def test_governance_knobs_exist_in_deploy_env_example_too():
    """F1/F2/F3 的旋钮必须同时出现在两份 env 参考里。

    deploy/backend/.env.example 是 vm212 部署实际参照的模板；07-27B 审计 H2
    把「多份 env 真值互相漂移」定为事故级问题类。收口前 deploy 版三条线的
    旋钮整段缺失——运维照模板起环境，读到的是一个不存在这三条线的世界。
    """
    backend_keys = {k for k in _raw_keys(ENV_EXAMPLE)
                    if k.startswith(GOVERNANCE_PREFIXES)}
    assert backend_keys, "backend/.env.example 里没有任何治理旋钮？基线认知错误"
    missing = sorted(backend_keys - _raw_keys(DEPLOY_ENV_EXAMPLE))
    assert not missing, (
        f"deploy/backend/.env.example 缺治理旋钮（补上并保持默认关/保守）: {missing}")
