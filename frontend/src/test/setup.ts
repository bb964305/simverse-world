// Node 25 exposes an incomplete experimental Web Storage global unless a
// storage file is configured. Install a deterministic Storage implementation
// before application modules read localStorage at import time.
class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>()

  get length() { return this.values.size }
  clear() { this.values.clear() }
  getItem(key: string) { return this.values.get(key) ?? null }
  key(index: number) { return [...this.values.keys()][index] ?? null }
  removeItem(key: string) { this.values.delete(key) }
  setItem(key: string, value: string) { this.values.set(key, String(value)) }
}

Object.defineProperty(globalThis, 'localStorage', {
  configurable: true,
  value: new MemoryStorage(),
})
Object.defineProperty(globalThis, 'sessionStorage', {
  configurable: true,
  value: new MemoryStorage(),
})

// Most legacy component assertions exercise the Chinese branch explicitly;
// individual i18n/default-English tests override the Zustand locale. Keeping
// this in the test harness avoids making production default-language behavior
// depend on the order in which test files happen to import the locale store.
localStorage.setItem('simverse-locale-v2', 'zh-CN')

// jsdom does not implement matchMedia. Install a benign default (matches:false,
// no-op listeners) so components/hooks that probe media queries degrade to the
// "no preference / desktop" branch. Individual tests override via vi.stubGlobal.
if (typeof globalThis.matchMedia === 'undefined') {
  Object.defineProperty(globalThis, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}
