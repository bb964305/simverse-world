import { useLocale } from '../services/locale'

export function LanguageToggle({ className = '' }: { className?: string }) {
  const locale = useLocale((state) => state.locale)
  const setLocale = useLocale((state) => state.setLocale)

  return (
    <div className={`language-toggle ${className}`.trim()} role="group" aria-label="Language / 语言" data-locale={locale}>
      <span aria-hidden="true">◎</span>
      <button type="button" title="Switch to English" aria-pressed={locale === 'en'} onClick={() => setLocale('en')}>EN</button>
      <span aria-hidden="true">/</span>
      <button type="button" title="切换到中文" aria-pressed={locale === 'zh-CN'} onClick={() => setLocale('zh-CN')}>中文</button>
    </div>
  )
}
