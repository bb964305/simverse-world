import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SearchDropdown } from './SearchDropdown'

afterEach(cleanup)

describe('SearchDropdown accessibility ownership', () => {
  it('uses a unique listbox id for each mounted search', () => {
    render(<><SearchDropdown /><SearchDropdown /></>)

    const controls = screen.getAllByRole('combobox').map((input) => input.getAttribute('aria-controls'))

    expect(controls.every(Boolean)).toBe(true)
    expect(new Set(controls).size).toBe(2)
  })
})
