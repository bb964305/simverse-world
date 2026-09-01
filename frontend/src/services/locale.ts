import { create } from 'zustand'

export type Locale = 'zh-CN' | 'en'

interface LocaleState {
  locale: Locale
  setLocale: (locale: Locale) => void
}

// v2 deliberately starts existing installs in English once. The previous
// release defaulted to Chinese and left that choice in storage even after the
// public site's default changed.
const STORAGE_KEY = 'simverse-locale-v2'

function initialLocale(): Locale {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'zh-CN' || stored === 'en') return stored
  return 'en'
}

const defaultLocale = initialLocale()
document.documentElement.lang = defaultLocale

export const useLocale = create<LocaleState>((set) => ({
  locale: defaultLocale,
  setLocale: (locale) => {
    localStorage.setItem(STORAGE_KEY, locale)
    document.documentElement.lang = locale
    set({ locale })
  },
}))
