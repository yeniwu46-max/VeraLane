import { useEffect, useRef, useState, type FormEvent } from 'react'
import type { BillTransaction } from './BillVisuals'
import type { InsightReport } from './insightApi'
import { OperationConfirm } from './OperationConfirm'
import { requestJson, type Operation } from './bankingApi'
import { formatBankTime } from './bankTime'
import './PlansPanel.css'

type PlanStatus = 'draft' | 'awaiting_confirmation' | 'completed' | 'partially_completed' | 'failed' | 'cancelled'
type PlanOption = { subscription_id: string; merchant: string; amount_yuan: string; renewal_on: string; eligible_in_period: boolean; evidence: BillTransaction[] }
type PlanResult = { status: 'completed' | 'partially_completed' | 'failed'; message: string; action_id: string; expected_savings_yuan: string; remaining_target_yuan: string; items: { subscription_id: string; merchant: string; amount_yuan: string; renewal_on: string; status: 'completed' | 'failed'; message: string }[] }
type SpendingPlan = { id: string; version: number; status: PlanStatus; goal: string; target_yuan: string; period_start: string; period_end: string; selected_subscription_ids: string[]; expected_savings_yuan: string; remaining_target_yuan: string; options: PlanOption[]; steps: { id: string; label: string; status: string; summary: string; evidence_ids: string[]; depends_on: string[] }[]; insight_summary: string; insight_report: InsightReport; created_at: string; updated_at: string; pending_action_id: string | null; result: PlanResult | null }
export type PlansPanelProps = { sessionId: string; onChanged: () => void; initialMessage?:string; initialPlanId?:string }
const labels: Record<string, string> = { draft: '待选择', awaiting_input: '待选择', awaiting_confirmation: '待确认', blocked: '等待前序步骤', completed: '已完成', partially_completed: '部分完成', failed: '未完成', cancelled: '已取消' }
const terminal = (plan: SpendingPlan) => ['completed', 'partially_completed', 'failed', 'cancelled'].includes(plan.status)
const money = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function EvidenceDialog({ title, transactions, onClose }: { title: string; transactions: BillTransaction[]; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal() }, [])
  return <dialog ref={dialog} className="transaction-dialog plans-panel__dialog" aria-labelledby="plan-evidence-title" onClose={onClose}><div className="transaction-dialog__head"><div><small>模拟账本 · 计划证据</small><h2 id="plan-evidence-title">{title}</h2></div><button type="button" onClick={() => dialog.current?.close()} aria-label="关闭计划证据">×</button></div>
    {!transactions.length ? <p className="plans-panel__muted">当前没有匹配的交易证据。</p> : <ul className="plans-panel__evidence">{transactions.map((tx) => <li key={tx.id}><div><strong>{tx.counterparty}</strong><b>{money(tx.amount_yuan)}</b></div><p>{tx.posted_on} · {tx.category} · {tx.note || '无备注'}</p><small>交易编号 {tx.id}</small></li>)}</ul>}
  </dialog>
}

function PlanWorkspace({ initial, sessionId, onChanged, onUpdated }: { initial: SpendingPlan; sessionId: string; onChanged: () => void; onUpdated: () => void }) {
  const [plan, setPlan] = useState(initial)
  const [selected, setSelected] = useState(initial.selected_subscription_ids)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [evidence, setEvidence] = useState<{ title: string; transactions: BillTransaction[] } | null>(null)
  const writes = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; writes.current?.abort() } }, [])

  async function mutate<T>(path: string, body: unknown, done: (response: T) => void) {
    if (writes.current) return
    const controller = new AbortController(); writes.current = controller
    setBusy(true); setError(''); setNotice('')
    try { const response = await requestJson<T>(path, body, controller.signal); if (!controller.signal.aborted) { done(response); onUpdated() } }
    catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : '操作未完成，请重试。')
        try {
          const latest = await requestJson<SpendingPlan>(`/api/plans/${plan.id}?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal)
          if (!controller.signal.aborted) { setPlan(latest); if (latest.status !== 'draft') setSelected(latest.selected_subscription_ids); onUpdated() }
        } catch { /* Keep the original operation error when status cannot be read. */ }
      }
    }
    finally { if (mounted.current && writes.current === controller) { writes.current = null; setBusy(false) } }
  }
  function prepare() {
    if (busy || !selected.length || terminal(plan)) return
    void mutate<{ plan: SpendingPlan; pending_action: Operation }>(`/api/plans/${plan.id}/prepare`, { session_id: sessionId, subscription_ids: selected }, (value) => { setPlan(value.plan); setOperation(value.pending_action) })
  }
  function edit() {
    if (busy || terminal(plan)) return
    setOperation(null)
    void mutate<SpendingPlan>(`/api/plans/${plan.id}/invalidate`, { session_id: sessionId }, (value) => { setPlan(value); setNotice('旧授权已失效，可以修改选择并重新核对。') })
  }
  function cancel() {
    if (busy || terminal(plan)) return
    setOperation(null)
    void mutate<SpendingPlan>(`/api/plans/${plan.id}/cancel`, { session_id: sessionId }, (value) => { setPlan(value); setNotice(value.status === 'cancelled' ? '计划已取消，未取消任何银行代扣协议。' : `计划当前${labels[value.status]}，请以下方实际回执为准。`) })
  }
  function completed(value: Record<string, unknown>) {
    if (!mounted.current) return
    const result = value as PlanResult
    if (!Array.isArray(result.items)) { setOperation(null); setError('请重新打开此计划，核对服务端的执行结果。'); onUpdated(); onChanged(); return }
    setPlan((current) => ({ ...current, status: result.status, result, expected_savings_yuan: result.expected_savings_yuan, remaining_target_yuan: result.remaining_target_yuan, steps: current.steps.map((step) => step.id === 'cancel' ? { ...step, status: result.status, summary: result.message } : step) }))
    setOperation(null); onUpdated(); onChanged()
  }
  const editable = !terminal(plan) && plan.status === 'draft' && !busy
  const comparison = plan.insight_report.comparison
  const previousEvidenceCount = comparison.transactions.filter((tx) => comparison.previous_start <= tx.posted_on && tx.posted_on <= comparison.previous_end).length
  return <section className="plans-panel__workspace" aria-label="支出优化计划详情">
    <header className="plans-panel__detail-head"><div><h2>{plan.goal}</h2><p>{plan.period_start} 至 {plan.period_end} · 计划 v{plan.version}</p></div><span className="plans-panel__status">{labels[plan.status]}</span></header>
    <div className="plans-panel__metrics"><div><span>目标减少</span><strong>{money(plan.target_yuan)}</strong></div><div><span>{plan.result ? '执行后预计减少' : '已核对选择预计减少'}</span><strong>{money(plan.expected_savings_yuan)}</strong></div><div><span>距目标还差</span><strong>{money(plan.remaining_target_yuan)}</strong></div></div>
    <p className="plans-panel__muted">预计金额仅适用于本计划期间，不代表已退款现金。未选择的协议继续生效。</p>
    <ol className="plans-panel__steps">{plan.steps.map((step, index) => <li key={step.id}><span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><div><div><h3>{step.label}</h3><small>{labels[step.status] || step.status}</small></div><p>{step.summary}</p>{step.id === 'query' && <><p className={previousEvidenceCount ? 'plans-panel__comparison' : 'plans-panel__comparison plans-panel__comparison--missing'} role="status">比较基期 {comparison.previous_start} 至 {comparison.previous_end}：{previousEvidenceCount} 笔匹配流水。{previousEvidenceCount ? '金额变化只说明账单贡献，不代表消费动机。' : '没有基期证据，暂时不能判断支出变化原因。'}</p><details className="plans-panel__analysis" open={!previousEvidenceCount}><summary>分析范围与限制</summary><p>本期报告 {plan.insight_report.start_date} 至 {plan.insight_report.end_date}，匹配 {plan.insight_report.transaction_count} 笔；所示覆盖日期不代表账单完整。</p><ul>{plan.insight_report.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul></details><button type="button" className="plans-panel__quiet" onClick={() => setEvidence({ title: '消费变化依据', transactions: plan.insight_report.comparison.transactions })}>查看比较期间交易</button></>}</div></li>)}</ol>
    <section className="plans-panel__options" aria-label="可选择的银行代扣协议"><h3>{terminal(plan) ? '计划核实的协议快照' : '选择允许取消的代扣'}</h3>
      {!plan.options.length && <p className="plans-panel__muted">没有可取消的生效协议，当前无法通过代扣优化达到目标。</p>}
      {plan.options.map((option) => <article key={option.subscription_id}><div><label><input type="checkbox" checked={selected.includes(option.subscription_id)} disabled={!editable} onChange={(event) => { setSelected((current) => event.target.checked ? [...current, option.subscription_id] : current.filter((id) => id !== option.subscription_id)); setOperation(null) }} /><strong>{option.merchant}</strong></label><b>{money(option.amount_yuan)}</b></div><p>已保存协议 · 扣费日 {option.renewal_on}<span>{option.eligible_in_period ? '计入本计划期间' : '不在本计划期间，不计入目标减少金额'}</span></p><button type="button" className="plans-panel__quiet" onClick={() => setEvidence({ title: `${option.merchant}扣费依据`, transactions: option.evidence })}>查看 {option.evidence.length} 笔依据</button></article>)}
    </section>
    {!terminal(plan) && <div className="plans-panel__actions">{plan.status === 'awaiting_confirmation' && <button type="button" className="plans-panel__quiet" disabled={busy} onClick={edit}>修改选择并使旧授权失效</button>}{!operation && <button type="button" className="plans-panel__primary" disabled={busy || !selected.length} onClick={prepare}>{busy ? '正在核对…' : plan.status === 'awaiting_confirmation' ? '重新核对当前选择' : `核对 ${selected.length} 项选择`}</button>}<button type="button" className="plans-panel__quiet" disabled={busy} onClick={cancel}>取消此未执行计划</button></div>}
    {error && <p className="plans-panel__error" role="alert">{error}</p>}{notice && <p className="plans-panel__notice" role="status">{notice}</p>}
    {operation && <OperationConfirm key={operation.id} action={operation} sessionId={sessionId} title="确认所选协议与减少金额" onDone={completed}><div className="plans-panel__authorization"><p>本次取消 {plan.selected_subscription_ids.length} 项银行代扣，预计减少 {money(plan.expected_savings_yuan)}，距目标还差 {money(plan.remaining_target_yuan)}。</p><ul>{plan.options.filter((option) => plan.selected_subscription_ids.includes(option.subscription_id)).map((option) => <li key={option.subscription_id}>{option.merchant} · {money(option.amount_yuan)} · {option.renewal_on}</li>)}</ul><p>期间 {plan.period_start} 至 {plan.period_end}。有效至 {formatBankTime(operation.expires_at)}（实际时间）。取消代扣不等于退订商户会员。</p></div></OperationConfirm>}
    {plan.result && <section className="plans-panel__results" aria-live="polite"><h3>逐项执行结果</h3><p>{plan.result.message}</p>{plan.result.items.map((item) => <article key={item.subscription_id}><div><strong>{item.merchant}</strong><span>{item.status === 'completed' ? '已取消模拟代扣' : '未执行'}</span></div><p>{item.message}</p></article>)}<small>操作编号 {plan.result.action_id}</small></section>}
    <p className="plans-panel__foot">计划编号 {plan.id} · 创建于 {formatBankTime(plan.created_at)}（实际时间）</p>
    {evidence && <EvidenceDialog {...evidence} onClose={() => setEvidence(null)} />}
  </section>
}

function PlansPanelContent({ sessionId, onChanged, initialMessage, initialPlanId }: PlansPanelProps) {
  const [message, setMessage] = useState(initialMessage || '下个月少花300元')
  const [plans, setPlans] = useState<SpendingPlan[] | null>(null)
  const [active, setActive] = useState<SpendingPlan | null>(null)
  const [workspaceKey, setWorkspaceKey] = useState(0)
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [listError, setListError] = useState('')
  const [clarification, setClarification] = useState('')
  const writes = useRef<AbortController | null>(null)
  const reads = useRef<AbortController | null>(null)
  useEffect(()=>{
    if(!initialPlanId)return
    const controller=new AbortController()
    requestJson<SpendingPlan>(`/api/plans/${initialPlanId}?session_id=${encodeURIComponent(sessionId)}`,undefined,controller.signal).then(setActive).catch(e=>{if(!controller.signal.aborted)setError(e instanceof Error?e.message:'读取计划失败')})
    return()=>controller.abort()
  },[sessionId,initialPlanId])
  useEffect(() => {
    const controller = new AbortController()
    requestJson<{ plans: SpendingPlan[] }>(`/api/plans?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal).then((value) => { if (!controller.signal.aborted) { setPlans(value.plans); setListError('') } }).catch((cause: unknown) => { if (!controller.signal.aborted) setListError(cause instanceof Error ? cause.message : '无法读取计划列表。') })
    return () => controller.abort()
  }, [sessionId, revision])
  useEffect(() => () => { writes.current?.abort(); reads.current?.abort() }, [])
  async function create(event: FormEvent) {
    event.preventDefault()
    if (!message.trim() || writes.current) return
    reads.current?.abort()
    const controller = new AbortController(); writes.current = controller
    setBusy(true); setError(''); setClarification('')
    try {
      const value = await requestJson<{ status: string; message: string; clarification?: string; plan?: SpendingPlan }>('/api/plans/preview', { session_id: sessionId, message: message.trim() }, controller.signal)
      if (!controller.signal.aborted) { if (value.plan) { setActive(value.plan); setWorkspaceKey((key) => key + 1); setRevision((current) => current + 1) } else setClarification(value.clarification || value.message) }
    } catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '无法生成计划。') }
    finally { if (writes.current === controller) { writes.current = null; setBusy(false) } }
  }
  async function open(id: string) {
    if (busy) return
    reads.current?.abort()
    const controller = new AbortController(); reads.current = controller
    setError(''); setClarification('')
    try { const value = await requestJson<SpendingPlan>(`/api/plans/${id}?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal); if (!controller.signal.aborted) { setActive(value); setWorkspaceKey((key) => key + 1) } }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '读取计划失败。') }
  }
  return <section className="plans-panel" aria-label="跨场景任务中心"><header><h1>任务中心</h1><p>说出支出目标，核对证据与计划，再决定允许执行的步骤。</p></header>
    <form className="plans-panel__form" onSubmit={create}><label htmlFor="spending-goal">支出优化目标</label><div><input id="spending-goal" value={message} onChange={(event) => setMessage(event.target.value)} maxLength={500} placeholder="先分析本月账单，下个月少花300元" /><button type="submit" className="plans-panel__primary" disabled={busy || !message.trim()}>{busy ? '正在查询并核实…' : '制定计划'}</button></div><small>规则解析 · 最多四步 · 支持下一个自然月的减少支出目标</small></form>
    {error && <p className="plans-panel__error" role="alert">{error}</p>}{clarification && <p className="plans-panel__notice" role="status">{clarification}</p>}
    <details className="plans-panel__history" open={!active}><summary>已保存计划 <span>{plans?.length ?? '…'}</span></summary>{listError ? <p className="plans-panel__error" role="alert">{listError}<button type="button" onClick={() => setRevision((current) => current + 1)}>重试</button></p> : plans === null ? <p className="plans-panel__muted" role="status">正在读取计划…</p> : !plans.length ? <p className="plans-panel__muted">暂无计划。提交目标后，查询结果和每一步状态会保存到这里。</p> : plans.map((plan) => <button type="button" key={plan.id} disabled={busy} aria-pressed={active?.id === plan.id} onClick={() => void open(plan.id)}><span><strong>{plan.goal}</strong><small>{plan.period_start}—{plan.period_end} · {formatBankTime(plan.created_at)}</small></span><b>{labels[plan.status]}</b></button>)}</details>
    {active && <PlanWorkspace key={`${active.id}:${workspaceKey}`} initial={active} sessionId={sessionId} onChanged={onChanged} onUpdated={() => setRevision((current) => current + 1)} />}
  </section>
}

export function PlansPanel(props: PlansPanelProps) {
  return <PlansPanelContent key={props.sessionId} {...props} />
}
