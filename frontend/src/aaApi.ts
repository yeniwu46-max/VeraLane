export type AaContact = { id: string; name: string; phone_masked: string }
export type AaSource = { id: string; posted_on: string; counterparty: string; amount_yuan: string }
export type AaDraft = {
  total_yuan: string | null
  note: string
  include_self: boolean | null
  payer_is_self: boolean | null
  requires_custom_shares: boolean
  participant_count: number | null
  source_transaction: AaSource | null
  participants: { name: string; contact_id: string | null; phone_masked: string | null; choices: AaContact[] }[]
  needs_review: string[]
}
export type AaPreview = {
  total_yuan: string
  self_yuan: string
  receivable_yuan: string
  note: string
  source_transaction: AaSource | null
  source_type: 'ledger' | 'user'
  reminder_on: string
  allocation_method: 'equal' | 'proportional' | 'manual'
  share_ratios: Record<string, number> | null
  participants: { id: string; name: string; phone_masked: string; amount_yuan: string; rounding_extra: boolean; share_ratio: number | null }[]
}
export type AaAction = { id: string; type: 'aa_collection'; tier: 'yellow' | 'red'; status: 'pending'; expires_at: string; details: AaPreview }
export type AaReply = { session_id: string; message: string; mode: 'offline' | 'deepseek'; aa_draft?: AaDraft; pending_action?: AaAction }
export type AaCollection = {
  id: string
  status: 'pending' | 'partial' | 'completed' | 'closed'
  allocation_method: 'equal' | 'proportional' | 'manual' | null
  share_ratios: Record<string, number> | null
  note: string
  total_yuan: string
  self_yuan: string
  receivable_yuan: string
  received_yuan: string
  outstanding_yuan: string
  net_advance_yuan: string
  source_transaction: AaSource | null
  source_type: 'ledger' | 'user'
  created_at: string
  closed_at: string | null
  participants: {
    id: string; request_id: string | null; name: string; phone_masked: string; amount_yuan: string; share_ratio?: number | null
    status: 'self' | 'not_required' | 'pending' | 'partial' | 'paid' | 'closed'
    received_yuan: string; outstanding_yuan: string
    payments: { amount_yuan: string; transaction_id: string; paid_at: string }[]
    paid_at: string | null; transaction_id: string | null
  }[]
}
export type AaCollections = { collections: AaCollection[]; demo_controls_enabled: boolean }
export type AaFormPayload = {
  session_id: string; total_yuan: string; contact_ids: string[]; include_self: boolean; note: string
  source_transaction_id: string | null; shares_yuan?: Record<string, string>; shares_ratio?: Record<string, number>
}
export type AaSeed = { id: string; draft?: AaDraft; source?: AaSource; message?: string; mode?: 'offline' | 'deepseek' }

export async function aaRequest<T>(url: string, body?: object, signal?: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    signal,
    ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  })
  const data = await response.json()
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : '请求未完成，请核对输入后重试。'
    throw new Error(detail)
  }
  return data as T
}

export function aaMoney(value: string) {
  return `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

export function aaCents(value: string): number | null {
  if (!/^\d+(\.\d{1,2})?$/.test(value.trim())) return null
  const [whole, decimal = ''] = value.trim().split('.')
  const cents = Number(whole) * 100 + Number(decimal.padEnd(2, '0'))
  return Number.isSafeInteger(cents) ? cents : null
}

export const aaStatusLabels: Record<AaCollection['status'], string> = {
  pending: '待收款', partial: '部分收齐', completed: '已收齐', closed: '已关闭',
}
