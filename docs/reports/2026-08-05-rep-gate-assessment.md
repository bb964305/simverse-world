# REP_ENABLED 开闸评估（2026-08-05）

> ROADMAP 近期优先级 #4 的收口产物。标定与代码变更在分支
> `claude/suspicious-snyder-041597`；**开闸动作本身不在本批**（红线：迁移/清库
> 与开闸/行为变更不得同一次变更；此处同理——代码部署与开关翻转分两次）。

## 1. 标定实测（vm212 生产库，只读）

`docker exec deploy-api-1 python scripts/rep_calibrate.py`（2026-08-05，exit 0）：

```
== REP 信用阈值标定（只读）==
样本 n=11  min=-0.0726  p10=-0.0130  p25=+0.0247  median=+0.0969  p75=+0.1599  p90=+0.1726  max=+0.1738  mean=+0.0857
负分占比 18.2%
当前 REP_CREDIT_MIN_SCORE=-0.3000 → 拒绝 0/11 人  ← 装饰性闸门（拒绝面为空）
gossip affinity 覆盖率：1953/2099 (93.0%) 命中非零 affinity；其余 146/2099 条落在 gossip_tone(0) fallback
建议 REP_CREDIT_MIN_SCORE=+0.0058（目标拒绝面 15%）→ 拒绝 2/11 人
最低 5 人: jiang-lin=-0.0726(n=425), zhou-dahe=-0.0130(n=57), lin-wanqiu=+0.0247(n=231), zhao-qiwen=+0.0774(n=300), a-lan=+0.0795(n=206)
最高 5 人: shen-jingshu=+0.1738(n=140), su-xiaoman=+0.1726(n=150), gu-mingyuan=+0.1599(n=77), chen-tiesheng=+0.1443(n=158), he-qiaoyun=+0.0996(n=132)
```

全精度建议值 `0.005829456134654817`（zhou-dahe 与 lin-wanqiu 分值中点，
`recommend_credit_min_score` 构造保证拒绝面非空且非全量）。独立复算：由
lowest5+median+highest5 重建的 11 值向量，mean 与生产 JSON 逐位一致
（`0.08572887607311888`），重建无缺漏；模拟推荐算法逐 bit 复现建议值。

## 2. 已落地变更（本批 3 个 commit）

| commit | 内容 |
|---|---|
| `9d348dc` | `rep_credit_min_score` -0.3 → **0.0058**（config.py + .env.example 同步防 env 复活；标定分布冻进 `test_rep_calibration.py` 回归） |
| `292943e` | `/townhall/overview` 新增只读 `reputation` 节（投影夜间 recompute 落库值，fail-open，`credit_ok` 按标定阈值判定） |
| `46d80a5` | 市政厅面板「声誉」tab（降序名单、信用受限徽记、未开闸横幅、旧后端兼容空态） |

取 0.0058 而非全精度：等价区间 `(-0.013017, +0.024676)` 内拒绝面同为 2/11，
4 位小数与 render 输出一致、可读。

## 3. 开闸条件核对

| 条件（ROADMAP「已确定的运行口径」） | 结论 |
|---|---|
| F1-1 tone 由关系 affinity 决定，base_tone 退为偏置 | ✅ 已合入（`6128ecb`），且实测 affinity 覆盖率 **93.0%**——负偏是真实社交信号，不是 fallback 常数假象（<5% 才算机制未生效，实测远高） |
| F1-2 声誉入票收敛到 `vote_trust_delta()` 唯一通道，候选集与名声解耦 | ✅ 已合入（`6128ecb`） |
| F1-3 `rep_credit_min_score` 实测标定、拒绝面非空 | ✅ 本批完成：0.0058 → 拒绝 2/11（jiang-lin、zhou-dahe），exit 0 |
| 市政厅消费同一份声誉数据 | ✅ 本批完成（只读，与选举/NPC 投票同源 `meta_json["reputation"]`） |

**结论：REP_ENABLED 满足开闸条件。**

## 4. 已知边界（开闸后需观察，不构成阻塞）

- **一步 EMA 压缩**：开闸前 `previous≡0`，本次读数≈稳态×`rep_ema_alpha(0.3)`。
  开闸后分数逐夜向 raw 收敛、分布外扩（约 ×3.3）。0.0058 阈值在稳态下预计仍
  只拒绝 raw<0.0058 的同两人（jiang-lin raw≈-0.24、zhou-dahe raw≈-0.04），
  语义稳定；但建议开闸 3-5 晚后用 `rep_calibrate.py` 复测一次分布。
- **`credit_allowed()` 生产调用点为 0**：赊账/IOU 路径尚未接线，本批唯一活
  读者是市政厅 `credit_ok` 展示。开闸的实际行为影响面 = ① 夜间 `recompute`
  开始往 `meta_json["reputation"]` 写分数；② `vote_trust_delta()` 开始以
  `rep_vote_trust_weight=1.0` 进入 NPC 投票打分。「拒绝面非空」当前是阈值
  函数的性质 + 面板展示，不是任何在产信贷行为。
- **样本规模 n=11**：全部为 07-25 事故后重 seed 的内置阵容。人口扩容（阶段 7）
  后阈值需重标定。

## 5. 开闸操作单（单独执行，不与本批部署同车）

1. 部署本批代码到 vm212（rsync deploy.sh 常规姿势），**先不动开关**——部署本身
   零行为变化（`REP_ENABLED` 默认 false；已核 `docker exec deploy-api-1 env |
   grep ^REP_` 零覆盖，编译期默认即活值）。
2. 验证市政厅声誉 tab 显示「未开闸」空态（部署后回归）。
3. 单独一次变更：deploy `.env` 加 `REP_ENABLED=true` + `docker compose up -d api
   agent-worker`（两容器都读该开关）。**不要**同车携带任何其它 env/代码改动。
4. 开闸当晚核验：`nightly` 日志出现 reputation recompute 行；
   `SELECT slug, meta_json->'reputation' FROM residents` 有分数落库。
5. 3-5 晚后复跑 `docker exec deploy-api-1 python scripts/rep_calibrate.py`，
   确认稳态分布下拒绝面仍非空、必要时按新分布微调阈值。
6. 市政厅声誉 tab 复核：名单出现、`credit_ok=false` 徽记与最低分居民对应。

## 6. 验收证据

- 全量 pytest：base（0e454e2）54 failed / 2673 passed；branch 54 failed /
  2677 passed；失败集 `diff` **逐条一致**（49 test_lab_* + 5
  test_postpone_open_polls 均为预存），+4 passed 为本批新测试。
- 前端：vitest 189 passed；`npm run build`（真类型门 `tsc -b`）rc=0；lint 3
  errors 均为 base 预存文件（App.tsx/useIsMobile.ts/GraphPage.tsx），本批 0 新增。
- 运行时证据（本地 sqlite + uvicorn + vite 真实链路）：`/townhall/overview`
  实际返回含 `reputation` 节（未开闸空态与有数据态两种），面板渲染截图见
  合并请求/会话记录。
