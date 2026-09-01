# Simverse Web3 开发、部署与使用手册

本手册描述当前 Web3 改造的真实运行方式。它不包含发币、交易、质押、分红或任何代币经济模型。项目币可以继续通过外部发射台独立发行；本仓库只负责钱包身份、Agent 所有权、训练/上传确权、记忆、存档和世界证明。

上游原有的 Soul Coin、商店和小镇经济仍是服务器内的游戏积分与玩法数据，保持原游戏兼容；它们没有被映射成链上代币，不能充值、提现或交易，本次改造也没有为其增加任何资金入口。

## 1. 现在怎样运作

```text
RabbyKit 钱包
  │ ① 连接钱包，签署一次性登录消息（不发交易）
  ▼
FastAPI 钱包认证
  │ ② 验证域名、链、地址、nonce、过期时间和 EIP-191 签名
  │ ③ 签发短期 JWT，供原游戏 REST / WebSocket 使用
  ▼
Simverse 原游戏 ────── PostgreSQL / Redis（实时世界与完整内容）
  │
  ├─ 训练文件、记忆、存档 → 钱包私有内容接口 / 后续可换 IPFS、Arweave
  │
  └─ 内容 SHA-256 + URI + 版本 + 所有权
                         ▼
             UUPS Proxy / SimverseAgentRegistry
```

钱包地址是公开身份的唯一来源。JWT 不是另一个账号体系，只是钱包完成签名后给现有高频游戏 API 和 WebSocket 使用的短期会话凭证。公开页面不再提供邮箱、GitHub 或 LinuxDo 登录；生产环境默认关闭旧登录 API。`DEBUG=true` 或显式 `WEB2_AUTH_ENABLED=true` 只用于旧数据迁移和上游测试兼容。

完整对话、模型文件和游戏存档通常很大，也可能包含隐私，因此不直接写进链上。链上保存内容哈希、受控 URI、父版本、版本号、时间和所有权；内容本体保留在链下。这样可以验证内容有没有被改过，也能以后把存储实现替换成 IPFS、Arweave 或加密对象存储。

## 2. 合约能力

合约位于 `contracts/contracts/SimverseAgentRegistry.sol`，采用 OpenZeppelin UUPS + ERC-1967 Proxy。

| 能力 | 谁能写 | 链上结果 |
|---|---|---|
| 创建 Agent Passport | 钱包本人 | 不可转让的 ERC-721 身份、元数据 URI/哈希 |
| 更新 Agent 元数据 | Passport 所有者 | 新元数据 URI/哈希 |
| 发布训练/上传版本 | Passport 所有者 | artifact URI/哈希、training root、递增版本 |
| 锚定记忆快照 | 所有者或受信世界写入者 | URI/哈希、父哈希、递增记忆版本 |
| 锚定游戏存档 | 所有者或受信世界写入者 | URI/哈希、父哈希、递增存档版本 |
| 写入世界证明 | `WORLD_WRITER_ROLE` | 事件类型、数据哈希、世界版本 |
| 升级实现 | `UPGRADER_ROLE` | Proxy 地址和历史数据保持不变 |

Passport 禁止 `approve`、`setApprovalForAll` 和钱包间转移。合约没有 ERC-20、支付、市场、版税或提款函数。

## 3. 玩家使用教程

1. 打开首页，右上角可以切换“中 / EN”。
2. 点击“连接钱包”，RabbyKit 会优先显示 Rabby，也支持浏览器内已注入的钱包；配置 WalletConnect Project ID 后可显示二维码钱包。
3. 第一次连接时切换到页面显示的网络，然后签署登录消息。该签名不消耗 Gas，也不会授权网站转走资产。
4. 完成原游戏的新手引导，进入小镇、聊天、锻造居民，玩法与上游保持一致。
5. 从顶部导航进入“链上 Agent”。
6. 在“创建 Agent 身份”中选择自己锻造的居民，点击创建，在钱包确认交易。这个 Passport 不可转让。
7. 在“保存与确权”中选择 Agent：
   - “训练 / 上传版本”：上传 Skill、模型清单或训练结果，并发布新版本；
   - “记忆快照”：上传自定义记忆文件；
   - “同步居民记忆上链”：由后端导出该居民当前真实记忆，再由钱包锚定；
   - “游戏存档”：上传存档文件；
   - “保存当前游戏状态”：保存角色外观和地图坐标，再由钱包锚定。
8. 交易确认后，下方 Passport 卡片会显示训练、记忆和存档的最新版本号。
9. 点击“恢复最新链上存档”时，页面先从合约读取最新 URI/哈希，再用钱包会话下载私有内容、重新计算 SHA-256；只有哈希、钱包和 Agent ID 都一致，才会恢复角色外观与地图坐标。

连接的钱包必须和当前登录钱包一致。切换到另一个钱包后，写入会被前端拒绝；合约还会再次校验 Passport 所有者。

## 4. 本地热加载

### 4.1 前置条件

- PowerShell 7；
- WSL Ubuntu + Python 3.11+；
- Node.js 24+ 与 npm；
- Rabby 或其他 EVM 浏览器钱包；
- 完整小镇建议启动 Redis；钱包 nonce 在 Redis 不可用时有单进程内存回退。

### 4.2 合约热节点

终端 A：

```powershell
cd contracts
npm ci
npm run node
```

终端 B：

```powershell
cd contracts
npm run deploy:local
```

把输出的 `SIMVERSE_AGENT_REGISTRY` 写入 `frontend/.env.local`：

```dotenv
VITE_API_URL=http://localhost:8000
VITE_WEB3_CHAIN_ID=31337
VITE_WEB3_CHAIN_NAME=Simverse Local
VITE_WEB3_RPC_URL=http://127.0.0.1:8545
VITE_AGENT_REGISTRY_ADDRESS=0x部署输出地址
```

Hardhat 每次重新启动都会清空本地链；之后必须重新部署并更新地址。只可在本地测试时导入 Hardhat 节点打印的公开测试私钥，绝不能给这个地址转入真实网络资产。

### 4.3 后端热加载

在 WSL 中：

没有本地 Redis 时，先开一个仅供开发的临时服务（关闭进程后数据清空）：

```bash
cd /mnt/c/Users/233/Documents/ChatGPT/赛博永生/backend
uv run python scripts/dev_fake_redis.py
```

另开 WSL 终端：

```bash
cd /mnt/c/Users/233/Documents/ChatGPT/赛博永生/backend
uv sync --extra dev
export DEBUG=true
export DATABASE_URL=sqlite+aiosqlite:///./skills_world_dev.db
export AUTO_CREATE_TABLES=true
export JWT_SECRET=local-web3-dev-secret-change-me-32-bytes
export LLM_API_KEY=test-dummy-key
export WEB3_CHAIN_ID=31337
export WEB3_CHAIN_NAME='Simverse Local'
export WEB3_URI=http://localhost:5173
export CORS_ORIGINS='["http://localhost:5173"]'
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

生产或已有数据库先运行：

```bash
uv run alembic upgrade head
```

迁移 `069_wallet_identity` 会给用户表增加唯一钱包地址。

### 4.4 前端热加载

```powershell
cd frontend
npm ci
npm run dev -- --host 0.0.0.0
```

打开 `http://localhost:5173`。Vite 会热更新 React 页面；Uvicorn `--reload` 会热更新后端；合约改动需要重新编译、部署或执行升级。

### 4.5 本地验收命令

```powershell
cd contracts
npm run compile
npm test
$env:SIMVERSE_AGENT_REGISTRY='0x代理地址'
npm run smoke:local
npm run upgrade:local
npm run smoke:local

cd ../frontend
npm test
npm run lint
npm run build
```

后端在 WSL 中：

```bash
cd backend
uv run pytest tests/test_auth.py tests/test_web3_content.py -q
```

## 5. Robinhood Chain 部署

项目目标链是 Robinhood Chain。共享验收先使用 Robinhood Chain Testnet（chain ID `46630`），正式网络使用 Robinhood Chain（chain ID `4663`）。两条网络都使用 ETH 支付 Gas：

| 网络 | Chain ID | RPC | 浏览器 |
|---|---:|---|---|
| Robinhood Chain Testnet | `46630` | `https://rpc.testnet.chain.robinhood.com` | `https://explorer.testnet.chain.robinhood.com` |
| Robinhood Chain | `4663` | `https://rpc.mainnet.chain.robinhood.com` | `https://robinhoodchain.blockscout.com` |

官方测试币入口是 `https://faucet.testnet.chain.robinhood.com`。公共 RPC 有速率限制，正式网站应换成 Alchemy、QuickNode 或其他 Robinhood Chain 基础设施商的专用端点。

根目录的 `私钥.txt` 已被 `.gitignore` 明确排除。文件只应包含 `0x` 开头的 32 字节 EVM 私钥；不要复制进命令历史、截图、日志、`.env` 或 Git。

先确认部署地址有足够 Robinhood Chain Testnet ETH，再在 PowerShell 进程内临时注入：

```powershell
cd contracts
$deployKey = (Get-Content -Raw -LiteralPath '..\私钥.txt').Trim()
$env:ROBINHOOD_PRIVATE_KEY = $deployKey
$env:ROBINHOOD_TESTNET_RPC_URL = 'https://rpc.testnet.chain.robinhood.com'
npm run deploy:robinhood-testnet
Remove-Item Env:ROBINHOOD_PRIVATE_KEY
```

部署脚本会输出 Proxy、Implementation、升级管理员，并写入 `contracts/deployments/46630.json`。网站只连接 Proxy 地址：

```dotenv
VITE_WEB3_CHAIN_ID=46630
VITE_WEB3_CHAIN_NAME=Robinhood Chain Testnet
VITE_WEB3_RPC_URL=https://rpc.testnet.chain.robinhood.com
VITE_AGENT_REGISTRY_ADDRESS=0xProxy地址
```

后端同步设置 `WEB3_CHAIN_ID=46630`、`WEB3_CHAIN_NAME="Robinhood Chain Testnet"` 和网站域名 `WEB3_URI`。前后端链 ID 不一致时，钱包登录会被拒绝。

本地验收通过并完成安全检查后，正式部署使用同一个脚本和 Robinhood Chain 主网配置：

```powershell
$env:ROBINHOOD_PRIVATE_KEY = $deployKey
$env:ROBINHOOD_RPC_URL = 'https://rpc.mainnet.chain.robinhood.com'
npm run deploy:robinhood
Remove-Item Env:ROBINHOOD_PRIVATE_KEY
```

正式网站使用 chain ID `4663`、名称 `Robinhood Chain`、主网 RPC 和 `contracts/deployments/4663.json` 里的 Proxy 地址。不要把 Testnet Proxy 配到主网网站。

### 当前 Robinhood Chain 主网部署

2026-09-01 已完成主网部署：

| 项目 | 地址 |
|---|---|
| UUPS Proxy（网站使用） | [`0x24f6f6bE48066cbE0B54d741cd4B52862Bb4b05c`](https://robinhoodchain.blockscout.com/address/0x24f6f6bE48066cbE0B54d741cd4B52862Bb4b05c) |
| Implementation | [`0xDb20c37F40a7715181Af7fA4A41117802FcD74f4`](https://robinhoodchain.blockscout.com/address/0xDb20c37F40a7715181Af7fA4A41117802FcD74f4) |
| Upgrade admin | `0x5e807ae9c82ba691fca0cc1f56eb01eb58d6f04c` |

升级安全所需的 OpenZeppelin manifest 保存在 `contracts/.openzeppelin/unknown-4663.json`，部署摘要保存在 `contracts/deployments/4663.json`。这两个文件必须随源码一起备份，升级前不得删除或重建。

## 6. 合约升级

升级前必须保留相同变量顺序，只能在末尾追加存储，并先运行合约测试。之后：

```powershell
cd contracts
$deployKey = (Get-Content -Raw -LiteralPath '..\私钥.txt').Trim()
$env:ROBINHOOD_PRIVATE_KEY = $deployKey
$env:ROBINHOOD_TESTNET_RPC_URL = 'https://rpc.testnet.chain.robinhood.com'
$env:SIMVERSE_AGENT_REGISTRY = '0xProxy地址'
npm run upgrade:robinhood-testnet
Remove-Item Env:ROBINHOOD_PRIVATE_KEY
```

Proxy 地址不变，网站无需改地址。生产环境应把 `UPGRADER_ROLE` 和 `DEFAULT_ADMIN_ROLE` 转移到多签，并把日常部署钱包从管理员中移除。

## 7. 安全边界

- 登录 challenge 绑定地址、链、来源域名、nonce、签发时间和 5 分钟过期时间；nonce 只能使用一次。
- 当前验证 EOA 的 EIP-191 签名；合约钱包的 ERC-1271 支持是后续项。
- RabbyKit 和合约调用都不会接触私钥；私钥始终由钱包持有。
- 内容接口要求已验证的钱包 JWT，并按用户隔离目录；链上 URI 公开可见，但下载仍要求同一身份会话。
- 迁移到公开去中心化存储前必须加密私密记忆，链上不要写明文隐私。
- Robinhood Chain Testnet 用于验收，不代表主网安全审计。正式上线前仍应做独立合约审计、多签、密钥轮换、备份和回滚演练。

## 8. English quick start

Simverse now uses a wallet as the public identity source and targets Robinhood Chain. RabbyKit connects the wallet; a one-time EIP-4361-style message creates a short game session without submitting a transaction. Agent creation, training/upload versions, memory snapshots, and game saves are anchored through the UUPS `SimverseAgentRegistry` proxy. Full content stays offchain; its hash, URI, ownership, and revision live onchain.

Run a Hardhat node, deploy locally, place the emitted proxy in `frontend/.env.local`, start FastAPI with chain ID `31337`, and start Vite. Open `/web3` after wallet login to create an Agent Passport and publish or restore training, memory, and save revisions. Use Robinhood Chain Testnet (`46630`) for shared testing and Robinhood Chain (`4663`) only after acceptance; keep all production upgrade roles behind a multisig.
