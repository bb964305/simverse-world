import type { Config } from '@wagmi/core'
import type { Chain } from 'viem'
import type { Locale } from '../locale'

const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'
const CHAIN_ID = Number(import.meta.env.VITE_WEB3_CHAIN_ID || 84532)
const CHAIN_NAME = import.meta.env.VITE_WEB3_CHAIN_NAME || (CHAIN_ID === 84532 ? 'Base Sepolia' : 'Simverse Local')
const RPC_URL = import.meta.env.VITE_WEB3_RPC_URL || (CHAIN_ID === 84532 ? 'https://sepolia.base.org' : 'http://127.0.0.1:8545')
const WALLETCONNECT_PROJECT_ID = import.meta.env.VITE_WALLETCONNECT_PROJECT_ID || ''

export interface WalletUser {
  id: string
  name: string
  email: string
  avatar: string | null
  soul_coin_balance: number
  is_admin?: boolean
  lab_enabled?: boolean
  wallet_address: string
}

export interface WalletAuthResult {
  access_token: string
  user: WalletUser
}

export interface Web3Runtime {
  config: Config
  chain: Chain
  core: typeof import('@wagmi/core')
  modal: {
    open: (params?: {
      forceOpen?: boolean
      onConnect?: () => void
      onConnectError?: (error: Error) => void
      onModalClosedByManualOperation?: () => void
    }) => void
    setLanguage: (locale: Locale) => void
  }
}

let runtimePromise: Promise<Web3Runtime> | undefined

export function configuredChainId(): number {
  return CHAIN_ID
}

export function configuredChainName(): string {
  return CHAIN_NAME
}

export function getWeb3Runtime(): Promise<Web3Runtime> {
  runtimePromise ??= createRuntime()
  return runtimePromise
}

export async function disconnectWallet(): Promise<void> {
  if (!runtimePromise) return
  const runtime = await runtimePromise
  await runtime.core.disconnect(runtime.config)
}

async function createRuntime(): Promise<Web3Runtime> {
  const [rabbykit, core, viem, chains] = await Promise.all([
    import('@rabby-wallet/rabbykit'),
    import('@wagmi/core'),
    import('viem'),
    import('@wagmi/core/chains'),
  ])
  const chain = CHAIN_ID === chains.baseSepolia.id
    ? {
        ...chains.baseSepolia,
        rpcUrls: { default: { http: [RPC_URL] } },
      }
    : viem.defineChain({
        id: CHAIN_ID,
        name: CHAIN_NAME,
        nativeCurrency: { name: 'Ether', symbol: 'ETH', decimals: 18 },
        rpcUrls: { default: { http: [RPC_URL] } },
        testnet: true,
      })

  const config = core.createConfig(rabbykit.getDefaultConfig({
    appName: 'Simverse World',
    appDesc: 'A persistent AI world with wallet-owned Agents and verifiable memories.',
    appUrl: window.location.origin,
    projectId: WALLETCONNECT_PROJECT_ID || 'simverse-injected-wallets-only',
    chains: [chain],
    transports: { [chain.id]: core.http(RPC_URL) },
  }))
  const modal = rabbykit.createModal({
    wagmi: config,
    language: 'zh-CN',
    theme: 'dark',
    showWalletConnect: Boolean(WALLETCONNECT_PROJECT_ID),
    themeVariables: {
      '--rk-font-family': 'Inter, system-ui, sans-serif',
      '--rk-primary-button-bg': '#d9ff3f',
      '--rk-primary-button-color': '#080c0f',
      '--rk-border-radius': '6px',
    },
  })
  return { config, chain, core, modal }
}

export async function requireWalletAccount(locale: Locale): Promise<{
  address: `0x${string}`
  runtime: Web3Runtime
}> {
  const runtime = await getWeb3Runtime()
  runtime.modal.setLanguage(locale)
  const current = runtime.core.getAccount(runtime.config)
  const address = current.address && current.isConnected
    ? current.address
    : await new Promise<`0x${string}`>((resolve, reject) => {
        let settled = false
        const finish = (error?: Error) => {
          if (settled) return
          const account = runtime.core.getAccount(runtime.config)
          if (!error && account.address) {
            settled = true
            resolve(account.address)
          } else if (error) {
            settled = true
            reject(error)
          }
        }
        runtime.modal.open({
          forceOpen: true,
          onConnect: () => finish(),
          onConnectError: (error) => finish(error),
          onModalClosedByManualOperation: () => finish(new Error(locale === 'en' ? 'Wallet connection cancelled.' : '已取消钱包连接')),
        })
      })

  const account = runtime.core.getAccount(runtime.config)
  if (account.chainId !== runtime.chain.id) {
    await runtime.core.switchChain(runtime.config, { chainId: runtime.chain.id })
  }
  return { address, runtime }
}

async function responseJson<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({}))
  if (response.ok) return body as T
  const detail = body?.detail
  const message = typeof detail === 'string'
    ? detail
    : Array.isArray(detail)
      ? detail.map((item) => item?.msg).filter(Boolean).join('；')
      : ''
  throw new Error(message || `Wallet API ${response.status}`)
}

export async function signInWithWallet(locale: Locale): Promise<WalletAuthResult> {
  const { address, runtime } = await requireWalletAccount(locale)

  const challengeResponse = await fetch(`${API}/auth/wallet/challenge`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ address, chain_id: runtime.chain.id }),
  })
  const challenge = await responseJson<{
    message: string
    nonce: string
    chain_id: number
  }>(challengeResponse)
  const signature = await runtime.core.signMessage(runtime.config, {
    account: address,
    message: challenge.message,
  })
  const verifyResponse = await fetch(`${API}/auth/wallet/verify`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      address,
      message: challenge.message,
      signature,
      nonce: challenge.nonce,
      chain_id: challenge.chain_id,
    }),
  })
  return responseJson<WalletAuthResult>(verifyResponse)
}
