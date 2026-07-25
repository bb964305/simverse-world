#!/usr/bin/env node
// V23 — asset provenance / release-packaging check (art-spec §资产来源与风险).
//
// Enforces that every third-party raster asset present in the distributable
// tree has a version-controlled provenance record (frontend/config/asset-provenance.json)
// and, in --release mode, that each such asset is actually cleared for
// distribution. Prevents shipping an unlicensed / unaudited asset.
//
// Modes:
//   (default)   integrity check — every present third-party tileset must have a
//               manifest entry AND its bytes must match the recorded hash. Exit
//               non-zero on a missing entry or a hash drift (an unrecorded edit).
//               Blocked/pending assets are REPORTED but tolerated (local
//               prototyping is allowed per art-spec item 2).
//   --release   packaging gate — additionally FAIL if any present third-party
//               asset is not (audit_status=cleared AND distribution_status=allowed).
//
// Node built-ins only (fs/path/crypto) — no new dependency, mirrors
// expand-town-map.mjs.
import { readFileSync, readdirSync, existsSync, statSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..', '..')
const MANIFEST = join(REPO, 'frontend', 'config', 'asset-provenance.json')
const VILLAGE_DIR = join(REPO, 'frontend', 'public', 'assets', 'village')
const TILEMAP_DIR = join(VILLAGE_DIR, 'tilemap')
const AGENT_DIR = join(VILLAGE_DIR, 'agents')
const AGENT_TEXTURE_CATEGORY = 'agents/*/texture.png'

const release = process.argv.includes('--release')

function sha16(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex').slice(0, 16)
}

function fail(msg) { console.error(`✗ ${msg}`) }

const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'))
const byFile = new Map(manifest.assets.filter((a) => !a.category).map((a) => [a.file, a]))

// The third-party raster packs the audit governs (art-spec: CuteRPG, Room
// Builder, interiors, plus the blocks helper). Enumerate what is actually on
// disk so a newly-added pack can't slip in unrecorded.
const present = existsSync(TILEMAP_DIR)
  ? readdirSync(TILEMAP_DIR).filter((f) => f.endsWith('.png')).map((f) => `tilemap/${f}`)
  : []

let errors = 0
let releaseBlocks = 0
const rows = []

for (const rel of present) {
  const entry = byFile.get(rel)
  if (!entry) {
    fail(`no provenance record for distributed asset: ${rel}`)
    errors++
    rows.push([rel, 'NO RECORD', '-', '-'])
    continue
  }
  const actual = sha16(join(TILEMAP_DIR, rel.replace('tilemap/', '')))
  const drift = entry.sha256_16 !== 'VARIES' && actual !== entry.sha256_16
  if (drift) {
    fail(`hash drift for ${rel}: recorded ${entry.sha256_16} but bytes are ${actual} — update the manifest + record the modification`)
    errors++
  }
  const cleared = entry.audit_status === 'cleared' && entry.distribution_status === 'allowed'
  if (release && !cleared) {
    fail(`RELEASE BLOCKED: ${rel} is audit=${entry.audit_status} distribution=${entry.distribution_status} (needs cleared+allowed)`)
    releaseBlocks++
  }
  rows.push([rel, entry.audit_status, entry.distribution_status, drift ? 'DRIFT' : 'ok'])
}

// Resident sheets are represented by one category record. Count the concrete
// files so additions cannot bypass the release gate under an unchanged glob.
const agentTextures = existsSync(AGENT_DIR)
  ? readdirSync(AGENT_DIR, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => join(AGENT_DIR, entry.name, 'texture.png'))
      .filter((path) => existsSync(path) && statSync(path).isFile())
  : []

if (agentTextures.length > 0) {
  const entry = manifest.assets.find((asset) => asset.category && asset.file === AGENT_TEXTURE_CATEGORY)
  const label = `${AGENT_TEXTURE_CATEGORY} (${agentTextures.length} files)`
  if (!entry) {
    fail(`no provenance category for distributed resident textures: ${label}`)
    errors++
    rows.push([label, 'NO RECORD', '-', '-'])
  } else {
    const countMatches = entry.file_count === agentTextures.length
    if (!countMatches) {
      fail(`category count drift for ${AGENT_TEXTURE_CATEGORY}: recorded ${entry.file_count} but found ${agentTextures.length}`)
      errors++
    }
    const cleared = entry.audit_status === 'cleared' && entry.distribution_status === 'allowed'
    if (release && !cleared) {
      fail(`RELEASE BLOCKED: ${label} is audit=${entry.audit_status} distribution=${entry.distribution_status} (needs cleared+allowed)`)
      releaseBlocks += agentTextures.length
    }
    rows.push([label, entry.audit_status, entry.distribution_status, countMatches ? 'count ok' : 'COUNT DRIFT'])
  }
}

// Report any manifest entries whose files are absent (stale record).
for (const [file] of byFile) {
  const abs = join(REPO, 'frontend', 'public', 'assets', 'village', file)
  if (!existsSync(abs)) rows.push([file, '(absent)', '-', 'not on disk'])
}

const fileWidth = Math.max(37, ...rows.map(([file]) => file.length + 1))
console.log(`\nAsset provenance (${manifest.task})`)
console.log(`${'file'.padEnd(fileWidth)} audit      distribution  integrity`)
for (const [f, a, d, b] of rows) {
  console.log(`${f.padEnd(fileWidth)} ${String(a).padEnd(10)} ${String(d).padEnd(13)} ${b}`)
}
console.log('')

if (errors > 0) {
  console.error(`FAILED integrity check: ${errors} problem(s). Every distributed third-party asset needs a matching provenance record.`)
  process.exit(1)
}
if (release && releaseBlocks > 0) {
  console.error(`FAILED release gate: ${releaseBlocks} asset file(s) not cleared for distribution. Complete the remaining provenance audit before releasing.`)
  process.exit(1)
}
if (release) {
  console.log('✓ release gate passed: all present third-party assets are cleared for distribution.')
} else {
  const blocked = rows.filter((r) => r[2] === 'blocked' || r[2] === 'prototype-only').length
  console.log(`✓ integrity check passed: all present third-party assets are recorded; concrete tilemaps are byte-matched and categories are count-matched.`)
  if (blocked > 0) console.log(`  note: ${blocked} asset record(s) are prototype-only/blocked — run with --release to see the packaging gate.`)
}
