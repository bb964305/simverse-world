import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ForgePage } from './ForgePage'

// ForgeChat/QuickForge/DeepForge/ForgePreview each own significant local
// state (chat history, generation progress) and hit the API/WS layers —
// out of scope here. Stub them so the test only exercises ForgePage's own
// split/toggle layout, per the "mobile vs desktop structure" testing note.
vi.mock('../components/TopNav', () => ({ TopNav: () => <nav /> }))
vi.mock('../components/forge/ForgeChat', () => ({ ForgeChat: () => <div>forge-chat-stub</div> }))
vi.mock('../components/forge/QuickForge', () => ({ QuickForge: () => <div>quick-forge-stub</div> }))
vi.mock('../components/forge/DeepForge', () => ({ DeepForge: () => <div>deep-forge-stub</div> }))
vi.mock('../components/forge/ForgePreview', () => ({ ForgePreview: () => <div>forge-preview-stub</div> }))
vi.mock('../services/ws', () => ({ connectWS: vi.fn() }))

function stubMatchMedia(matches: boolean) {
  vi.stubGlobal('matchMedia', vi.fn().mockImplementation((query: string) => ({
    matches,
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  })))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

function renderPage() {
  return render(
    <MemoryRouter>
      <ForgePage />
    </MemoryRouter>,
  )
}

describe('ForgePage mobile layout', () => {
  it('splits left/right on desktop with both panes visible', () => {
    stubMatchMedia(false)
    renderPage()
    expect(screen.getByTestId('forge-split').style.flexDirection).toBe('row')
    expect(screen.getByTestId('forge-edit-pane').style.display).toBe('flex')
    expect(screen.getByTestId('forge-preview-pane').style.display).toBe('flex')
    // No edit/preview toggle on desktop.
    expect(screen.queryByRole('button', { name: /✏️ 编辑/ })).not.toBeInTheDocument()
  })

  it('stacks panes and shows only the edit pane by default on mobile', () => {
    stubMatchMedia(true)
    renderPage()
    expect(screen.getByTestId('forge-split').style.flexDirection).toBe('column')
    expect(screen.getByTestId('forge-edit-pane').style.display).toBe('flex')
    expect(screen.getByTestId('forge-preview-pane').style.display).toBe('none')
    // Both children stay mounted (state-preserving toggle), just hidden.
    expect(screen.getByText('forge-chat-stub')).toBeInTheDocument()
    expect(screen.getByText('forge-preview-stub')).toBeInTheDocument()
  })

  it('toggles to the preview pane without unmounting the edit pane', () => {
    stubMatchMedia(true)
    renderPage()
    fireEvent.click(screen.getByRole('button', { name: /👁️ 预览/ }))
    expect(screen.getByTestId('forge-preview-pane').style.display).toBe('flex')
    expect(screen.getByTestId('forge-edit-pane').style.display).toBe('none')
    // Still mounted, just hidden — switching back would not reset progress.
    expect(screen.getByText('forge-chat-stub')).toBeInTheDocument()
  })
})
