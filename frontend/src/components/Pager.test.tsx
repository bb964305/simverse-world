import '@testing-library/jest-dom/vitest'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { useState } from 'react'
import { Pager } from './Pager'

afterEach(cleanup)

function Harness({ total, initial = 1 }: { total: number; initial?: number }) {
  const [page, setPage] = useState(initial)
  return (
    <div>
      <span data-testid="page">{page}</span>
      <Pager page={page} totalPages={total} setPage={setPage} />
    </div>
  )
}

describe('Pager', () => {
  it('shows current / total page text', () => {
    render(<Pager page={2} totalPages={5} setPage={vi.fn()} />)
    expect(screen.getByText(/第 2 \/ 5 页/)).toBeInTheDocument()
  })

  it('advances the page when 下一页 is clicked', () => {
    render(<Harness total={3} />)
    fireEvent.click(screen.getByRole('button', { name: /下一页/ }))
    expect(screen.getByTestId('page').textContent).toBe('2')
  })

  it('disables 上一页 on the first page', () => {
    render(<Pager page={1} totalPages={3} setPage={vi.fn()} />)
    expect(screen.getByRole('button', { name: /上一页/ })).toBeDisabled()
  })

  it('disables 下一页 on the last page', () => {
    render(<Pager page={3} totalPages={3} setPage={vi.fn()} />)
    expect(screen.getByRole('button', { name: /下一页/ })).toBeDisabled()
  })
})
