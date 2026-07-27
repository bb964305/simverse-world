import type { LabRun } from './api'

export function formatLabMemory(memoryMb: number): string {
  return memoryMb % 1024 === 0 ? `${memoryMb / 1024} GB` : `${memoryMb} MB`
}

export function formatLabRunProfile(run: LabRun): string | null {
  if (!run.model_name || !run.resource_cpu_cores || !run.resource_memory_mb) return null
  return `${run.model_name} · ${run.resource_cpu_cores} 核 · ${formatLabMemory(run.resource_memory_mb)}`
}
