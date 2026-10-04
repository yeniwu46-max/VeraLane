import type { BillTransaction } from './BillVisuals'

export type InsightChange = {
  name: string
  current_yuan: string
  previous_yuan: string
  delta_yuan: string
  transaction_ids: string[]
}

export type InsightReport = {
  rule_version: string
  period: string
  start_date: string
  end_date: string
  filters: { category: string | null; merchant: string | null; min_amount_yuan: string | null; max_amount_yuan: string | null }
  total_yuan: string
  transaction_count: number
  transactions: BillTransaction[]
  coverage: { first_date: string | null; last_date: string | null; months_with_data: number; complete_history: boolean }
  comparison: {
    current_start: string
    current_end: string
    previous_start: string
    previous_end: string
    equal_days: number
    current_total_yuan: string
    previous_total_yuan: string
    delta_yuan: string
    delta_percent: string | null
    category_changes: InsightChange[]
    merchant_changes: InsightChange[]
    transactions: BillTransaction[]
  }
  alerts: {
    id: string
    type: 'possible_duplicate' | 'amount_spike' | 'subscription_price_increase'
    title: string
    reason: string
    evidence: BillTransaction[]
    related_subscription_id: string | null
  }[]
  limitations: string[]
  summary: string
}

export type InsightReply = {
  status: 'ok' | 'needs_clarification'
  mode: 'offline'
  message: string
  clarification?: string
  report?: InsightReport
}

export async function fetchInsight(period: string, signal: AbortSignal, query?: { sessionId: string; message: string }): Promise<InsightReply> {
  const response = await fetch(query ? '/api/insights/query' : `/api/insights?period=${encodeURIComponent(period)}`, {
    signal,
    ...(query ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: query.sessionId, message: query.message, period }) } : {}),
  })
  const body = await response.json()
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '暂时无法读取账单，请稍后重试。')
  return body as InsightReply
}
