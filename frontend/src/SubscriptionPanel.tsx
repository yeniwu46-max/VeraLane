import { useEffect, useRef, useState } from 'react'
import type { BillTransaction } from './BillVisuals'
import { formatBankTime } from './bankTime'
import './SubscriptionPanel.css'

type Diagnostic = { merchant: string; subscription_id: string | null; status: 'active' | 'cancelled' | 'candidate'; cycle: string; confidence: string; renewal_on: string | null; estimated_renewal: { from: string; to: string; overdue: boolean } | null; amount_yuan: string; price_change_yuan: string; price_increased: boolean; after_cancel_ids: string[]; limitations: string[]; evidence: BillTransaction[] }
type Reminder = { id: string; title: string; body: string; due_on: string; source_status: 'active' | 'cancelled' | null; read_at: string | null; created_at: string }
type BatchItem = { subscription_id: string; merchant: string; amount_yuan: string; renewal_on: string; status?: 'completed' | 'failed'; message?: string }
type BatchDetails = { items: BatchItem[]; period_start: string; period_end: string; expected_savings_yuan: string }
type BatchAction = { id: string; type: 'subscription_batch'; tier: 'yellow'; expires_at: string; details: BatchDetails }
type BatchReceipt = BatchDetails & { action_id: string; status: 'completed' | 'partially_completed' | 'failed'; message: string }
type Data = { diagnostics: { as_of: string; items: Diagnostic[] }; reminders: { as_of: string; items: Reminder[] } }
export type SubscriptionPanelProps = { sessionId: string; onChanged: () => void }
const statusLabels = { active: '代扣生效中', cancelled: '代扣已取消', candidate: '疑似周期扣费' }
const money = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

async function request<T>(path: string, signal: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(path, { signal, ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) })
  const result = await response.json()
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '请求未完成，请重试。')
  return result as T
}

function EvidenceDialog({ item, onClose }: { item: Diagnostic; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal() }, [])
  return <dialog ref={dialog} className="sub-manager__dialog transaction-dialog" aria-labelledby="subscription-evidence-title" onClose={onClose}>
    <div className="transaction-dialog__head"><div><small>账单证据 · {item.confidence}</small><h2 id="subscription-evidence-title">{item.merchant}</h2></div><button type="button" onClick={() => dialog.current?.close()} aria-label="关闭订阅证据">×</button></div>
    <p className="sub-manager__muted">{statusLabels[item.status]} · 规则推测周期：{item.cycle}</p>
    {item.evidence.length ? <ul className="sub-manager__evidence">{item.evidence.map((tx) => <li key={tx.id}>
      <div><strong>{tx.posted_on}</strong><b>{money(tx.amount_yuan)}</b></div><p>{tx.category} · {tx.note || '无备注'}</p><small>交易编号 {tx.id}</small>
      {item.after_cancel_ids.includes(tx.id) && <p className="sub-manager__attention">这笔记录发生在取消日期之后，请核对扣费来源。</p>}
    </li>)}</ul> : <p className="sub-manager__muted">已保存协议，暂时没有匹配的历史扣费记录。</p>}
    <ul className="sub-manager__limitations">{item.limitations.map((line) => <li key={line}>{line}</li>)}</ul>
  </dialog>
}

function CancellationSummary({ details, receipt }: { details: BatchDetails; receipt?: BatchReceipt }) {
  return <>
    <ul className="sub-manager__batch-items">{details.items.map((item) => <li key={item.subscription_id}><div><strong>{item.merchant}</strong><span>{money(item.amount_yuan)} · 协议扣费日 {item.renewal_on}</span></div>{receipt && <div className="sub-manager__item-result"><b>{item.status === 'completed' ? '已取消代扣' : '未执行'}</b><span>{item.message}</span></div>}</li>)}</ul>
    <p className="sub-manager__saving">预计减少 <strong>{money(details.expected_savings_yuan)}</strong><span>{details.period_start} 至 {details.period_end}</span></p>
    <p className="sub-manager__muted">仅计入上述期间符合条件的协议扣费；不代表退款，商户会员状态与其他支付渠道未知。</p>
  </>
}

function SubscriptionPanelContent({ sessionId, onChanged }: SubscriptionPanelProps) {
  const reads = useRef<AbortController | null>(null)
  const writes = useRef<AbortController | null>(null)
  const [data, setData] = useState<Data | null>(null)
  const [revision, setRevision] = useState(0)
  const [loadError, setLoadError] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<string[]>([])
  const [action, setAction] = useState<BatchAction | null>(null)
  const [reviewed, setReviewed] = useState(false)
  const [receipt, setReceipt] = useState<BatchReceipt | null>(null)
  const [evidence, setEvidence] = useState<Diagnostic | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    reads.current = controller
    Promise.all([request<Data['diagnostics']>('/api/subscriptions/diagnostics', controller.signal), request<Data['reminders']>(`/api/reminders?session_id=${encodeURIComponent(sessionId)}`, controller.signal)])
      .then(([diagnostics, reminders]) => { if (!controller.signal.aborted) { setData({ diagnostics, reminders }); setLoadError('') } })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setLoadError(cause instanceof Error ? cause.message : '无法读取订阅记录。') })
    return () => controller.abort()
  }, [sessionId, revision])
  useEffect(() => () => { reads.current?.abort(); writes.current?.abort() }, [])

  function refresh() {
    setData(null); setLoadError(''); setAction(null); setReviewed(false); setSelected([]); setRevision((value) => value + 1)
  }
  function choose(id: string, checked: boolean) {
    setSelected((previous) => checked ? [...previous, id] : previous.filter((value) => value !== id))
    setAction(null); setReviewed(false); setError('')
  }
  async function mutate<T>(path: string, body: unknown, onSuccess: (value: T) => void) {
    if (writes.current) return
    const controller = new AbortController()
    writes.current = controller
    setBusy(true); setError('')
    try { const value = await request<T>(path, controller.signal, body); if (!controller.signal.aborted) onSuccess(value) }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '操作未完成，请重试。') }
    finally { if (writes.current === controller) { writes.current = null; setBusy(false) } }
  }
  function prepare() {
    if (!selected.length || busy) return
    setReceipt(null); setReviewed(false)
    void mutate<{ pending_action: BatchAction }>('/api/subscriptions/prepare-batch', { session_id: sessionId, subscription_ids: selected }, (value) => setAction(value.pending_action))
  }
  function confirm() {
    if (!action || !reviewed || busy) return
    void mutate<BatchReceipt>(`/api/actions/${action.id}/confirm`, { session_id: sessionId }, (value) => {
      if (!value.items) { setError(value.message || '请刷新订阅状态核对执行结果。'); return }
      setReceipt(value); refresh(); onChanged()
    })
  }
  function markRead(id: string) {
    void mutate(`/api/reminders/${encodeURIComponent(id)}/read`, { session_id: sessionId }, () => setRevision((value) => value + 1))
  }

  return <section className="sub-manager" aria-label="智能订阅管家">
    <header className="sub-manager__header"><div><h1>订阅管理</h1><p>核对周期扣费，选择要取消的银行代扣协议。</p></div><button className="sub-manager__quiet" type="button" disabled={busy} onClick={refresh}>刷新记录</button></header>
    {loadError && <p className="sub-manager__error" role="alert">{loadError}<button type="button" onClick={refresh}>重试</button></p>}
    {!data && !loadError && <p className="sub-manager__muted" role="status">正在核对协议、扣费记录与提醒…</p>}
    {data && <>
      <div className="sub-manager__source">规则识别 · 模拟账本与已保存协议<span>数据截至 {data.diagnostics.as_of}</span></div>
      <div className="sub-manager__list">
        {!data.diagnostics.items.length && <p className="sub-manager__muted">当前没有已保存协议或稳定的周期扣费线索。</p>}
        {data.diagnostics.items.map((item) => <article className="sub-manager__row" key={item.subscription_id || item.merchant}>
          <div className="sub-manager__row-title"><label>{item.status === 'active' && item.subscription_id && <input type="checkbox" checked={selected.includes(item.subscription_id)} disabled={busy} onChange={(event) => choose(item.subscription_id!, event.target.checked)} aria-label={`选择取消${item.merchant}银行代扣`} />}<strong>{item.merchant}</strong></label><span className={`sub-manager__status sub-manager__status--${item.status}`}>{statusLabels[item.status]}</span><b>{money(item.amount_yuan)}</b></div>
          <div className="sub-manager__row-facts"><span>{item.confidence} · 周期 {item.cycle}</span>{item.renewal_on && <span>协议续费日 {item.renewal_on}</span>}{item.status === 'cancelled' && <span>商户会员状态未知</span>}{item.estimated_renewal && <span>预计 {item.estimated_renewal.from}—{item.estimated_renewal.to}{item.estimated_renewal.overdue ? '（历史预测，待核对）' : ''}</span>}</div>
          <div className="sub-manager__row-actions"><span>{item.price_increased ? `最近两笔扣费增加 ${money(item.price_change_yuan)}` : item.status === 'candidate' ? '未找到对应协议，暂不能在此取消' : '按保存的协议核对金额与日期'}{item.after_cancel_ids.length > 0 && ` · 取消后有 ${item.after_cancel_ids.length} 笔记录待核对`}</span><button type="button" className="sub-manager__quiet" onClick={() => setEvidence(item)}>查看依据 · {item.evidence.length} 笔</button></div>
        </article>)}
      </div>
      <div className="sub-manager__selection"><p>已选择 {selected.length} 项代扣<span>只处理你选中的协议。</span></p><button type="button" className="sub-manager__primary" disabled={busy || !selected.length} onClick={prepare}>{busy ? '正在处理…' : '预览取消计划'}</button></div>
      <details className="sub-manager__reminders" open={data.reminders.items.some((item) => !item.read_at && item.source_status === 'active')}><summary>站内提醒 <span>{data.reminders.items.filter((item) => !item.read_at).length} 条未读</span></summary>
        {!data.reminders.items.length && <p className="sub-manager__muted">当前没有需要提醒的续费。</p>}
        {data.reminders.items.map((item) => <article key={item.id} className="sub-manager__reminder"><div><strong>{item.source_status === 'cancelled' ? '历史续费提醒 · 协议已取消' : item.source_status === 'active' ? item.title : '历史提醒 · 协议状态待核对'}</strong><p>{item.source_status === 'active' ? item.body : `原记录日期 ${item.due_on}。当前${item.source_status === 'cancelled' ? '银行代扣已取消' : '协议状态待核对'}，此提醒保留供查阅。`}</p><small>生成于 {formatBankTime(item.created_at)} · 演示时间{item.read_at && ' · 已读'}</small></div>{!item.read_at && <button type="button" className="sub-manager__quiet" disabled={busy} onClick={() => markRead(item.id)}>标为已读</button>}</article>)}
      </details>
    </>}
    {error && <p className="sub-manager__error" role="alert">{error}</p>}
    {action && <section className="sub-manager__confirmation" aria-label="取消银行代扣确认"><h2>核对取消计划</h2><CancellationSummary details={action.details} /><p className="sub-manager__muted">确认有效至 {formatBankTime(action.expires_at)}（实际时间）。修改选择后需重新核对。</p><label className="sub-manager__review"><input type="checkbox" checked={reviewed} disabled={busy} onChange={(event) => setReviewed(event.target.checked)} />我已逐项核对，同意取消以上模拟银行代扣；商户服务需另行管理。</label><button type="button" className="sub-manager__confirm" disabled={busy || !reviewed} onClick={confirm}>{busy ? '正在执行…' : `确认取消 ${action.details.items.length} 项代扣`}</button></section>}
    {receipt && <section className="sub-manager__receipt" aria-live="polite"><h2>{receipt.status === 'completed' ? '取消结果' : receipt.status === 'partially_completed' ? '部分完成，请核对未执行项' : '未执行，请核对原因'}</h2><p>{receipt.message}</p><CancellationSummary details={receipt} receipt={receipt} /><small>操作编号 {receipt.action_id}</small></section>}
    {evidence && <EvidenceDialog item={evidence} onClose={() => setEvidence(null)} />}
  </section>
}

export function SubscriptionPanel(props: SubscriptionPanelProps) {
  return <SubscriptionPanelContent key={props.sessionId} {...props} />
}
