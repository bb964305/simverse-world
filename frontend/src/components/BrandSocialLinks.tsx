const SOCIALS = [
  {
    name: 'X',
    href: 'https://x.com/SIM_VERSESPACE',
    path: 'M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117Z',
  },
  {
    name: 'Telegram',
    href: 'https://t.me/SIM_VERSESPACE',
    path: 'M11.944 0A12 12 0 1 0 24 12 12.013 12.013 0 0 0 11.944 0Zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.064-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.258-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.015-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.324-.437.893-.663 3.498-1.524 5.831-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.477-1.635Z',
  },
] as const

export function BrandSocialLinks({ className = '' }: { className?: string }) {
  return (
    <div className={`brand-socials ${className}`.trim()} aria-label="Simverse social channels">
      {SOCIALS.map((social) => (
        <a key={social.name} href={social.href} target="_blank" rel="noreferrer" aria-label={`Simverse on ${social.name}`} title={social.name}>
          <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d={social.path} /></svg>
        </a>
      ))}
    </div>
  )
}
