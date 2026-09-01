import { create } from 'zustand'

export type Locale = 'zh-CN' | 'en'

interface LocaleState {
  locale: Locale
  setLocale: (locale: Locale) => void
}

const STORAGE_KEY = 'simverse-locale'

function initialLocale(): Locale {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (stored === 'zh-CN' || stored === 'en') return stored
  return navigator.language.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en'
}

export const useLocale = create<LocaleState>((set) => ({
  locale: initialLocale(),
  setLocale: (locale) => {
    localStorage.setItem(STORAGE_KEY, locale)
    document.documentElement.lang = locale
    set({ locale })
  },
}))
