import { useLocale } from '../services/locale'

export function LanguageToggle({ className = '' }: { className?: string }) {
  const locale = useLocale((state) => state.locale)
  const setLocale = useLocale((state) => state.setLocale)

  return (
    <div className={className} role="group" aria-label="Language / 语言">
      <button type="button" aria-pressed={locale === 'zh-CN'} onClick={() => setLocale('zh-CN')}>中</button>
      <span aria-hidden="true">/</span>
      <button type="button" aria-pressed={locale === 'en'} onClick={() => setLocale('en')}>EN</button>
    </div>
  )
}
