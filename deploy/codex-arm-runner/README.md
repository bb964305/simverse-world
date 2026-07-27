# Codex ARM runner and model gateway

This deployment connects current Codex to Alibaba Cloud DeepSeek through a
narrow Responses-to-Chat gateway. Codex only sees the virtual model
`lab-auto`; the signed run token selects exactly one provider model:

| Reward tier | Provider model | Per-run compute |
| --- | --- | --- |
| low (`< 100 SC`) | `deepseek-v4-flash` | 2 CPU / 2 GiB |
| high (`>= 100 SC`) | `deepseek-v4-pro` | 4 CPU / 4 GiB |

The gateway holds the DashScope API key and a durable SQLite usage ledger. The
Codex Runtime receives only a short-lived, run-scoped token. Codex-launched
shell commands inherit a clean environment that excludes keys, secrets, and
tokens.

The ARM image uses its outer container as the execution boundary, so its
runtime configuration sets Codex's inner sandbox to `danger-full-access`.
That access is limited to a non-root container with a read-only root filesystem,
all Linux capabilities dropped, `no-new-privileges`, resource limits, and an
internal-only runtime network. Only the gateway joins the egress network.
The Runtime has a 4 CPU / 8 GiB outer pool and admits either two low-tier runs
or one high-tier run at a time. Linux CPU affinity and `RLIMIT_AS` apply the
per-run limits before the Codex process starts.

## Images

- `Dockerfile.gateway`: `/v1/responses` facade and usage ledger.
- `Dockerfile.runtime`: Lab HTTP Adapter runtime with Codex 0.144.4 for ARM64.
- `Dockerfile`: one-shot ARM64 Codex probe used by the functional test.
- `runtime-proxy.conf`: keyless ingress proxy into the internal runtime network.

Codex 0.144.4 is the latest stable release verified on 2026-07-27. The ARM64
release archive is pinned to SHA-256
`4d07243ef4ae6786b8b321d7aea3f9be4e1d2c597ae5407e7c1b9873334082b2`.

## Configuration

Create deployment-local files from `model-gateway.env.example` and
`codex-runtime.env.example`. Do not commit them. The Lab backend uses matching
values:

```text
LAB_ADAPTER=codex
LAB_MODEL_GATEWAY_BASE_URL=http://<runner-host>:8096/v1
LAB_MODEL_GATEWAY_AUTH_SECRET=<same dedicated gateway signing secret>
LAB_CODEX_BASE_URL=http://<runner-host>:8097
LAB_CODEX_API_KEY=<same dedicated runtime API key>
```

Use a private network policy so ports 8096 and 8097 accept only the Lab backend
and ARM-local containers. The gateway alone needs outbound access to
`dashscope.aliyuncs.com`; the Runtime must not receive the DashScope key.

Start the services from this directory with:

```bash
docker compose up -d --build
```

## Functional verification

On the ARM host, with the repository and an environment file containing
`LLM_API_KEY`, run:

```bash
ENV_FILE=/secure/path/model.env ./functional-test.sh
```

The test builds all three images, starts an ephemeral gateway, proves shell tool
loops through both reward tiers, checks non-zero provider usage, and then runs a
complete Lab Runtime session that returns an artifact. Test containers are
removed on exit; the API key is passed only as a runtime environment variable.
