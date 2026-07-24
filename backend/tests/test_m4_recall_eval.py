"""M4 F4.2 — the recall-eval harness runs offline and returns sane metrics."""
import pytest

from scripts.memory_recall_eval import run_eval, keyword_score, _fallback_embed, _cosine


@pytest.mark.anyio
async def test_recall_eval_runs_and_reports_metrics():
    res = await run_eval(k=5)
    assert res["n"] == 20
    for strat in ("keyword", "vector"):
        assert 0.0 <= res[strat]["recall_at_k"] <= 1.0
        assert 0.0 <= res[strat]["mrr"] <= 1.0
    # both strategies should retrieve the gold in top-5 for most probes
    assert res["keyword"]["recall_at_k"] >= 0.5
    assert res["vector"]["recall_at_k"] >= 0.5


def test_keyword_and_vector_primitives_are_sane():
    assert keyword_score("咖啡馆老板娘", "咖啡馆老板娘记得我") > 0
    assert keyword_score("完全无关", "abcdef") == 0.0
    a = _fallback_embed("邮差送信")
    b = _fallback_embed("邮差送信")
    assert _cosine(a, b) == pytest.approx(1.0, abs=1e-9)  # identical text → cos 1
