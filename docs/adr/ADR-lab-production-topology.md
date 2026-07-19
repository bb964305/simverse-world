# ADR: Lab production trust topology (least-privilege boundaries)

- **Status: Proposed / topology documented + single-node enforced; full multi-service
  cluster pending real infrastructure**
- Date: 2026-07-20
- Context: recovery plan Phase 8 step 3 — "Document and enforce the production
  trust topology... If the deployment cannot isolate these boundaries, it cannot
  claim production capability."

## Trust zones (each a distinct identity + network policy)

The Lab is only production-capable when these six planes run under **separate
credentials, network policies, and least-privilege identities**. No single
process may hold the union of these grants.

| Plane | Runs | May reach | MUST NOT reach |
|---|---|---|---|
| **API / control plane** | `api` (FastAPI) | DB (app role), Redis, WS | the sandbox, real-runtime provider secrets, the object store's private bucket write path |
| **Lab Runner / runtime gateway** | `lab-runner` (`python -m app.lab.main`) | DB (runner role), Redis queue/control, the runtime provider endpoint via the Broker | the DB admin role, other tenants' data, host FS of the sandbox |
| **Broker / egress enforcement** | in-process in the Runner, but egress goes through an explicit proxy/firewall | only the allow-listed egress targets | arbitrary internet, cloud metadata (169.254.169.254) |
| **Sandbox executor** | rootless OCI container (Phase 8, dedicated Linux) | its scratch tmpfs only | host FS, Docker socket, network (`--network none`), any secret |
| **Artifact storage / scanner** | object store + scanner (Phase 5, when adopted) | the quarantine + released buckets it owns | the DB, the runtime provider |
| **World Governor** | proposal apply/revert path | the overlay + revision + outbox tables | financial ledgers, secrets |

## Enforced now (single node) vs pending (multi-service cluster)

**Enforced / proven on the dedicated Linux runner (evidence in
`docs/renders/`):**

- The `lab-runner` is a **separate deploy service** (`deploy/backend/docker-compose.yml`)
  from `api`/`agent-worker`, with its own healthcheck, restart policy, and DB/Redis
  deps, independently scalable, plus deploy-level + runtime kill switches.
- The **sandbox executor** proves the no-host-bind / no-network / no-docker-socket
  / non-root / read-only-rootfs / quota isolation contract on real rootless OCI
  (V11, `docs/renders/lab-oci-evidence/`).
- Secrets stay OUT of the sandbox: `OciExecutor._HOST_ENV_KEYS` forwards only the
  docker-CLI keys to the launcher, never to the container env.

**Pending — needs real multi-service cluster infrastructure (cannot be
provisioned single-node):**

- Distinct DB roles per plane (app / runner / governor) with row/table grants,
  and per-service network policies (e.g. only the Runner may reach the runtime
  provider; only the executor host runs containers).
- A separate egress-proxy service in front of the Broker.
- The artifact scanner + private object-store buckets (see
  `ADR-lab-artifact-storage.md`).
- Chaos/capacity drills across the isolated services with their real identities.

## Rule

A deployment that collapses any two of these planes into one identity **cannot
claim production capability** and must keep `lab_agent_v1_enabled=false` /
`lab_oci_enabled=false`. The single-node evidence proves the executor + runner +
kill-switch contracts; the multi-tenant identity/network isolation is the
remaining gate, and it requires the real cluster.
