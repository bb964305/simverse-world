"""M4 F4.2 — memory recall evaluation harness.

Compares two retrieval strategies over a labelled (query → gold memory) set:

  * keyword   — character/token overlap score (the pre-embedding baseline);
  * vector    — cosine over embeddings from ``app.memory.embedding`` when an
                embedding backend (Ollama / OpenAI-compatible) is configured,
                else a deterministic offline fallback so the harness runs
                anywhere (CI, this sandbox).

Metrics: recall@K and MRR. Prints a comparison table and returns the dict so
tests/tools can assert on it.

Run:  python -m scripts.memory_recall_eval
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re

# 20 labelled probes: (query, gold_id, corpus_text_for_gold)
DATASET: list[tuple[str, str, str]] = [
    ("咖啡馆老板娘记得我的口味", "m1", "林晚秋在咖啡馆记住了我常点的手冲和习惯的靠窗座位"),
    ("谁在酒馆讲了个夸张的故事", "m2", "周大河在酒馆眉飞色舞地讲了一个越说越离谱的旧故事"),
    ("工坊修好了我的旧钟表", "m3", "陈铁生在工坊修好了那只停摆多年的旧座钟,榫卯严丝合缝"),
    ("图书馆借书被推荐了另一本", "m4", "沈静书在图书馆按我的借阅史又多推荐了一本书"),
    ("学院老师讲了小镇的历史", "m5", "顾明远在学院的公开课上讲了小镇百年的来路"),
    ("学生问了一连串为什么", "m6", "苏小满缠着我连问了三个为什么,问到我投降"),
    ("杂货铺赊账让我先拿走东西", "m7", "何巧云在杂货铺让我先记账把东西拿走,嘴上抱怨了三句"),
    ("市政厅贴出了节庆公告", "m8", "赵启文在市政厅贴出了下一个节庆的官方公告"),
    ("实验楼研究员在记录镇况", "m9", "江临在实验楼更新了记录天气与物价的镇况日志"),
    ("广场上有人给路人画速写", "m10", "阿岚在中央广场支着画架给路过的居民画速写"),
    ("邮差把到期的信送到了", "m11", "骆小舟把一封到期的时间胶囊亲手送到了收件人手上"),
    ("下雨天大家去哪里躲雨", "m12", "一场急雨里居民们纷纷躲进最近的室内场所避雨"),
    ("集市日广场很热闹", "m13", "集市日那天中央广场支满了摊子,讨价还价声不断"),
    ("父女在酒馆隔着桌子和解", "m14", "阿岚和父亲陈铁生在酒馆隔着几张桌子终于坐到了一处"),
    ("谁当选了新镇长", "m15", "全镇投票之后,新一任镇长在市政厅公告里揭晓"),
    ("有人把小说手稿给朋友看", "m16", "沈静书第一次把写了四稿的小说手稿交给林晚秋看"),
    ("镇上要建一座邮局", "m17", "居民投票通过后,南苑空地上开始兴建一座新邮局"),
    ("研究员提交了供水提案", "m18", "江临把应对旱季缺水的供水研究提案正式提交给了全镇"),
    ("画展终于办进了工坊", "m19", "阿岚的画展最终办进了父亲的工坊,父女多年心结解开"),
    ("剧院里开了第一场故事会", "m20", "周大河在新落成的剧院里开了第一场说书故事会"),
]

# distractor corpus so retrieval isn't trivial
DISTRACTORS: list[tuple[str, str]] = [
    ("d1", "今天天气不错,阳光洒在北林荫道上"),
    ("d2", "有人在南草坪散步,遛了很久"),
    ("d3", "晚上的星光公寓亮起了灯"),
    ("d4", "小镇入口来了一位陌生的旅人"),
    ("d5", "东岸花园的树叶开始泛黄"),
]


def _tokenize(s: str) -> list[str]:
    return re.findall(r"[一-鿿]|[a-zA-Z]+", s.lower())


def keyword_score(query: str, doc: str) -> float:
    q, d = set(_tokenize(query)), set(_tokenize(doc))
    if not q or not d:
        return 0.0
    return len(q & d) / math.sqrt(len(q) * len(d))


def _fallback_embed(text: str, dim: int = 64) -> list[float]:
    """Deterministic offline embedding: hashed char-bigrams → L2-normalised
    vector. Not semantic, but stable and good enough to exercise the vector
    path when no embedding backend is configured."""
    vec = [0.0] * dim
    toks = _tokenize(text)
    grams = toks + [toks[i] + toks[i + 1] for i in range(len(toks) - 1)]
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


async def _embed(texts: list[str]) -> tuple[list[list[float]], str]:
    """Real embeddings when configured, else the deterministic fallback."""
    try:
        from app.config import settings
        from app.memory.embedding import generate_embeddings_batch
        if settings.ollama_base_url or settings.effective_api_key:
            out = await generate_embeddings_batch(texts)
            if out and all(v is not None for v in out):
                return [list(v) for v in out], "backend"
    except Exception:
        pass
    return [_fallback_embed(t) for t in texts], "fallback"


def _recall_and_mrr(ranked_ids: list[str], gold: str, k: int) -> tuple[float, float]:
    topk = ranked_ids[:k]
    recall = 1.0 if gold in topk else 0.0
    mrr = 0.0
    for i, mid in enumerate(ranked_ids, start=1):
        if mid == gold:
            mrr = 1.0 / i
            break
    return recall, mrr


async def run_eval(k: int = 5) -> dict:
    corpus = [(mid, text) for _, mid, text in DATASET] + DISTRACTORS
    ids = [mid for mid, _ in corpus]
    texts = [t for _, t in corpus]

    embeds, mode = await _embed(texts)
    id_to_vec = dict(zip(ids, embeds))

    kw_recall = kw_mrr = vec_recall = vec_mrr = 0.0
    n = len(DATASET)
    for query, gold, _ in DATASET:
        # keyword ranking
        kw_ranked = sorted(ids, key=lambda mid: keyword_score(
            query, dict(corpus)[mid]), reverse=True)
        r, m = _recall_and_mrr(kw_ranked, gold, k)
        kw_recall += r; kw_mrr += m

        # vector ranking
        (qv,), _ = await _embed([query])
        vec_ranked = sorted(ids, key=lambda mid: _cosine(qv, id_to_vec[mid]), reverse=True)
        r, m = _recall_and_mrr(vec_ranked, gold, k)
        vec_recall += r; vec_mrr += m

    result = {
        "mode": mode, "k": k, "n": n,
        "keyword": {"recall_at_k": kw_recall / n, "mrr": kw_mrr / n},
        "vector": {"recall_at_k": vec_recall / n, "mrr": vec_mrr / n},
    }
    return result


def _print(result: dict) -> None:
    print(f"\nMemory recall eval — embedding mode: {result['mode']} "
          f"(n={result['n']}, K={result['k']})\n")
    print(f"{'strategy':<10}{'recall@K':>12}{'MRR':>10}")
    print("-" * 32)
    for name in ("keyword", "vector"):
        s = result[name]
        print(f"{name:<10}{s['recall_at_k']:>12.3f}{s['mrr']:>10.3f}")
    print()
    if result["mode"] == "fallback":
        print("NOTE: no embedding backend configured — vector row uses the "
              "deterministic offline fallback. Set OLLAMA_BASE_URL (or an "
              "LLM key) and re-run for real semantic numbers.\n")


if __name__ == "__main__":
    res = asyncio.run(run_eval())
    _print(res)
