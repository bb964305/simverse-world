import { describe, expect, it } from 'vitest'
import { trainingMerkleRoot } from './trainingMerkle'

describe('trainingMerkleRoot', () => {
  it('is deterministic and changes independently with bytes or provenance', async () => {
    const base = await trainingMerkleRoot(new Blob(['training-a']), { resident: 'r1' })
    expect(await trainingMerkleRoot(new Blob(['training-a']), { resident: 'r1' })).toBe(base)
    expect(await trainingMerkleRoot(new Blob(['training-b']), { resident: 'r1' })).not.toBe(base)
    expect(await trainingMerkleRoot(new Blob(['training-a']), { resident: 'r2' })).not.toBe(base)
  })

  it('covers files larger than one chunk', async () => {
    const bytes = new Uint8Array(1024 * 1024 + 1)
    bytes[bytes.length - 1] = 1
    const root = await trainingMerkleRoot(new Blob([bytes]), { schema: 'test' })
    bytes[bytes.length - 1] = 2
    expect(await trainingMerkleRoot(new Blob([bytes]), { schema: 'test' })).not.toBe(root)
  })
})
