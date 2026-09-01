import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { onboardingPath } from '../services/authReturnTo'
import { useLocale } from '../services/locale'
import { signInWithWallet } from '../services/web3/wallet'
import { web3ErrorMessage } from '../services/web3/errors'
import { useGameStore } from '../stores/gameStore'

export function WalletLoginButton({ next }: { next: string }) {
  const locale = useLocale((state) => state.locale)
  const setAuth = useGameStore((state) => state.setAuth)
  const navigate = useNavigate()
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  const connect = async () => {
    if (pending) return
    setPending(true)
    setError('')
    try {
      const result = await signInWithWallet(locale)
      setAuth(result.user, result.access_token)
      navigate(onboardingPath(next), { replace: true })
    } catch (reason) {
      setError(web3ErrorMessage(reason, locale, locale === 'en' ? 'Wallet sign-in failed.' : '钱包登录失败。'))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="wallet-login">
      <button className="auth-submit wallet-login__button" type="button" disabled={pending} onClick={() => void connect()}>
        <span aria-hidden="true">◇</span>
        {pending
          ? (locale === 'en' ? 'Waiting for wallet…' : '等待钱包确认…')
          : (locale === 'en' ? 'Connect wallet & enter' : '连接钱包并进入')}
      </button>
      {error && <div className="auth-error" role="alert">{error}</div>}
    </div>
  )
}
