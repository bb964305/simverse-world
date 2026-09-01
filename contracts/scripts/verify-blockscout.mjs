import { readFile, readdir } from 'node:fs/promises'
import { resolve } from 'node:path'

const deploymentPath = resolve('deployments/4663.json')
const deployment = JSON.parse(await readFile(deploymentPath, 'utf8'))
const address = deployment.implementation
if (!/^0x[0-9a-fA-F]{40}$/.test(address ?? '')) {
  throw new Error(`Invalid implementation address in ${deploymentPath}`)
}

const buildInfoDir = resolve('artifacts/build-info')
const inputFiles = (await readdir(buildInfoDir))
  .filter((name) => name.endsWith('.json') && !name.endsWith('.output.json'))
const candidates = []
for (const name of inputFiles) {
  const raw = await readFile(resolve(buildInfoDir, name), 'utf8')
  const build = JSON.parse(raw)
  const sourceName = Object.keys(build.input?.sources ?? {})
    .find((source) => source.endsWith('/SimverseAgentRegistryV3.sol'))
  if (sourceName) candidates.push({ name, build, sourceName })
}
if (!candidates.length) {
  throw new Error('No build-info containing SimverseAgentRegistryV3 was found; run npm run compile first')
}

const { build, sourceName } = candidates.at(-1)
const compilerVersion = `v${build.solcLongVersion}`
const baseUrl = process.env.BLOCKSCOUT_URL || 'https://robinhoodchain.blockscout.com'
const verifyUrl = `${baseUrl}/api/v2/smart-contracts/${address}/verification/via/standard-input`
const body = new FormData()
body.set('compiler_version', compilerVersion)
body.set('contract_name', `${sourceName}:SimverseAgentRegistryV3`)
body.set('autodetect_constructor_args', 'true')
body.set('license_type', 'mit')
body.set('files[0]', new Blob([JSON.stringify(build.input)], { type: 'application/json' }), 'standard-input.json')

const response = await fetch(verifyUrl, { method: 'POST', body })
const responseText = await response.text()
let verificationBackend = 'blockscout'
if (!response.ok) {
  // Some public Blockscout instances put write endpoints behind a Cloudflare
  // browser challenge. Sourcify is the explorer's upstream verification
  // source and supports Robinhood Chain directly, so use its authenticated-by-
  // bytecode API as the deterministic fallback instead of requiring UI clicks.
  const sourcifyResponse = await fetch(`https://sourcify.dev/server/v2/verify/4663/${address}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      stdJsonInput: build.input,
      compilerVersion: build.solcLongVersion,
      contractIdentifier: `${sourceName}:SimverseAgentRegistryV3`,
    }),
  })
  const sourcifyText = await sourcifyResponse.text()
  const alreadyVerified = sourcifyResponse.status === 409 && sourcifyText.includes('already_verified')
  if (!sourcifyResponse.ok && !alreadyVerified) {
    throw new Error(`Blockscout verification failed (${response.status}); Sourcify fallback failed (${sourcifyResponse.status}): ${sourcifyText.slice(0, 1000)}`)
  }
  verificationBackend = 'sourcify'
  const ticket = JSON.parse(sourcifyText)
  if (ticket.verificationId) {
    for (let attempt = 0; attempt < 40; attempt += 1) {
      await new Promise((resolveWait) => setTimeout(resolveWait, 3_000))
      const jobResponse = await fetch(`https://sourcify.dev/server/v2/verify/${ticket.verificationId}`)
      if (!jobResponse.ok) continue
      const job = await jobResponse.json()
      if (job.isJobCompleted && job.contract?.match) break
      if (job.isJobCompleted) throw new Error(`Sourcify verification did not match: ${JSON.stringify(job).slice(0, 2000)}`)
    }
  }
}

const statusUrl = `${baseUrl}/api/v2/smart-contracts/${address}`
for (let attempt = 0; attempt < (verificationBackend === 'blockscout' ? 20 : 1); attempt += 1) {
  await new Promise((resolveWait) => setTimeout(resolveWait, 3_000))
  const statusResponse = await fetch(statusUrl)
  if (!statusResponse.ok) continue
  const status = await statusResponse.json()
  if (status.is_verified || status.is_fully_verified || status.is_partially_verified) {
    console.log(JSON.stringify({
      address,
      contract: status.name,
      compiler: status.compiler_version,
      isVerified: Boolean(status.is_verified),
      isFullyVerified: Boolean(status.is_fully_verified),
      backend: verificationBackend,
      explorer: `${baseUrl}/address/${address}?tab=contract`,
    }, null, 2))
    process.exit(0)
  }
}

if (verificationBackend === 'sourcify') {
  const sourcifyStatus = await fetch(`https://sourcify.dev/server/v2/contract/4663/${address}`)
  if (sourcifyStatus.ok) {
    const status = await sourcifyStatus.json()
    console.log(JSON.stringify({
      address,
      contract: 'SimverseAgentRegistryV3',
      compiler: build.solcLongVersion,
      backend: verificationBackend,
      match: status.match,
      sourcify: `https://repo.sourcify.dev/4663/${address}`,
      explorer: `${baseUrl}/address/${address}?tab=contract`,
    }, null, 2))
    process.exit(0)
  }
}

throw new Error(`Verification was accepted but not confirmed after 60 seconds: ${statusUrl}`)
