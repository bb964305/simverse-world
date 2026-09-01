import { describe, expect, it } from 'vitest'
import { web3ErrorMessage } from './errors'

describe('web3ErrorMessage', () => {
  it('turns a verbose viem signature rejection into safe English copy', () => {
    const raw = new Error(`User rejected the request.
Request Arguments: from: 0x5E807ae9C82bA691Fca0CC1f56EB01eb58d6f04C to: 0x24f6f6bE48066cbE0B54d741cd4B52862Bb4b05c data: 0xa3dcc39c...
Contract Call: function: createAgentForResident(string metadataURI, bytes32 metadataHash, bytes32 residentKey)
Details: Request Signature: User denied request signature.
Version: viem@2.56.1`)
    const message = web3ErrorMessage(raw, 'en')
    expect(message).toBe('Request cancelled in your wallet. Nothing was submitted and no gas was spent.')
    expect(message).not.toMatch(/0x|viem|createAgentForResident/)
  })

  it('provides bilingual cancellation and gas guidance', () => {
    expect(web3ErrorMessage({ code: 4001, message: 'Rejected' }, 'zh-CN')).toContain('取消本次请求')
    expect(web3ErrorMessage(new Error('insufficient funds for gas * price + value'), 'en')).toContain('Not enough ETH')
  })

  it('does not render unsafe low-level diagnostics for unknown failures', () => {
    const message = web3ErrorMessage(new Error('Contract Call:\ndata: 0x1234\nDocs: https://viem.sh'), 'en', 'Registration failed.')
    expect(message).toBe('Registration failed.')
  })
})
