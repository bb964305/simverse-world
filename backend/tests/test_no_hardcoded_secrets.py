"""源码里不得出现供应商密钥字面量（07-27B C1b）。

**这道闸的由来。** 提交 `5193854` 把两把真实 API key 硬编码进
`app/routers/admin/system_config.py` 并推到了**公开仓库**；`78fd4bf`
（v1.0 开源前的 sanitize）只替换了工作区，历史 blob 至今仍可取出。两把 key 已于
2026-07-27 在供应商侧吊销，但当时**没有任何前置检查**阻止它们进仓库——没有
pre-commit secret scan、CI 里也没有 gitleaks。这个测试补的就是那个缺口。

**为什么不直接复用 `app/lab/guard.py` 的 `_SECRET_RE`。** 那条正则的最后一个
子句是 `(?i:api[_-]?key|token|password|secret)\\s*[:=]\\s*\\S+`——它服务的是
「把运行时文本里的疑似密钥涂黑」，宁可错杀。用它扫源码会命中
`api_key: str | None = None`、`token = create_token(...)` 这类几百处正常代码，
噪声会让这道闸在第一周就被人加满 allowlist 然后失效。

所以只取**高置信度的供应商字面量前缀**：这些形态在正常源码里不会出现，一旦命中
基本可以断定是真密钥。误报接近零，也就没有「加 allowlist 绕过」的动力。

纯静态检查：读文件，不 import app、不连库、不跑网络。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: 高置信度的供应商密钥字面量。刻意**不含** guard.py 那条
#: `api_key\\s*[:=]\\s*\\S+` 的宽泛子句，理由见模块 docstring。
SECRET_LITERAL_RE = re.compile(
    r"sk-[A-Za-z0-9]{16,}"          # OpenAI / 兼容端点
    r"|AKIA[0-9A-Z]{12,}"           # AWS access key id
    r"|gh[pousr]_[A-Za-z0-9]{20,}"  # GitHub token
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|AIza[0-9A-Za-z_-]{30,}"      # Google API key
)

SCAN_ROOTS = (
    REPO / "backend" / "app",
    REPO / "backend" / "scripts",
    REPO / "backend" / "seed",
    REPO / "frontend" / "src",
)
SCAN_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}

#: 本文件自己含有上面那些形态的**正则**（不是密钥），必须排除，否则闸会自噬。
SELF = Path(__file__).resolve()


def _offenders() -> list[str]:
    hits: list[str] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if (path.suffix not in SCAN_SUFFIXES or not path.is_file()
                    or path.resolve() == SELF or "node_modules" in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                m = SECRET_LITERAL_RE.search(line)
                if m:
                    # 只报位置和前 6 个字符，绝不把命中内容整段写进测试输出——
                    # 失败信息会进 CI 日志，而 CI 日志的可见面比源码还宽。
                    rel = path.relative_to(REPO)
                    hits.append(f"{rel}:{lineno} 命中 {m.group(0)[:6]}…")
    return sorted(hits)


def test_no_provider_key_literals_in_source():
    """`backend/app`、`backend/scripts`、`backend/seed`、`frontend/src` 零命中。

    这一条红，先别急着加 allowlist：仓库是公开的，一旦推上去，历史里就永久
    留着它（`5193854` 至今仍可 `git show`）。正确顺序是**先吊销、再改代码**。
    """
    offenders = _offenders()
    assert not offenders, (
        "源码里出现了疑似供应商密钥字面量。仓库是公开的——先去供应商控制台吊销，"
        "再把值移进环境变量，不要只删代码（历史留得住）:\n  "
        + "\n  ".join(offenders))


def test_the_scanner_actually_matches_a_real_shape():
    """守卫自身的守卫：正则必须真能抓到每一种形态。

    没有这条，一次手滑把正则改坏（比如误删一个分支）会让整道闸静默失效，而
    `test_no_provider_key_literals_in_source` 依然是绿的——绿得毫无意义。
    """
    samples = {
        "openai": "sk-" + "a1b2c3d4e5f6g7h8i9",
        "aws": "AKIA" + "ABCDEFGHIJKL",
        "github": "ghp_" + "a" * 24,
        "slack": "xoxb-" + "1234567890ab",
        "google": "AIza" + "b" * 32,
    }
    missed = sorted(k for k, v in samples.items()
                    if not SECRET_LITERAL_RE.search(v))
    assert not missed, f"扫描正则漏掉了这些形态: {missed}"


def test_the_scanner_ignores_ordinary_code():
    """反向断言：正常写法不得误报，否则这道闸会被 allowlist 淹死。"""
    benign = [
        'api_key: str | None = None',
        'token = create_token(user.id)',
        'password = form.get("password")',
        'settings.effective_api_key',
        'r"sk-[A-Za-z0-9]{16,}"',   # guard.py 里的正则本身
        'MASKED_VALUE = "********"',
    ]
    flagged = [s for s in benign if SECRET_LITERAL_RE.search(s)]
    assert not flagged, f"这些正常写法被误报了，正则太宽: {flagged}"
