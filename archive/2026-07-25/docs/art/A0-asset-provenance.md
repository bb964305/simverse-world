# A0 — Asset Provenance & Licensing Audit

**Status: audit OPEN — third-party art is `blocked` for distribution.** This is the
art-line prerequisite gate (art-spec §完成定义 / §资产来源与风险; V23). No derived
atlas from these packs may be exported for public release until the audit clears.

Machine-readable source of truth: [`asset-provenance.json`](./asset-provenance.json).
Release-packaging gate: `frontend/scripts/verify-asset-provenance.mjs` (also
`npm run assets:verify` / `npm run assets:verify:release`).

## Inventory (third-party, bundled)

All live under `frontend/public/assets/village/` and were committed 2026-07-06 (as-is).

| Pack | Files | Dims | Likely upstream | License status |
|---|---|---|---|---|
| CuteRPG_* | 9 × `tilemap/CuteRPG_*.png` | 512×512 | Bundled in Stanford `joonspk-research/generative_agents` (Smallville). Original pack author/store **unconfirmed**. | **UNVERIFIED** |
| Room Builder | `tilemap/Room_Builder_32x32.png` | 2432×3488 | **LimeZu — Modern Interiors** (itch.io), 32× variant (likely) | paid-tier commercial+edit, **no source redistribution**, credit required — confirm |
| interiors | `tilemap/interiors_pt1..5.png` | 512×~10k | **LimeZu — Modern Interiors** (likely) | same as Room Builder |
| blocks helper | `tilemap/blocks_1.png` | 320×256 | Bundled in generative_agents map assets | **UNVERIFIED** |
| Character sprites | 25 × `agents/<name>/texture.png` | 96×128 | Character sprites bundled in generative_agents (repacked into per-agent atlas) | **UNVERIFIED** |

## Findings

1. **No authorization record existed in the repo** for any of these (art-spec risk row
   confirms: "仓库未找到 CuteRPG、Room Builder、interiors 的授权记录"). This manifest
   is the first version-controlled record.
2. **LimeZu Modern Interiors** (Room Builder + interiors): the license permits commercial
   use and editing under a paid tier (≥ $1.50), requires credit, and **forbids
   redistributing/reselling the source asset**. Committing the raw PNGs to a public repo
   *distributes the source* — a likely violation even with a valid purchase. Two follow-ups:
   confirm the purchase/tier for the exact copy here, and ensure release builds ship only
   the **composed tilemap output**, not the raw packs, with the required LimeZu credit.
   ([limezu.itch.io/moderninteriors](https://limezu.itch.io/moderninteriors))
3. **CuteRPG_\*** and **blocks_1** and **character sprites**: origin unconfirmed. One
   secondary source claims the Smallville tileset is by LimeZu, but the `CuteRPG_*`
   pack's original author/store/license is not verified. Treat as unlicensed until a
   primary-source license is found.

## Required actions to clear the gate (per file → set `audit_status: cleared`, `distribution_status: allowed`)

1. For LimeZu packs: attach proof of purchase + the exact license tier; add the credit
   string to the game credits; move raw source packs out of the public/release build
   (keep only the composed tilemap output) OR obtain a redistribution grant.
2. For CuteRPG / blocks / sprites: locate the primary-source license. If none can be
   confirmed, **replace** with a known-licensed pack (or self-made/commissioned art with
   recorded source files) before any public release.
3. Any new/self-made/generated asset: record source file, working file, palette, export
   settings; generated art additionally records the generation source and passes manual
   pixel cleanup (art-spec item 3).

## How the V23 gate enforces this

- `node frontend/scripts/verify-asset-provenance.mjs` — integrity: every distributed
  third-party asset must have a manifest entry and byte-match its recorded hash (catches
  unrecorded edits and unrecorded new packs). Blocked assets are tolerated for **local
  prototyping**.
- `node frontend/scripts/verify-asset-provenance.mjs --release` — packaging gate: **fails**
  unless every present third-party asset is `audit_status=cleared` AND
  `distribution_status=allowed`. Wire this into the release/publish pipeline so an
  unaudited asset can never ship.

Current result: integrity ✓ (all recorded + byte-matched); `--release` ✗ (16 assets
blocked) — the correct fail-closed state until the audit is completed.
