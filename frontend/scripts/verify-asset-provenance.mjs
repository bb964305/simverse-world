#!/usr/bin/env node
// Release gate for third-party tilemaps, resident sprites, and caravan atlases.
import { readFileSync, readdirSync, existsSync, lstatSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { inflateSync } from 'node:zlib'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve, relative } from 'node:path'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = resolve(HERE, '..', '..')
const MANIFEST = process.env.SIMVERSE_ASSET_MANIFEST || join(REPO, 'frontend', 'config', 'asset-provenance.json')
const CATALOG = process.env.SIMVERSE_SPRITE_CATALOG || join(REPO, 'frontend', 'config', 'resident-sprite-generation.json')
const DENYLIST = process.env.SIMVERSE_SPRITE_DENYLIST || join(REPO, 'frontend', 'config', 'resident-sprite-legacy-denylist.json')
const useDist = process.argv.includes('--dist')
const VILLAGE_DIR = process.env.SIMVERSE_ASSET_VILLAGE_DIR || (useDist
  ? join(REPO, 'frontend', 'dist', 'assets', 'village')
  : join(REPO, 'frontend', 'public', 'assets', 'village'))
const TILEMAP_DIR = join(VILLAGE_DIR, 'tilemap')
const AGENT_DIR = join(VILLAGE_DIR, 'agents')
const CARAVAN_DIR = join(VILLAGE_DIR, 'caravan')
const release = process.argv.includes('--release') || useDist
const RECEIPT_POLICY_FILE = 'agents/*/{texture,portrait}.png'
const HEX64 = /^[0-9a-f]{64}$/

// The resident installer deliberately verifies an isolated fixture whose
// village root contains only `agents/`.  Treat only that exact explicit
// override shape as a resident-scoped gate.  Source/dist trees, and fuller
// override trees with a missing caravan directory, still require the complete
// caravan contract below.
const explicitVillageOverride = Boolean(process.env.SIMVERSE_ASSET_VILLAGE_DIR)
const overrideRootEntries = explicitVillageOverride && directory(VILLAGE_DIR)
  ? readdirSync(VILLAGE_DIR).sort()
  : []
const agentsOnlyOverride = explicitVillageOverride && directory(AGENT_DIR) &&
  overrideRootEntries.includes('agents') && overrideRootEntries.every((name) =>
    name === 'agents' || (name === '.resident-sprite-install.lock' && regular(join(VILLAGE_DIR, name))))

let errors = 0
let releaseBlocks = 0
const rows = []
function fail(message) { console.error(`x ${message}`); errors++ }
function block(message, count = 1) {
  if (release) console.error(`x RELEASE BLOCKED: ${message}`)
  releaseBlocks += count
}
function sha256(path) { return createHash('sha256').update(readFileSync(path)).digest('hex') }
function sha16(path) { return sha256(path).slice(0, 16) }
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}
function canonicalSha(value) { return createHash('sha256').update(canonical(value)).digest('hex') }
function regular(path) { return existsSync(path) && lstatSync(path).isFile() && !lstatSync(path).isSymbolicLink() }
function directory(path) { return existsSync(path) && lstatSync(path).isDirectory() && !lstatSync(path).isSymbolicLink() }
function parseJson(path, label) {
  try {
    if (!regular(path)) throw new Error('not a regular file')
    return JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    fail(`${label} is missing, unsafe, or invalid JSON`)
    return null
  }
}

// Decode the exact RGBA/non-interlaced PNG contract emitted by Pillow. This lets
// the gate verify that portrait.png is the nearest-neighbor enlargement of the
// down-idle frame without adding a release-time npm dependency.
function decodeRgbaPng(path) {
  const data = readFileSync(path)
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])
  if (data.length < 33 || !data.subarray(0, 8).equals(signature)) throw new Error('bad PNG signature')
  let offset = 8
  let width; let height; let seenIhdr = false; let seenIend = false
  const idat = []
  while (offset + 12 <= data.length) {
    const length = data.readUInt32BE(offset)
    const type = data.toString('ascii', offset + 4, offset + 8)
    const start = offset + 8; const end = start + length
    if (end + 4 > data.length) throw new Error('truncated PNG chunk')
    if (type === 'IHDR') {
      if (seenIhdr || length !== 13) throw new Error('bad IHDR')
      seenIhdr = true
      width = data.readUInt32BE(start); height = data.readUInt32BE(start + 4)
      if (data[start + 8] !== 8 || data[start + 9] !== 6 || data[start + 10] !== 0 || data[start + 11] !== 0 || data[start + 12] !== 0) {
        throw new Error('PNG must be 8-bit non-interlaced RGBA')
      }
    } else if (type === 'IDAT') idat.push(data.subarray(start, end))
    else if (type === 'IEND') { seenIend = true; offset = end + 4; break }
    offset = end + 4
  }
  if (!seenIhdr || !seenIend || offset !== data.length || idat.length === 0) throw new Error('incomplete PNG')
  const packed = inflateSync(Buffer.concat(idat))
  const stride = width * 4
  if (packed.length !== height * (stride + 1)) throw new Error('unexpected PNG scanline size')
  const pixels = Buffer.alloc(width * height * 4)
  let source = 0
  for (let y = 0; y < height; y++) {
    const filter = packed[source++]
    const row = y * stride
    for (let x = 0; x < stride; x++) {
      const raw = packed[source++]
      const left = x >= 4 ? pixels[row + x - 4] : 0
      const up = y > 0 ? pixels[row - stride + x] : 0
      const upLeft = y > 0 && x >= 4 ? pixels[row - stride + x - 4] : 0
      let value
      if (filter === 0) value = raw
      else if (filter === 1) value = raw + left
      else if (filter === 2) value = raw + up
      else if (filter === 3) value = raw + Math.floor((left + up) / 2)
      else if (filter === 4) {
        const p = left + up - upLeft
        const pa = Math.abs(p - left); const pb = Math.abs(p - up); const pc = Math.abs(p - upLeft)
        value = raw + (pa <= pb && pa <= pc ? left : pb <= pc ? up : upLeft)
      } else throw new Error('unknown PNG filter')
      pixels[row + x] = value & 255
    }
  }
  return { width, height, pixels, bytes: data.length }
}
function portraitMatches(texture, portrait) {
  if (texture.width !== 96 || texture.height !== 128 || portrait.width !== 256 || portrait.height !== 256) return false
  for (let y = 0; y < 256; y++) for (let x = 0; x < 256; x++) {
    const sourceX = 32 + Math.floor(x / 8); const sourceY = Math.floor(y / 8)
    const si = (sourceY * texture.width + sourceX) * 4
    const pi = (y * portrait.width + x) * 4
    for (let channel = 0; channel < 4; channel++) if (texture.pixels[si + channel] !== portrait.pixels[pi + channel]) return false
  }
  return true
}

function validateGridAtlas(atlas, action, size, anchorY) {
  if (!atlas || atlas.meta?.image !== 'texture.png' || atlas.meta?.format !== 'RGBA8888' ||
      atlas.meta?.size?.w !== size * 3 || atlas.meta?.size?.h !== size * 4) return false
  const frames = new Map((atlas.frames || []).map((entry) => [entry.filename, entry]))
  if (frames.size !== 20) return false
  for (const [row, direction] of ['down', 'left', 'right', 'up'].entries()) {
    const names = [
      `${direction}-${action}.000`, `${direction}-${action}.001`,
      `${direction}-${action}.002`, `${direction}-${action}.003`, direction,
    ]
    const xs = [0, size, size * 2, size, size]
    for (let index = 0; index < names.length; index++) {
      const entry = frames.get(names[index])
      const frame = entry?.frame
      if (!frame || frame.x !== xs[index] || frame.y !== row * size ||
          frame.w !== size || frame.h !== size ||
          entry.anchor?.x !== 0.5 || entry.anchor?.y !== anchorY) return false
    }
  }
  return true
}

function validateCaravanPng(path, record, width, height) {
  if (!record || record.file !== path.split('/').at(-1) || record.width !== width ||
      record.height !== height || record.mode !== 'RGBA' || record.bytes !== readFileSync(path).length ||
      record.sha256 !== sha256(path) || !HEX64.test(record.sha256 || '')) return false
  try {
    const decoded = decodeRgbaPng(path)
    if (decoded.width !== width || decoded.height !== height) return false
    const palette = new Set(); let opaquePixels = 0
    for (let offset = 0; offset < decoded.pixels.length; offset += 4) {
      const red = decoded.pixels[offset]; const green = decoded.pixels[offset + 1]
      const blue = decoded.pixels[offset + 2]; const alpha = decoded.pixels[offset + 3]
      if (alpha !== 0 && alpha !== 255) return false
      if (alpha === 0) palette.add('transparent')
      else {
        opaquePixels++
        palette.add(`${red},${green},${blue},255`)
        if (red >= 240 && green <= 24 && blue >= 240) return false
      }
    }
    return opaquePixels > 0 && palette.size <= 32
  } catch {
    return false
  }
}

const manifest = parseJson(MANIFEST, 'asset provenance manifest')
const catalog = parseJson(CATALOG, 'resident sprite catalog')
const denylistDoc = parseJson(DENYLIST, 'legacy sprite denylist')
if (!manifest || !catalog || !denylistDoc) process.exit(1)
if (manifest.schema_version !== 2) fail('asset provenance manifest schema_version must be 2')
if (catalog.schema_version !== 1 || catalog.source_policy !== 'first_party_text_spec_no_visual_reference' || catalog.slots?.length !== 25) {
  fail('resident sprite catalog must contain exactly 25 no-visual-reference slots')
}
if (denylistDoc.schema_version !== 1 || denylistDoc.sha256?.length !== 50 || new Set(denylistDoc.sha256).size !== 50 || denylistDoc.sha256.some((hash) => !HEX64.test(hash))) {
  fail('legacy resident denylist must contain 50 unique full SHA-256 values')
}
const denied = new Set(denylistDoc.sha256)
const byFile = new Map()
for (const asset of manifest.assets.filter((asset) => !asset.category)) {
  if (byFile.has(asset.file)) fail(`duplicate provenance record: ${asset.file}`)
  byFile.set(asset.file, asset)
}
const policy = manifest.assets.find((asset) => asset.category && asset.file === RECEIPT_POLICY_FILE)
if (!policy || policy.file_count !== 50 || policy.receipt_policy !== 'resident-generation-provenance-v1') {
  fail('resident generated-file receipt policy is missing or invalid')
}

// Concrete third-party tilemaps retain their checked-in hash and owner/license gate.
const presentTilemaps = directory(TILEMAP_DIR)
  ? readdirSync(TILEMAP_DIR).filter((file) => file.endsWith('.png')).map((file) => `tilemap/${file}`)
  : []
for (const rel of presentTilemaps) {
  const entry = byFile.get(rel)
  if (!entry) { fail(`no provenance record for distributed asset: ${rel}`); continue }
  const actual = sha16(join(VILLAGE_DIR, rel))
  const drift = actual !== entry.sha256_16
  if (drift) fail(`hash drift for ${rel}: recorded ${entry.sha256_16}, found ${actual}`)
  const cleared = entry.audit_status === 'cleared' && entry.distribution_status === 'allowed'
  if (!cleared) block(`${rel} is not cleared+allowed`)
  rows.push([rel, entry.audit_status, entry.distribution_status, drift ? 'DRIFT' : 'ok'])
}
for (const [file] of byFile) if (!existsSync(join(VILLAGE_DIR, file))) rows.push([file, '(absent)', '-', 'not on disk'])

const catalogSha = canonicalSha(catalog)
const slotKeys = catalog.slots.map((slot) => slot.sprite_key)
if (new Set(slotKeys).size !== 25 || new Set(catalog.slots.map((slot) => slot.asset_key)).size !== 25) fail('catalog slot keys must be unique')
if (!directory(AGENT_DIR)) fail('canonical agents directory is missing or unsafe')
const parent = dirname(AGENT_DIR)
if (!useDist && (existsSync(join(parent, '.resident-sprite-install.json')) || readdirSync(parent).some((name) => name.startsWith('.agents-stage-') || name.startsWith('.agents-backup-') || name.startsWith('.agents-discard-')))) {
  fail('unfinished resident sprite install/recovery artifacts are present')
}
const rootEntries = directory(AGENT_DIR) ? readdirSync(AGENT_DIR, { withFileTypes: true }) : []
const actualDirs = rootEntries.filter((entry) => entry.isDirectory() && !entry.isSymbolicLink()).map((entry) => entry.name).sort()
const expectedDirs = [...slotKeys].sort()
if (JSON.stringify(actualDirs) !== JSON.stringify(expectedDirs)) fail('agent directories do not exactly match the canonical 25 slots')
for (const entry of rootEntries) if (entry.isSymbolicLink()) fail(`agent root contains symlink: ${entry.name}`)
const rootFiles = rootEntries.filter((entry) => entry.isFile()).map((entry) => entry.name).sort()
const generatedBatchPresent = rootFiles.includes('generation-batch.json')
const expectedRootFiles = generatedBatchPresent ? ['generation-batch.json', 'sprite.json'] : ['sprite.json']
if (JSON.stringify(rootFiles) !== JSON.stringify(expectedRootFiles)) fail('agent root contains unexpected files')

// Shared Phaser atlas metadata is a strict 4-direction x (3 walk + alias + idle) contract.
const atlas = parseJson(join(AGENT_DIR, 'sprite.json'), 'shared agent sprite atlas')
if (atlas) {
  const frames = new Map((atlas.frames || []).map((entry) => [entry.filename, entry.frame]))
  const expectedNames = []
  for (const direction of ['down', 'left', 'right', 'up']) expectedNames.push(
    `${direction}-walk.000`, `${direction}-walk.001`, `${direction}-walk.002`, `${direction}-walk.003`, direction,
  )
  if (frames.size !== 20 || expectedNames.some((name) => !frames.has(name))) fail('shared sprite atlas must expose exactly 20 canonical frames')
  const rowsByDirection = { down: 0, left: 32, right: 64, up: 96 }
  for (const direction of Object.keys(rowsByDirection)) {
    const y = rowsByDirection[direction]
    const coordinates = [[0, y], [32, y], [64, y], [32, y], [32, y]]
    const names = [`${direction}-walk.000`, `${direction}-walk.001`, `${direction}-walk.002`, `${direction}-walk.003`, direction]
    names.forEach((name, index) => {
      const frame = frames.get(name)
      if (!frame || frame.x !== coordinates[index][0] || frame.y !== coordinates[index][1] || frame.w !== 32 || frame.h !== 32) {
        fail(`shared sprite atlas frame is invalid: ${name}`)
      }
    })
  }
}

const batchRecord = generatedBatchPresent ? parseJson(join(AGENT_DIR, 'generation-batch.json'), 'generated sprite batch receipt') : null
if (batchRecord && (
  batchRecord.schema_version !== 1 || batchRecord.catalog_id !== catalog.catalog_id || batchRecord.catalog_sha256 !== catalogSha ||
  batchRecord.source_policy !== catalog.source_policy || batchRecord.items?.length !== 25 || batchRecord.max_requests_total !== 275 ||
  batchRecord.price_snapshot?.currency !== 'USD' || !batchRecord.price_snapshot?.price_per_request_usd ||
  !batchRecord.price_snapshot?.max_cost_usd || !batchRecord.price_snapshot?.cost_source
)) fail('generated sprite batch receipt does not match the canonical catalog')

const newTextureHashes = new Set(); const newPortraitHashes = new Set(); const seenFiles = new Set()
let clearedGeneratedFiles = 0
for (const slot of catalog.slots) {
  const directoryPath = join(AGENT_DIR, slot.sprite_key)
  if (!directory(directoryPath)) { block(`missing canonical slot ${slot.sprite_key}`, 2); continue }
  const entries = readdirSync(directoryPath, { withFileTypes: true })
  if (entries.some((entry) => entry.isSymbolicLink() || !entry.isFile())) fail(`slot ${slot.sprite_key} contains unsafe entries`)
  const names = entries.map((entry) => entry.name).sort()
  const receiptPresent = names.includes('generation-provenance.json')
  const expected = receiptPresent
    ? ['agent.json', 'generation-provenance.json', 'portrait.png', 'texture.png']
    : ['agent.json', 'portrait.png', 'texture.png']
  if (JSON.stringify(names) !== JSON.stringify(expected)) fail(`slot ${slot.sprite_key} contains missing or unexpected files`)
  const agent = parseJson(join(directoryPath, 'agent.json'), `${slot.sprite_key} agent.json`)
  const expectedPortrait = `assets/village/agents/${slot.sprite_key}/portrait.png`
  if (!agent || agent.name !== slot.sprite_key || agent.portrait !== expectedPortrait || sha256(join(directoryPath, 'agent.json')) !== slot.agent_json_sha256) {
    fail(`slot ${slot.sprite_key} metadata identity, portrait path, or catalog hash differs`)
  }
  const texturePath = join(directoryPath, 'texture.png'); const portraitPath = join(directoryPath, 'portrait.png')
  if (!regular(texturePath) || !regular(portraitPath)) { block(`${slot.sprite_key} is missing texture or portrait`, 2); continue }
  const textureHash = sha256(texturePath); const portraitHash = sha256(portraitPath)
  if (!receiptPresent) {
    if (!denied.has(textureHash) || !denied.has(portraitHash)) fail(`unreceipted slot ${slot.sprite_key} is not the recorded blocked legacy baseline`)
    block(`${slot.sprite_key} texture and portrait have no generated provenance receipt`, 2)
    rows.push([`agents/${slot.sprite_key}/{texture,portrait}.png`, 'pending', 'blocked', 'legacy baseline'])
    continue
  }
  const receipt = parseJson(join(directoryPath, 'generation-provenance.json'), `${slot.sprite_key} generation receipt`)
  let valid = Boolean(receipt)
  const item = batchRecord?.items?.find((candidate) => candidate.asset_key === slot.asset_key && candidate.sprite_key === slot.sprite_key)
  if (!receipt || receipt.schema_version !== 1 || receipt.batch_id !== batchRecord?.batch_id || receipt.catalog_id !== catalog.catalog_id ||
      receipt.catalog_sha256 !== catalogSha || receipt.source_policy !== catalog.source_policy || receipt.asset_key !== slot.asset_key ||
      receipt.sprite_key !== slot.sprite_key || !item || item.run_id !== receipt.generation?.run_id || receipt.files?.length !== 2) valid = false
  const records = new Map((receipt?.files || []).map((record) => [record.file, record]))
  if (records.size !== 2) valid = false
  for (const [kind, path, hash, expectedWidth, expectedHeight] of [
    ['resident_texture', texturePath, textureHash, 96, 128],
    ['resident_portrait', portraitPath, portraitHash, 256, 256],
  ]) {
    const rel = `agents/${slot.sprite_key}/${kind === 'resident_texture' ? 'texture.png' : 'portrait.png'}`
    const record = records.get(rel)
    if (!record || seenFiles.has(rel) || record.asset_kind !== kind || record.sha256 !== hash || !HEX64.test(record.sha256 || '') ||
        record.bytes !== readFileSync(path).length || record.mime_type !== 'image/png' || record.width !== expectedWidth || record.height !== expectedHeight ||
        record.color_mode !== 'RGBA' || record.audit_status !== 'cleared' || record.distribution_status !== 'allowed' ||
        record.rights_basis !== 'first_party_generated') valid = false
    seenFiles.add(rel)
  }
  const generation = receipt?.generation
  if (!generation || generation.request_sha256 !== canonicalSha(generation.request) || generation.request?.asset_key !== slot.asset_key ||
      generation.request?.model !== batchRecord?.model || generation.provider_request_ids?.length < 4 || generation.submitted_request_count < 4 ||
      !HEX64.test(generation.capability_receipt_id || '') || !HEX64.test(generation.phaser_evidence_sha256 || '') ||
      !HEX64.test(generation.approval_evidence_sha256 || '') || !HEX64.test(generation.capability_evidence_sha256 || '') ||
      !generation.phaser_reviewer || !generation.approved_by || !generation.normalized_origin || !generation.model_alias ||
      generation.review_surface !== 'phaser-canvas-v1' || generation.phaser_version !== '3.90.0' ||
      generation.phaser_frames?.length !== 12 || !HEX64.test(generation.phaser_screenshot_sha256 || '') ||
      !generation.estimated_cost_upper_bound_usd || generation.cost_source !== batchRecord?.price_snapshot?.cost_source) valid = false
  const portraitRecord = records.get(`agents/${slot.sprite_key}/portrait.png`)
  if (portraitRecord?.derivation?.source_sha256 !== textureHash || portraitRecord?.derivation?.frame !== 'down-walk.001' ||
      portraitRecord?.derivation?.resize !== 'nearest-neighbor-256x256') valid = false
  try {
    if (!portraitMatches(decodeRgbaPng(texturePath), decodeRgbaPng(portraitPath))) valid = false
  } catch { valid = false }
  if (denied.has(textureHash) || denied.has(portraitHash)) valid = false
  if (newTextureHashes.has(textureHash) || newPortraitHashes.has(portraitHash)) valid = false
  newTextureHashes.add(textureHash); newPortraitHashes.add(portraitHash)
  if (!valid) { fail(`generated provenance or image derivation is invalid for slot ${slot.sprite_key}`); block(`${slot.sprite_key} generated receipt is invalid`, 2) }
  else { clearedGeneratedFiles += 2; rows.push([`agents/${slot.sprite_key}/{texture,portrait}.png`, 'cleared', 'allowed', 'receipt ok']) }
}
if (generatedBatchPresent && clearedGeneratedFiles !== 50) block(`generated batch clears ${clearedGeneratedFiles}/50 resident files`, 50 - clearedGeneratedFiles)
if (!generatedBatchPresent) rows.push([RECEIPT_POLICY_FILE, 'pending', 'blocked', '0/50 receipts'])

// The caravan is a first-party generated runtime entity rather than a resident
// slot. Its receipts, PNG constraints, and coupled Phaser atlases are still
// release-gated so a stale JSON/new texture cache pair cannot ship unnoticed.
// The one exception is the explicit agents-only installer fixture identified
// above; it has no caravan tree by design and cannot be mistaken for a full
// release tree.
const caravanContracts = [
  {
    part: 'merchant',
    files: [['texture.png', 96, 128], ['portrait.png', 256, 256]],
    atlas: ['walk', 32, 0.5],
  },
  {
    part: 'convoy',
    files: [['texture.png', 192, 256]],
    atlas: ['roll', 64, 0.75],
  },
  {
    part: 'stall',
    files: [['texture.png', 64, 64]],
    atlas: null,
  },
]
if (!agentsOnlyOverride && !directory(CARAVAN_DIR)) fail('caravan asset directory is missing or unsafe')
else if (!agentsOnlyOverride) {
  const caravanDirs = readdirSync(CARAVAN_DIR, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
    .map((entry) => entry.name).sort()
  if (JSON.stringify(caravanDirs) !== JSON.stringify(['convoy', 'merchant', 'stall'])) {
    fail('caravan directory must contain exactly convoy, merchant, and stall assets')
  }
}
for (const contract of agentsOnlyOverride ? [] : caravanContracts) {
  const partDir = join(CARAVAN_DIR, contract.part)
  let valid = directory(partDir)
  const expectedNames = [
    ...contract.files.map(([file]) => file),
    'generation-provenance.json',
    ...(contract.atlas ? ['atlas.json'] : []),
  ].sort()
  if (valid) {
    const names = readdirSync(partDir, { withFileTypes: true })
    if (names.some((entry) => entry.isSymbolicLink() || !entry.isFile()) ||
        JSON.stringify(names.map((entry) => entry.name).sort()) !== JSON.stringify(expectedNames)) valid = false
  }
  const receipt = parseJson(join(partDir, 'generation-provenance.json'), `${contract.part} caravan generation receipt`)
  if (!receipt || receipt.schema_version !== 1 || receipt.generator !== 'Codex built-in ImageGen' ||
      receipt.rights_basis !== 'first_party_generated' || typeof receipt.prompt_contract !== 'string' ||
      receipt.prompt_contract.length < 80 || !Array.isArray(receipt.generated_sources) ||
      receipt.generated_sources.length === 0 || !String(receipt.postprocess?.chroma_key || '').includes('remove_chroma_key.py') ||
      receipt.files?.length !== contract.files.length) valid = false
  const receiptFiles = new Map((receipt?.files || []).map((record) => [record.file, record]))
  for (const [file, width, height] of contract.files) {
    const path = join(partDir, file)
    if (!regular(path) || !validateCaravanPng(path, receiptFiles.get(file), width, height)) valid = false
  }
  if (contract.part === 'merchant' && valid) {
    try {
      if (!portraitMatches(
        decodeRgbaPng(join(partDir, 'texture.png')),
        decodeRgbaPng(join(partDir, 'portrait.png')),
      )) valid = false
    } catch { valid = false }
  }
  if (contract.atlas) {
    const atlas = parseJson(join(partDir, 'atlas.json'), `${contract.part} caravan atlas`)
    if (!validateGridAtlas(atlas, ...contract.atlas)) valid = false
  }
  if (!valid) fail(`generated provenance, image, or atlas contract is invalid for caravan/${contract.part}`)
  rows.push([
    `caravan/${contract.part}/{${expectedNames.join(',')}}`,
    valid ? 'cleared' : 'invalid',
    valid ? 'allowed' : 'blocked',
    valid ? 'receipt ok' : 'INVALID',
  ])
}

const fileWidth = Math.max(37, ...rows.map(([file]) => file.length + 1))
console.log(`\nAsset provenance (${useDist ? 'dist' : 'source'} tree; ${manifest.task})`)
console.log(`${'file'.padEnd(fileWidth)} audit      distribution  integrity`)
for (const [file, audit, distribution, integrity] of rows) console.log(
  `${file.padEnd(fileWidth)} ${String(audit).padEnd(10)} ${String(distribution).padEnd(13)} ${integrity}`,
)
console.log('')
if (errors > 0) {
  console.error(`FAILED integrity check: ${errors} problem(s).`)
  process.exit(1)
}
if (release && releaseBlocks > 0) {
  console.error(`FAILED release gate: ${releaseBlocks} generated/third-party file condition(s) are not cleared.`)
  process.exit(1)
}
if (release) console.log(agentsOnlyOverride
  ? 'ok release gate passed: all 50 generated resident files are cleared (agents-only override).'
  : 'ok release gate passed: tilemaps and all 50 generated resident files are cleared; caravan atlases are cleared.')
else {
  console.log(agentsOnlyOverride
    ? 'ok integrity check passed: the canonical 25-slot baseline is byte-accounted (agents-only override).'
    : 'ok integrity check passed: tilemaps, the canonical 25-slot baseline, and caravan atlases are byte-accounted.')
  if (releaseBlocks > 0) console.log(`  note: ${releaseBlocks} generated-file condition(s) remain blocked; run --release for the packaging gate.`)
}
