import type { Locale } from '../locale'

type ErrorRecord = Record<string, unknown>

function asRecord(value: unknown): ErrorRecord | null {
  return typeof value === 'object' && value !== null ? value as ErrorRecord : null
}

function collectErrorDetails(reason: unknown): string[] {
  const details: string[] = []
  let current: unknown = reason
  const seen = new Set<unknown>()
  for (let depth = 0; depth < 8 && current && !seen.has(current); depth += 1) {
    seen.add(current)
    if (typeof current === 'string') {
      details.push(current)
      break
    }
    const record = asRecord(current)
    if (!record) break
    for (const key of ['name', 'message', 'shortMessage', 'details']) {
      if (typeof record[key] === 'string') details.push(record[key])
    }
    if (typeof record.code === 'number' || typeof record.code === 'string') details.push(String(record.code))
    current = record.cause
  }
  return details
}

function isSafeApplicationMessage(value: string): boolean {
  const message = value.trim()
  if (!message || message.length > 220) return false
  if (/^(?:error|typeerror|domexception|transactionexecutionerror|contractfunctionexecutionerror)$/i.test(message)) return false
  return !(
    /request arguments|contract call|request signature|docs:|version:|viem@/i.test(message)
    || /\b(data|args|sender|address|function):\s*(?:0x|\[|\()/i.test(message)
    || /0x[0-9a-f]{96,}/i.test(message)
    || message.includes('\n')
  )
}

/**
 * Convert wallet/RPC/contract errors into short, actionable UI copy. This
 * deliberately strips calldata, signatures, account addresses, library docs,
 * and nested viem diagnostics from anything rendered to a player.
 */
export function web3ErrorMessage(
  reason: unknown,
  locale: Locale,
  fallback?: string,
): string {
  const english = locale === 'en'
  const details = collectErrorDetails(reason)
  const combined = details.join('\n').toLowerCase()

  if (
    combined.includes('userrejectedrequesterror')
    || combined.includes('user rejected')
    || combined.includes('user denied')
    || combined.includes('denied request signature')
    || combined.includes('request rejected')
    || details.includes('4001')
  ) {
    return english
      ? 'Request cancelled in your wallet. Nothing was submitted and no gas was spent.'
      : '你已在钱包中取消本次请求。没有交易提交，也不会扣除 Gas。'
  }
  if (combined.includes('resource unavailable') || combined.includes('already pending') || details.includes('-32002')) {
    return english
      ? 'A wallet request is already open. Complete or cancel it in your wallet, then try again.'
      : '钱包中已有待处理请求。请先确认或取消该请求，然后重试。'
  }
  if (combined.includes('insufficient funds') || combined.includes('exceeds the balance')) {
    return english
      ? 'Not enough ETH on Robinhood Chain to pay network gas.'
      : 'Robinhood Chain 上的 ETH 不足，无法支付网络 Gas。'
  }
  if (combined.includes('chain mismatch') || combined.includes('wrong network') || combined.includes('unsupported chain')) {
    return english
      ? 'Switch your wallet to Robinhood Chain and try again.'
      : '请将钱包切换到 Robinhood Chain 后重试。'
  }
  if (combined.includes('walletalreadyhasagent') || combined.includes('wallet already has')) {
    return english
      ? 'This wallet already owns an Agent Passport. Refresh to restore the existing identity.'
      : '这个钱包已经拥有 Agent Passport，请刷新页面恢复现有身份。'
  }
  if (combined.includes('residentalreadyregistered') || combined.includes('resident already')) {
    return english
      ? 'This resident is already registered onchain. Refresh to restore its Passport.'
      : '这位居民已经完成链上登记，请刷新页面恢复其 Passport。'
  }
  if (
    combined.includes('failed to fetch')
    || combined.includes('http request failed')
    || combined.includes('network error')
    || combined.includes('timeout')
    || combined.includes('rpc')
  ) {
    return english
      ? 'Robinhood Chain is temporarily unreachable. No new transaction was submitted; try again shortly.'
      : '暂时无法连接 Robinhood Chain。没有提交新交易，请稍后重试。'
  }
  if (combined.includes('execution reverted') || combined.includes('contractfunctionexecutionerror')) {
    return english
      ? 'The contract rejected this request. Refresh your identity status before trying again.'
      : '合约拒绝了本次请求。请刷新身份状态后再试。'
  }
  if (combined.includes('connector not found') || combined.includes('provider not found') || combined.includes('no provider')) {
    return english
      ? 'No browser wallet was detected. Install a wallet extension or open this site inside your wallet browser.'
      : '未检测到浏览器钱包。请安装钱包扩展，或在钱包内置浏览器中打开本站。'
  }

  const safe = details.find(isSafeApplicationMessage)
  if (safe) return safe.trim()
  return fallback || (english ? 'The wallet request could not be completed. Please try again.' : '钱包请求未能完成，请重试。')
}

export function friendlyWeb3Error(reason: unknown, locale: Locale, fallback?: string): Error {
  const message = web3ErrorMessage(reason, locale, fallback)
  if (reason instanceof Error && reason.message === message) return reason
  return new Error(message, { cause: reason })
}
