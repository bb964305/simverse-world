# Lab Agent asset-clearance lane

Status: replacement required; release gate remains red.

The 2026-07-21 release verifier reports 16 concrete third-party files as
`audit_status=pending` and `distribution_status=blocked`. The manifest also has
one category record covering the resident texture sheets. No authoritative
purchase/license evidence is available in the repository or protected external
release inputs, so none may be reclassified as cleared.

## Disposition

| Current group | Count | Decision |
|---|---:|---|
| `CuteRPG_*` environment sheets | 9 | Replace with original Simverse-owned outdoor atlas content and remap every used tile. |
| LimeZu Room Builder/interior sheets | 6 | Replace with original Simverse-owned indoor atlas content; remove raw source-pack bytes from the release tree. |
| `blocks_1.png` | 1 | Replace with first-party collision data derived from the authoritative map, not copied art. |
| `agents/*/texture.png` category | 25 sheets | Replace with original resident sprite sheets and record each final file hash individually. |

Replacement is not complete merely because a file renders. AC20 stays red until
the final manifest records author/source, creation method, license/rights,
modifications, commercial and derivative permission, full SHA-256, and
`cleared/allowed` for every shipped file. Map digest, raw collision reachability,
frontend/backend byte identity, and the full visual matrix must be rerun after
replacement.

Generated placeholders do not satisfy this lane. Replacement art must be a
coherent, inspectable Simverse asset set with editable source or reproducible
generation provenance and a rights declaration that permits repository and
commercial distribution.

