# Resident Sprite Operations

This runbook covers the administrator-only image workflow. It does not authorize provider spend. Run it only in staging with an approved capability receipt, reviewed price evidence, and an explicit batch budget approval.

## Static Batch Boundary

The M3 batch replaces 25 reusable static sprite slots. It is separate from the environment-specific resident table and the current 11 built-in NPC identities. The only generation input catalog is `frontend/config/resident-sprite-generation.json`; legacy PNG files are never provider references.

Each slot receives one run per batch. A rejected or quarantined result ends that batch attempt. Prepare and approve a new batch rather than creating an unaccounted derived run.

## 1. Qualify the Provider

Use `backend/scripts/generate_resident_sprite.py` to run `probe-wire`, `qualify-generate`, and `qualify-review`. The operator and reviewer must differ. Capability qualification is a separate paid authorization from the 275-request static batch: the wire probe can submit up to two requests while negotiating the multipart field, followed by exactly five A/B qualification requests, for a maximum of seven.

Configure the provider and a durable, non-public qualification evidence root before any paid command. Do not expose the API key to the API container:

```bash
export RESIDENT_SPRITE_PROVIDER_BASE_URL='https://approved-provider.example'
export RESIDENT_SPRITE_PROVIDER_API_KEY='...'
export RESIDENT_SPRITE_PROVIDER_MODEL='approved-model-alias'
export RESIDENT_SPRITE_QUALIFICATION_ROOT='/durable/private/resident-sprite-qualification'
```

Public provider origins require HTTPS. A disposable test endpoint may be used
only with the explicit process-local
`RESIDENT_SPRITE_ALLOW_INSECURE_HTTP_TEST=true` override. The resulting
capability is permanently marked `insecure_http_test` and cannot be consumed
unless the worker repeats the same opt-in. Never enable this for production
credentials or production generation.
The same opt-in also permits a public HTTP image-result URL returned by an
HTTPS provider during disposable testing. Provider credentials are never sent
to that result URL; private or mixed DNS, redirects, oversized bodies, and
invalid PNG dimensions still fail closed.
The v2 capability also records `bounded-center-fit-v1`. When a relay ignores
the requested size, a single-frame PNG is normalizable only when both edges
are at least 512 px, total pixels stay between 655,360 and 8,294,400, and its
aspect ratio is at most 3:1. Landscape strips are center-fit without aspect
distortion; portrait qualification sheets are padded on the magenta
background. All normalized bytes are hashed into qualification and run
evidence, and automatic QC still rejects unusable panel composition.

Freeze the reviewed per-request upper bound on the wire probe, then repeat the exact request and cost confirmation before A/B generation:

```bash
cd backend
.venv/bin/python scripts/generate_resident_sprite.py probe-wire \
  --spec '<qualification-spec.json>' \
  --operator '<capability-operator>' \
  --price-per-request-usd '<reviewed-upper-bound>' \
  --confirm-max-requests 7 \
  --confirm-max-cost-usd '<at-least-7-times-upper-bound>' \
  --cost-source '<reviewed-price-evidence>'

.venv/bin/python scripts/generate_resident_sprite.py qualify-generate \
  --spec '<qualification-spec.json>' \
  --wire-receipt '<wire-receipt-id>' \
  --operator '<same-capability-operator>' \
  --confirm-max-requests 7 \
  --confirm-max-cost-usd '<same-frozen-max-cost>'

.venv/bin/python scripts/generate_resident_sprite.py qualify-review \
  --qualification-id '<qualification-id>' \
  --reviewer '<different-capability-reviewer>' \
  --scores '<blind-scores.json>'
```

The resulting receipt records the actual combined request count (six or seven), the authorized upper bound, and the price evidence source. Configure the reviewed receipt and runtime storage for generation:

```bash
export RESIDENT_SPRITE_REQUEST_COST_UPPER_BOUND_USD='<reviewed-per-request-upper-bound>'
export RESIDENT_SPRITE_CAPABILITY_RECEIPT='/secure/path/capability.json'
export RESIDENT_SPRITE_REVOCATION_ROOT='/secure/path/revocations'
export RESIDENT_SPRITE_ARTIFACT_DIR='/durable/private/resident-sprite-runs'
```

Do not expose the API key to the API container. Only the isolated worker or this staging operator process needs it.
The v2 adapter prefers inline `b64_json`. For compatible relays that still
return a signed image URL, it downloads exactly one public HTTPS URL without
provider credentials, redirects, proxies, or cookies, then enforces the same
25 MiB, PNG, pixel-count, and exact-dimension checks. Private, loopback,
reserved, non-443, or mixed public/private DNS results fail closed.

For Compose deployment, keep provider credentials only in
`deploy/backend/resident-sprite-worker.env` (created from the checked-in
`.example`). The shared `deploy/backend/.env` is injected into the API and must
not contain any `RESIDENT_SPRITE_PROVIDER_*` secret.
The admin workflow reports this configured request-cost upper bound multiplied by
the durable submitted-request count. It is a conservative budget signal, not a
provider invoice; when the setting is zero, the UI reports the cost as unknown.

## 2. Freeze the Batch and Price Ceiling

Record a conservative per-request upper bound and its authoritative source. `prepare` makes no provider calls.

```bash
cd backend
.venv/bin/python scripts/manage_resident_sprite_batch.py prepare \
  --batch-root /durable/private/resident-sprite-batches \
  --model approved-model-alias \
  --price-per-request-usd '<upper-bound>' \
  --max-cost-usd '<at-least-275-times-upper-bound>' \
  --cost-source '<reviewed-price-evidence>'
```

Archive the canonical stdout. Review all 25 request specifications and the reported `max_requests_total=275` before authorizing spend.

Provider evidence prefers an allowlisted request ID from response headers or JSON. If a relay
returns neither, the adapter persists only `result-url-sha256:<digest>` for the signed result URL;
the raw URL and its query parameters are never written to receipts or logs.

## 3. Execute the Paid Batch

All three confirmations must exactly match the frozen batch. The command runs slots sequentially and persists each run ID before its first provider request.

```bash
.venv/bin/python scripts/manage_resident_sprite_batch.py generate \
  --batch-root /durable/private/resident-sprite-batches \
  --artifact-root /durable/private/resident-sprite-runs \
  --batch-id '<batch-id>' \
  --confirm-batch-id '<batch-id>' \
  --confirm-max-requests 275 \
  --confirm-max-cost-usd '<frozen-max-cost>'
```

Use `--asset-key <key>` for a controlled single-slot execution. If a provider result is uncertain, inspect the run with `generate_resident_sprite.py recover`; do not issue another request until the external status is reconciled.

When a newly authorized target batch and an older source batch contain complementary
automatic-QC results, carry forward eligible immutable runs without provider calls:

```bash
.venv/bin/python scripts/manage_resident_sprite_batch.py consolidate \
  --batch-root /durable/private/resident-sprite-batches \
  --batch-id '<target-batch-id>' \
  --artifact-root /durable/private/target-resident-sprite-runs \
  --source-batch-id '<source-batch-id>' \
  --source-artifact-root /durable/private/source-resident-sprite-runs
```

Consolidation fails closed unless the catalog, source policy, model, baseline tree,
price snapshot, request hash, and capability receipt match exactly. Only original
`auto_qc_passed` source runs are eligible; chained imports are rejected. Any failed
target run remains in the audit trail and its submitted request count is added to
the selected run before the per-slot and batch ceilings are checked. Each import
also writes immutable evidence under the target batch's `consolidations/` directory.

## 4. Review Every Candidate in Phaser

Start the loopback-only batch review surface. It serves the private candidates by manifest hash, uses the repository's local Phaser bundle, and prints a one-time token URL. It never exposes the artifact root as a static directory.

```bash
cd backend
.venv/bin/python scripts/review_resident_sprite_batch.py \
  --batch-root /durable/private/resident-sprite-batches \
  --artifact-root /durable/private/resident-sprite-runs \
  --batch-id '<batch-id>' \
  --reviewer '<approved-reviewer-id>'
```

Inspect all four animated directions in the browser. Approval requires all nine checklist items and stores a deterministic `640x360` Phaser canvas screenshot, the exact 12-frame set, Phaser version, candidate texture hash, reviewer, notes, and timestamp. Rejection requires a reason and quarantines the run. The standalone `generate_resident_sprite.py review-phaser` command does not create this screenshot evidence and is therefore insufficient for an M3 static install.

Reviewers used by the final installer must be explicitly allowlisted on the install command and must differ from the capability operator.

The batch cannot install unless all 25 manifests are `human_approved`, automatic QC has no findings, capability evidence is present, every run has provider request IDs, and every approval has valid Phaser screenshot evidence.

## 5. Install and Verify

The installer validates the frozen baseline, old-asset denylist, unique image hashes, portrait derivation, all review evidence, and 50 per-file receipts. It stages a complete sibling tree and switches it with a lock and durable recovery journal.

```bash
.venv/bin/python scripts/manage_resident_sprite_batch.py install \
  --batch-root /durable/private/resident-sprite-batches \
  --artifact-root /durable/private/resident-sprite-runs \
  --batch-id '<batch-id>' \
  --reviewer '<approved-reviewer-id>'

cd ../frontend
npm run assets:verify:release
npm run build
npm run assets:verify:dist
```

Both release gates must pass. The build automatically uses the installed batch ID as the cache version for texture, portrait, and atlas URLs.

## Recovery

If installation stops after the journal is written, all release gates fail closed. Inspect the paths recorded in `.resident-sprite-install.json`, then explicitly finish or roll back:

```bash
cd backend
.venv/bin/python scripts/manage_resident_sprite_batch.py recover-install --action finish
# or
.venv/bin/python scripts/manage_resident_sprite_batch.py recover-install --action rollback
```

Recovery compares complete old and new tree hashes and refuses unknown path contents. Do not manually delete staging, backup, lock, or journal files.
