# T1 — P2 收尾 / Adapter 选型 阻塞报告（书面）

- 日期：2026-07-19（Cowork 会话实测）
- 关联：`docs/adr/ADR-lab-runtime-adapter.md`（保持 **未选型 / Proposed**）、
  `docs/KICKOFF_PROMPT_LAB_REMAINING.md` T1、验收 V04–V06（真机段）与 V11（OCI）。
- 硬约束依据：kickoff「硬约束 #1 不伪造 adapter 评分」「#2 colima/Docker Desktop
  不是生产隔离证据」。

## 结论（一句话）

真实 runtime endpoint 未配置、且本会话沙箱无可用容器运行时 → **走 T1「否则」分支**：
不接入任何真实 adapter、ADR 维持未选型、`lab_oci_enabled` 维持 False，Mock 路径不受影响；
本报告记录缺哪些 env、如何配置、以及解除阻塞后的可复现步骤。

## 本会话实测证据

1. **真实 adapter endpoint 全空**（`backend/app/config.py` 默认值，`backend/.env` 无任何覆盖）：

   | 配置键 | 值 | 说明 |
   |---|---|---|
   | `lab_hermes_base_url` / `lab_hermes_api_key` | `""` / `""` | Hermes 候选未配置 |
   | `lab_openclaw_base_url` / `lab_openclaw_api_key` | `""` / `""` | OpenClaw 候选未配置 |
   | `lab_computer_use_base_url` / `lab_computer_use_api_key` | `""` / `""` | computer-use 候选未配置 |
   | `lab_adapter` | `"mock"` | 默认唯一启用 runtime |
   | `lab_oci_enabled` / `lab_oci_image` | `False` / `""` | OCI 真实执行未启用 |

   `backend/.env` 中 `grep -iE 'LAB_|HERMES|OPENCLAW|COMPUTER_USE|OCI'` 无命中。

2. **fail-closed 已被机器断言**：新增 `backend/tests/test_lab_adapter_selection.py`（7 用例，绿）
   证明——默认 runtime=mock、OCI off；三个真实 HTTP adapter import/构造安全但 `start()` 在
   `base_url` 为空时抛 `LabAdapterUnconfigured`（任何 run 不可能对未配置 runtime 起跑）；
   registry 对未知名/未配置真实名降级回 Mock，不硬崩 runner。

3. **无容器运行时可收集 V11 证据**：本会话沙箱 `command -v docker podman nerdctl` 均无；
   且沙箱本身是 bwrap 容器、非「专用 Linux runner（cgroup v2 + rootless OCI + seccomp/AppArmor
   + 受控 egress）」。按硬约束 #2，即便有 colima/Docker Desktop 也不算生产隔离证据。

## 被阻塞的验收项

- **V04–V06（真机段）**：adapter handshake / cursor·ACK·replay / cancel·kill / health /
  resume·checkpoint 的真机断言，依赖至少一个真实 endpoint。框架就绪（`adapter_gate.py` +
  `test_lab_adapter_gate.py`），仅缺被测对象。
- **V11（OCI 真实隔离证据）**：`pytest -m lab_oci tests/integration/test_lab_executor_oci.py`
  需在专用 Linux runner 上跑；见 `deploy/lab-oci-runner/README.md` 准备脚本与步骤。

## 如何解除阻塞（可复现）

### A. Adapter 选型（V04–V06）

1. 供应一个真实 runtime 实例，设置其 endpoint（择一或多）：
   ```
   LAB_HERMES_BASE_URL=...        LAB_HERMES_API_KEY=...
   LAB_OPENCLAW_BASE_URL=...      LAB_OPENCLAW_API_KEY=...
   LAB_COMPUTER_USE_BASE_URL=...  LAB_COMPUTER_USE_API_KEY=...
   ```
2. 用薄 conformance shim 暴露探针所需 hook（`handshake_manifest`、`emit_tool_intent`、
   `provider_events`、`subagent_child_caps`、`accepts_infra_handles`、`license_manifest_path`、
   可选 `cancel/terminate/kill/health`）。
3. 对每个候选跑 `adapter_gate.run_conformance(shim, db=<staging session>)` →
   `adapter_gate.score_candidate(name, results)`。
4. **仅当**恰好一个候选 `total >= 80` **且**三个 mandatory 维度全 `>= 0.6`：把
   `ADR-lab-runtime-adapter.md` 置 **Accepted**、记录胜者 + 落选者逐维度证据、把该 adapter
   接到其 feature flag 后。开启 V04–V06 全量断言。
5. 若无候选达标：维持 Mock、P2 选型继续停摆、再出一版 ADR（**禁止**臆造通过）。

### B. OCI（V11）

见 `deploy/lab-oci-runner/`（`provision-runner.sh` + `README.md`）：在专用 Linux runner
（cgroup v2、rootless docker/podman、seccomp+AppArmor、受控 egress）上 `docker pull alpine:latest`
后运行 `pytest -m lab_oci tests/integration/test_lab_executor_oci.py`，把输出与 runner 环境指纹
存档为证据；证据合格前 `lab_oci_enabled` 保持 False，仅在 staging 手动灰度真实执行。

## 处置

- ADR：维持未选型（无需改动）。
- 代码：不接入任何真实 adapter；新增 fail-closed 守卫测试锁死当前安全默认。
- 后续任务（T2–T8）继续，Mock 路径不受影响。
