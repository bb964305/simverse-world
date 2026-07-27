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

The outer container is not treated as a multi-tenant execution boundary. Codex
keeps its `workspace-write` bubblewrap/seccomp sandbox, and every run is assigned
a distinct Linux UID and mode-0700 workspace. The ARM host uses rootless Docker,
so the outer Docker seccomp profile is disabled to permit bubblewrap's nested
user namespace; Codex establishes the restricted filesystem/network namespace
first and reapplies seccomp inside it. `no-new-privileges` and the outer
capability allowlist remain active. `/proc` is mounted with
`hidepid=2`. A root control process owns a per-run loopback credential proxy;
the real gateway token remains in that process and Codex receives only a random
proxy credential that is useless outside its run. CPU, memory, and PID limits
are enforced by controller-owned cgroup v2 children that the run UID cannot
modify. Those children remain below the Runtime container's own Docker scope,
so container teardown also owns their lifecycle. The entrypoint retains a
writable bind only to that run subtree, remounts the complete host cgroup view
read-only, and then drops `SYS_ADMIN`. Startup fails closed if any isolation
prerequisite is absent.

The Runtime has a 4 CPU / 8 GiB outer pool and admits either two low-tier runs
or one high-tier run at a time. Completed, failed, timed-out, and cancelled runs
all revoke their gateway grant and remove their workspace in `finally`.

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
`codex-runtime.env.example`. Create `secrets/runtime_api_key` as a mode-0400
file containing the Runtime control key; do not put it in an environment file
or commit it. With rootless Docker, the `secrets` directory and key file must be
owned by the user running the Docker daemon so it can traverse and bind-mount
them; keep the directory mode 0700 and the file mode 0400. The Lab backend uses
matching values:

```text
LAB_ADAPTER=codex
LAB_MODEL_GATEWAY_BASE_URL=http://model-gateway:8096/v1
LAB_MODEL_GATEWAY_AUTH_SECRET=<same dedicated gateway signing secret>
LAB_CODEX_BASE_URL=http://<runner-host>:8097
LAB_CODEX_API_KEY=<same dedicated runtime API key>
```

`LAB_MODEL_GATEWAY_BASE_URL` is grant metadata for the Runtime-internal service
alias; the Runtime pins its own configured gateway URL and does not trust this
caller-supplied value. Port 8096 is not published by Compose. Use a private
network policy so port 8097 accepts only the Lab backend. The gateway alone
needs outbound access to `dashscope.aliyuncs.com`; the Runtime never receives
the DashScope key.

Codex admission currently requires `LAB_TERMINALIZER_V2_ENABLED=false`. The v1
escrow terminalizer atomically refunds the task deposit minus metered model cost
using `ceil(cost_usd_cents * LAB_SC_PER_USD / 100)`. The PostgreSQL v2 financial
kernel still encodes full-refund-only commands, so the backend rejects that
combination before creating or funding a task; it must not be enabled until a
forward migration makes the database kernel cost-aware.

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

The test builds all three images, starts an ephemeral gateway on loopback port
18096, proves shell tool loops through both reward tiers, checks non-zero
provider usage, and then runs a complete Lab Runtime session that returns an
artifact. It also checks `hidepid=2`, the dropped `SYS_ADMIN` capability, the
per-run UID, and exact cgroup CPU/memory limits. Test containers are removed on
exit; the temporary Runtime API key is mounted from a mode-0600 file in a
private Docker volume and is never placed in the Runtime environment.
