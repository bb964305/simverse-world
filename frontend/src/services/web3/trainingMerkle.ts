import { concatHex, keccak256, toHex, type Hex } from 'viem'

const CHUNK_BYTES = 1024 * 1024

function combineMerkleLevel(level: Hex[]): Hex[] {
  const next: Hex[] = []
  for (let index = 0; index < level.length; index += 2) {
    const left = level[index]
    const right = level[index + 1] ?? left
    next.push(keccak256(concatHex([left, right])))
  }
  return next
}

/**
 * Build an ordered Merkle root over every uploaded byte plus an explicit
 * provenance descriptor. This is independent from the server's whole-file
 * SHA-256 artifact hash and can later support chunk inclusion proofs.
 */
export async function trainingMerkleRoot(
  content: Blob,
  descriptor: Record<string, unknown>,
): Promise<Hex> {
  const bytes = new Uint8Array(await content.arrayBuffer())
  const leaves: Hex[] = []
  for (let offset = 0; offset < bytes.length; offset += CHUNK_BYTES) {
    leaves.push(keccak256(bytes.slice(offset, offset + CHUNK_BYTES)))
  }
  leaves.push(keccak256(toHex(JSON.stringify(descriptor))))

  let level = leaves
  while (level.length > 1) level = combineMerkleLevel(level)
  return level[0]
}
