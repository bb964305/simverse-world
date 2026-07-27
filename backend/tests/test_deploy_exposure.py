"""生产 compose 的暴露面回归锁。

db / redis 一直刻意绑 127.0.0.1，api 却绑过 0.0.0.0:8100 —— 那等于把 68 个
admin 端点和 /metrics 直接挂到公网，同时让 rate_limit.py 信任的
CF-Connecting-IP 变成攻击者可控的头。这条测试把「不得有对外端口绑定」钉死。
"""
from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "deploy" / "backend" / "docker-compose.yml"
_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / "deploy" / "backend" / ".env.example"


def _port_bindings() -> list[tuple[str, str]]:
    """[(service, published-port-spec)] for every published port in the prod compose."""
    spec = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    out = []
    for name, svc in (spec.get("services") or {}).items():
        for entry in (svc or {}).get("ports") or []:
            out.append((name, str(entry)))
    return out


def test_no_service_publishes_to_all_interfaces():
    """Every published port must be bound to loopback; cloudflared reaches it there."""
    offenders = [
        f"{svc}: {binding}"
        for svc, binding in _port_bindings()
        if not str(binding).startswith("127.0.0.1:")
    ]
    assert not offenders, (
        "prod compose publishes ports outside loopback:\n" + "\n".join(offenders)
    )


def test_env_example_documents_metrics_guard():
    """/metrics is open when METRICS_TOKEN is empty — the deploy template must say so."""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "METRICS_TOKEN" in text, ".env.example must document METRICS_TOKEN"
    assert "METRICS_ENABLED" in text, ".env.example must document METRICS_ENABLED"
