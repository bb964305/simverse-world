"""Deploy 配置面防护栏（2026-08-06 .env 覆盖险情预防）.

背景：vm212 的 agent-worker LLM 活配置（AGENT_BASE_URL/AGENT_MODEL/AGENT_API_KEY）
曾只存在于远端 backend/.env + 镜像内 /app/.env（Dockerfile `COPY . .` 带入），
而 deploy.sh 的 backend rsync 带 --delete 且不排除 .env——照跑一次就会把它
覆盖成本机 dev 版，静默换 LLM 供应商外加带入本机 LAB_ADAPTER。三条不变量
对应三层防护；07-27B H2 把「多份 env 真值互相漂移」定为事故级问题类。

纯静态检查：不起 app、不连 DB。
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_SH = REPO_ROOT / "deploy" / "backend" / "deploy.sh"
DOCKERIGNORE = REPO_ROOT / "backend" / ".dockerignore"
DEPLOY_ENV_EXAMPLE = REPO_ROOT / "deploy" / "backend" / ".env.example"


def _rsync_blocks(text: str) -> list[str]:
    """把 deploy.sh 里每条 rsync 调用（含续行）切成独立块。"""
    blocks: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if current is None:
            if line.lstrip().startswith("rsync"):
                current = [line]
                if not line.rstrip().endswith("\\"):
                    blocks.append("\n".join(current))
                    current = None
        else:
            current.append(line)
            if not line.rstrip().endswith("\\"):
                blocks.append("\n".join(current))
                current = None
    return blocks


def test_backend_rsync_excludes_dotenv():
    """带 --delete 的 rsync 必须排除 .env——exclude 掉的接收端文件 --delete 不删。"""
    blocks = [b for b in _rsync_blocks(DEPLOY_SH.read_text(encoding="utf-8"))
              if "--delete" in b]
    assert blocks, "deploy.sh 里找不到带 --delete 的 rsync——脚本结构变了，本测试需跟进"
    for block in blocks:
        assert "--exclude '.env'" in block, (
            "带 --delete 的 rsync 不排除 .env，会把远端活配置覆盖成本机 dev 版:\n"
            + block)


def test_dockerignore_excludes_dotenv():
    """镜像不得内嵌 .env——运行时配置一律走 compose env_file 注入。"""
    assert DOCKERIGNORE.exists(), (
        "backend/.dockerignore 不存在——Dockerfile `COPY . .` 会把 .env 烤进镜像")
    lines = [l.strip() for l in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()]
    assert ".env" in lines, ".dockerignore 必须精确排除 .env（.env.example 是 git 跟踪的参考，不受影响）"


def test_deploy_health_check_hits_published_port():
    """compose 把 API 发布在 127.0.0.1:8100（docker-compose.yml api.ports），
    deploy.sh 末尾的健康检查打 8000 只会永远打印误导性的 ✗。"""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert "localhost:8100/health" in text
    assert "localhost:8000/health" not in text
