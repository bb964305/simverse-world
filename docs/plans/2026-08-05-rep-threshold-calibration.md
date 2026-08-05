# Plan：REP 阈值实测重标定 + 市政厅声誉公示（ROADMAP 近期优先级 #4）

> 2026-08-05。工作分支 `claude/suspicious-snyder-041597`（worktree）。
> 前置证据：vm212 生产库 `scripts/rep_calibrate.py` 实测（只读，exit 0），见 §0。
> 红线：本批**不改** `REP_ENABLED` 默认值，开闸动作与本批代码不同一次部署。
> 冲突面：并行任务会在 config.py/.env.example 补 F1/F2/F3 旋钮——本任务只动
> `rep_credit_min_score` 一行（config.py:607、.env.example:559）+ townhall 前后端。

## §0 标定实测（2026-08-05, vm212 deploy-api-1, 只读）

```
样本 n=11  min=-0.0726  p10=-0.0130  p25=+0.0247  median=+0.0969  p75=+0.1599
p90=+0.1726  max=+0.1738  mean=+0.0857  负分占比 18.2%
当前 REP_CREDIT_MIN_SCORE=-0.3000 → 拒绝 0/11 人  ← 装饰性闸门（拒绝面为空）
gossip affinity 覆盖率：1953/2099 (93.0%) 命中非零 affinity
建议 REP_CREDIT_MIN_SCORE=+0.0058（目标拒绝面 15%）→ 拒绝 2/11 人
最低: jiang-lin=-0.0726(n=425), zhou-dahe=-0.0130(n=57), lin-wanqiu=+0.0247(n=231)
```

全精度 `recommended=0.005829456134654817`（= zhou-dahe 与 lin-wanqiu 分值中点）。
完整 11 个分值可重建：lowest5 = rank1-5，median = rank6，highest5 = rank7-11。
取值定 **0.0058**（render 的 4 位舍入；区间 `(-0.013017, +0.024676)` 内任意值
拒绝面同为 2/11，构造保证非空且非全量）。

## Step 1（TDD）：`rep_credit_min_score` -0.3 → 0.0058

**测试先行** `backend/tests/test_rep_calibration.py` 末尾追加：

```python
# ── F1 第 3 项收口:2026-08-05 vm212 生产标定(scripts/rep_calibrate.py, exit 0) ──
# lowest5 = rank1-5, median = rank6, highest5 = rank7-11 → 11 个分值完整重建。
VM212_SCORES_2026_08_05 = [
    -0.07255692647058824,   # jiang-lin (n=425)
    -0.01301682192982457,   # zhou-dahe (n=57)
    0.024675734199134203,   # lin-wanqiu (n=231)
    0.07742592133333333,    # zhao-qiwen (n=300)
    0.07951788834951452,    # a-lan (n=206)
    0.09686159192825113,    # median (rank 6)
    0.0995802651515151,     # he-qiaoyun (n=132)
    0.14430753164556964,    # chen-tiesheng (n=158)
    0.1598606883116883,     # gu-mingyuan (n=77)
    0.17258855,             # su-xiaoman (n=150)
    0.1737732142857143,     # shen-jingshu (n=140)
]


def test_default_threshold_matches_vm212_calibration():
    """默认值必须落在实测建议值所在的拒绝面等价区间内(拒绝 2/11)。"""
    assert settings.rep_credit_min_score == pytest.approx(0.0058)
    recommended = recommend_credit_min_score(VM212_SCORES_2026_08_05, 0.15)
    assert recommended == pytest.approx(0.005829456134654817)
    rejected_default = sum(
        1 for s in VM212_SCORES_2026_08_05 if s < settings.rep_credit_min_score
    )
    rejected_recommended = sum(
        1 for s in VM212_SCORES_2026_08_05 if s < recommended
    )
    assert rejected_default == rejected_recommended == 2  # 非空且非全量


def test_credit_allowed_has_nonempty_rejection_on_vm212_distribution():
    denied = [s for s in VM212_SCORES_2026_08_05 if not credit_allowed(s)]
    granted = [s for s in VM212_SCORES_2026_08_05 if credit_allowed(s)]
    assert len(denied) == 2 and len(granted) == 9
    assert not credit_allowed(-0.07255692647058824)  # jiang-lin
    assert credit_allowed(0.024675734199134203)      # lin-wanqiu
```

跑出**失败**（现值 -0.3 → rejected_default=0）。

**实现**：
- `backend/app/config.py:607`
  `rep_credit_min_score: float = 0.0058  # 2026-08-05 vm212 实测标定(scripts/rep_calibrate.py): n=11 拒绝 2/11;旧值 -0.3 拒绝面为空`
- `backend/.env.example:559` → `REP_CREDIT_MIN_SCORE=0.0058`（防 env 覆盖复活旧值，仿 e46757a）

**验收**：`pytest tests/test_rep_calibration.py tests/test_reputation_service.py -q` 全绿。
Commit: `fix(rep): rep_credit_min_score 实测重标定 -0.3→0.0058（vm212 n=11 拒绝面 2/11, 附标定输出）`

## Step 2（TDD）：/townhall/overview 增加 reputation 节（只读、fail-open）

**测试先行** `backend/tests/test_townhall.py` 追加：

```python
@pytest.mark.anyio
async def test_overview_projects_stored_reputation(client, db_session):
    db_session.add_all([
        _res("gao", "高分居", meta_json={"reputation": {
            "score": 0.17, "samples": 140, "updated_at": "2026-08-05T00:00:00+00:00"}}),
        _res("di", "低分居", meta_json={"reputation": {
            "score": -0.07, "samples": 425, "updated_at": "2026-08-05T00:00:00+00:00"}}),
        _res("wu", "无分居"),  # meta_json=None → 不该出现在名单里
    ])
    await db_session.commit()

    data = (await client.get("/townhall/overview")).json()
    rep = data["reputation"]
    assert rep["enabled"] == settings.rep_enabled
    assert rep["credit_min_score"] == pytest.approx(settings.rep_credit_min_score)
    rows = {r["slug"]: r for r in rep["residents"]}
    assert set(rows) == {"gao", "di"}          # 只列真实落过库的
    assert [r["slug"] for r in rep["residents"]] == ["gao", "di"]  # 按分数降序
    assert rows["gao"]["credit_ok"] is True
    assert rows["di"]["credit_ok"] is False    # -0.07 < 0.0058
    assert rows["di"]["samples"] == 425
    assert rows["di"]["name"] == "低分居"


@pytest.mark.anyio
async def test_overview_reputation_fails_open(client, db_session, monkeypatch):
    from app.routers import townhall as th
    def boom(residents):
        raise RuntimeError("boom")
    monkeypatch.setattr(th, "_reputation", boom)
    resp = await client.get("/townhall/overview")
    assert resp.status_code == 200
    assert resp.json()["reputation"]["residents"] == []
```

注意 `_res` 的 `meta_json` 走 kwargs 覆盖（`d.update(kw)`），"wu" 无 duty/mayor →
`meta_json=None`，会被 `_npc_residents` 的 `meta_json.isnot(None)` 过滤——正好当
「无声誉数据」路径。跑出**失败**（KeyError: 'reputation'）。

**实现** `backend/app/routers/townhall.py`：

```python
from app.services import reputation_service

def _reputation(residents: list[Resident]) -> dict:
    """S1-1 声誉公示:只读投影 meta_json["reputation"](夜间 recompute 的落库值),
    不在请求路径上重算。只列真实被写过分数的居民——开闸前该键不存在,面板显示
    空态而不是一排假中性 0。credit_ok 消费的就是标定后的 rep_credit_min_score。"""
    rows = []
    for r in residents:
        stored = (r.meta_json or {}).get("reputation")
        if not isinstance(stored, dict):
            continue
        score = reputation_service.score_from_meta(r.meta_json)
        try:
            samples = int(stored.get("samples") or 0)
        except (TypeError, ValueError):
            samples = 0
        rows.append({
            "slug": r.slug, "name": r.name, "score": score, "samples": samples,
            "updated_at": stored.get("updated_at"),
            "credit_ok": reputation_service.credit_allowed(score),
        })
    rows.sort(key=lambda row: row["score"], reverse=True)
    return {
        "enabled": settings.rep_enabled,
        "credit_min_score": settings.rep_credit_min_score,
        "residents": rows,
    }
```

overview handler 里按既有 fail-open 模式加一段：

```python
    try:
        reputation = _reputation(residents)
    except Exception:
        logger.warning("townhall: reputation projection failed", exc_info=True)
        reputation = {"enabled": settings.rep_enabled,
                      "credit_min_score": settings.rep_credit_min_score,
                      "residents": []}
```

返回 dict 加 `"reputation": reputation`。

**验收**：`pytest tests/test_townhall.py -q` 全绿。
Commit: `feat(townhall): overview 增加只读声誉公示节（消费 recompute 落库值 + credit_ok 闸门读数）`

## Step 3（TDD）：前端市政厅「声誉」tab

**测试先行** `frontend/src/components/TownHallPanel.test.tsx`：
- OVERVIEW fixture 加 `reputation`（两行：credit_ok true/false + samples）
- 新用例①：点「声誉」tab → 按分数降序列出居民、低分者带「信用受限」徽记
- 新用例②：`reputation: { enabled: false, credit_min_score: 0.0058, residents: [] }`
  → 显示「声誉系统未开闸」空态文案
跑出**失败**（找不到 tab）。

**实现**：
- `frontend/src/services/api/townhall.ts`：
  ```ts
  export interface TownHallReputationRow {
    slug: string; name: string; score: number; samples: number;
    updated_at: string | null; credit_ok: boolean
  }
  export interface TownHallReputation {
    enabled: boolean; credit_min_score: number; residents: TownHallReputationRow[]
  }
  ```
  `TownHallOverview` 加 `reputation?: TownHallReputation`（**可选**：前端独立部署
  CF Workers，旧后端 + 新前端时 fail-open 到空态）。
- `frontend/src/components/TownHallPanel.tsx`：
  - `TownTab` 加 `'rep'`；TABS 加 `{ key: 'rep', label: '声誉' }`
  - `RepTab`：无数据/未开闸 → 空态文案；有数据 → 行卡（名字、`score.toFixed(3)`
    带符号、`n=samples` 证据数、`credit_ok=false` 红色「信用受限」徽记）；页脚
    `信用阈值 {credit_min_score}·夜间聚合`。样式复用 `card`/`muted`。

**验收**：`npm run test`、`npm run lint`、`npx tsc --noEmit`、`npm run build` 全绿。
Commit: `feat(townhall-ui): 市政厅声誉 tab（只读公示 + 信用受限徽记）`

## Step 4：全量回归（相对 base 零新增失败）

- `cd backend && mv .env /tmp/env-backup 2>/dev/null; .venv/bin/python -m pytest tests/ -q`
  （worktree 若无 .venv，用主 checkout venv 的 python，从 worktree backend/ 目录跑）
- base（master 0e454e2）同法跑一遍，diff 失败集；预存失败集中在 test_lab_*（~49）。
- 硬门 = 相对 base **零新增失败**，非 literal 0 failed。
- `/Volumes/data` 陷阱：rc 写 /tmp 落盘核实。

## Step 5：verify-before-done（真实运行证据）

- 本地起后端（sqlite:/tmp + DEBUG）+ 种子两位居民带 reputation meta →
  `GET /townhall/overview` 真实响应含 reputation 节。
- 本地起前端 dev（VITE_API_URL 指本地后端；注意 proxy 1082 别劫持 localhost）→
  浏览器开市政厅 → 声誉 tab 截图（有数据态 + 未开闸空态两张）。

## Step 6：开闸评估结论（文档，不动开关）

写入本 plan 同目录报告或直接在 handoff 里：
- 拒绝面非空已成立（0.0058 → 2/11），exit 0；
- affinity 覆盖率 93% → F1 语气机制真实生效，负偏非 fallback 常数假象；
- 已知残余：一步 EMA 读数≈稳态×α(0.3)，开闸后分布外扩，0.0058 的拒绝面预计仍
  锁定 raw<0 的同两人（jiang-lin raw≈-0.24、zhou-dahe raw≈-0.04），阈值语义稳定；
- 结论 + 开闸操作单（单独部署，不与本批同车）。

## 对抗审查修正（2026-08-05 critic，无 BLOCKER，采纳的 MINOR）

- **F1.3** `_res("wu")` 显式 `meta_json=None` 经 SQLAlchemy JSON(none_as_null=False)
  存成 JSON `'null'` 而非 SQL NULL → **不会**被 `isnot(None)` 过滤；"wu" 实际由
  `_reputation` 的 `isinstance(stored, dict)` 守卫剔除。测试保留，注释改口径。
- **F1.4** townhall 测试 `monkeypatch.setattr(settings, "rep_credit_min_score", 0.0058)`
  解耦对 Step 1 默认值的隐式依赖。
- **F3.3** 阈值 pin 断言编译期默认 `Settings.model_fields["rep_credit_min_score"].default`
  （env-proof）；credit_allowed 分布测试同样 monkeypatch 后断言。
- **F3.4** base 全量已在改动前的本 worktree 跑完（54 failed = 49 lab + 5 postpone，
  log /tmp/rep-pytest-base.log），无需动任何 `.env`。
- **F4.1** `TownTabBody` if 链**必须**加 `if (tab === 'rep') return <RepTab .../>`（否
  则静默 fallthrough 到 ResultTab，tsc 不报）。
- **F4.4** 类型门用 `npm run build`（`tsc -b && vite build`）；裸 `npx tsc --noEmit`
  在 solution-style tsconfig 下是空转。
- **F4.6** RepTab 空态键在 `residents.length === 0`；`enabled === false` 单独出
  「未开闸」横幅，可与已有数据并存（开闸前 recompute 后的 vm212 正是此态）。
- **F4.3** 影响面补记：`GovernanceInsightsPanel.tsx` 也消费 `getTownHallOverview()`，
  可选字段不破坏编译，本批不改它。
- **F5.4** Step 6 评估必须写明：`credit_allowed` 生产调用点为 0，闸门本批仍无
  消费方；唯一活读者是市政厅 `credit_ok` 展示。
- **F5.5** 开闸操作单加一条：核对 vm212 容器实际 env 无 `REP_CREDIT_MIN_SCORE`
  覆盖（`docker exec deploy-api-1 env | grep REP_`）。
- **F6.2** Step 6 更新 ROADMAP：#4 收口、`:576` → `:607` 行号修正。

## Self-review checklist

- [x] spec coverage：任务 4 项 ↔ Step1(标定+改值)/Step2-3(市政厅)/Step6(评估)/§0(实测)
- [x] placeholder scan：无 TODO/省略号代码；测试向量全部来自真实标定 JSON
- [x] type consistency：`_res` kwargs 覆盖 meta_json 已核（d.update(kw)）；
      `score_from_meta`/`credit_allowed` 签名已读源；vitest 已配置（package.json:15）
- [x] step size：每 step 一 commit，TDD 先失败后通过
