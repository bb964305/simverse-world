import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AdminSidebar } from './AdminSidebar'

afterEach(cleanup)

describe('AdminSidebar', () => {
  it('keeps analysis navigation primary and write controls collapsed', () => {
    render(
      <AdminSidebar
        activeTab="overview"
        controlOpen={false}
        onControlToggle={vi.fn()}
        onTabChange={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: /发展总览/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /居民与社会/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /经济运行/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /治理与事件/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /事件投放/ })).toBeNull()
  })

  it('reveals the centralized control center on demand', () => {
    const onControlToggle = vi.fn()
    const { rerender } = render(
      <AdminSidebar
        activeTab="overview"
        controlOpen={false}
        onControlToggle={onControlToggle}
        onTabChange={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /控制中心/ }))
    expect(onControlToggle).toHaveBeenCalledTimes(1)

    rerender(
      <AdminSidebar
        activeTab="events"
        controlOpen
        onControlToggle={onControlToggle}
        onTabChange={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: /用户权限/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /居民编辑/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Agent 托管/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /经济参数/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /事件投放/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /提案审批/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /实验楼控制/ })).toBeTruthy()
  })
})
