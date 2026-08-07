import { describe, expect, it } from 'vitest'
import { formatLabMemory, formatLabRunProfile } from './labModel'

describe('Lab model and resource display', () => {
  it('formats the resource snapshot stored on a run', () => {
    expect(formatLabRunProfile({
      model_name: 'deepseek-v4-pro', resource_cpu_cores: 4, resource_memory_mb: 4096,
    } as never)).toBe('deepseek-v4-pro · 4 核 · 4 GB')
  })

  it('does not invent a profile for legacy runs', () => {
    expect(formatLabRunProfile({} as never)).toBeNull()
    expect(formatLabMemory(2560)).toBe('2560 MB')
  })
})
