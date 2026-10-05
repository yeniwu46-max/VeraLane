import { useEffect, useRef, useState, type FormEvent } from 'react'
import { OperationConfirm } from './OperationConfirm'
import { requestJson, type Operation } from './bankingApi'
import { formatBankTime } from './bankTime'
import './AdvancedTransfersPanel.css'

type Contact = { id: string; name: string; phone_masked: string }
type Transfer = { contact_id: string; recipient: string; phone_masked: string; amount_yuan: string; note: string }
type Occurrence = { id?: string; index?: number; occurrence_index?: number; execute_at: string; window_expires_at: string; status?: string; transaction_id?: string | null; failure_reason?: string | null; finished_at?: string | null }
type RecurringDetails = Transfer & { first_at: string; monthly_day: number; count: number; total_yuan: string; occurrences: Occurrence[]; skipped_months: string[]; notice: string; available_now_yuan?: string }
type BatchItem = Transfer & { item_id: string; id?: string; status?: string; transaction_id?: string | null; failure_reason?: string | null }
type BatchDetails = { items: BatchItem[]; total_yuan: string; count: number; notice: string; available_now_yuan?: string; warnings?: string[] }
type RecurringPlan = RecurringDetails & { id: string; status: string; created_at: string }
type Batch = BatchDetails & { id: string; status: string; created_at: string }
type Data = { recurring: { plans: RecurringPlan[] }; batch: { batches: Batch[] } }
type RecurringDraft = { contact_id: string | null; amount_yuan: string | null; note: string; first_at: string | null; monthly_day: number | null; count: number | null }
type BatchDraft = { items: { contact_id: string | null; recipient: string; amount_yuan: string; note: string }[] }
type Interpretation = { kind: 'recurring' | 'batch'; status: string; message: string; needs_review: string[]; draft: RecurringDraft | BatchDraft }
type BatchRow = { key: number; contact_id: string; amount_yuan: string; note: string; recipient?: string }
type Preview = { kind: 'recurring'; details: RecurringDetails; body: unknown } | { kind: 'batch'; details: BatchDetails; body: unknown }
export type AdvancedTransfersPanelProps = { sessionId: string; contacts: Contact[]; onChanged: () => void }
const labels: Record<string, string> = { active: '进行中', running: '执行中', pending: '待执行', completed: '已完成', partially_completed: '部分完成', failed: '未完成', expired: '超窗未执行', cancelled: '已取消' }
const money = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function RecurringFacts({ details }: { details: RecurringDetails }) {
  return <><dl><div><dt>收款人</dt><dd>{details.recipient} · {details.phone_masked}</dd></div><div><dt>每笔金额 / 实际次数</dt><dd>{money(details.amount_yuan)} / {details.count} 次</dd></div><div><dt>备注</dt><dd>{details.note}</dd></div></dl><div className="advanced-transfer__total">总授权金额 <strong>{money(details.total_yuan)}</strong></div><ol className="advanced-transfer__periods">{details.occurrences.map((item, index) => <li key={item.id || item.execute_at}><strong>第 {item.occurrence_index || item.index || index + 1} 期 · {formatBankTime(item.execute_at)}</strong>{item.status && <span>{labels[item.status] || item.status}</span>}<p>执行窗口截至 {formatBankTime(item.window_expires_at)}（演示时间）</p>{item.failure_reason && <p>{item.failure_reason}</p>}{item.transaction_id && <small>交易编号 {item.transaction_id}</small>}{item.finished_at && <small>处理时间 {formatBankTime(item.finished_at)}</small>}</li>)}</ol><p>每月 {details.monthly_day} 日；{details.skipped_months.length ? `跳过无此日的月份：${details.skipped_months.join('、')}。` : '本次展开没有需要跳过的月份。'}</p><p>{details.notice}</p></>
}

function BatchFacts({ details }: { details: BatchDetails }) {
  return <><div className="advanced-transfer__total">共 {details.count} 笔 · 总授权金额 <strong>{money(details.total_yuan)}</strong></div><ul className="advanced-transfer__list">{details.items.map((item, index) => <li key={item.id || item.item_id}><div><strong>{index + 1}. {item.recipient} · {item.phone_masked}</strong><b>{money(item.amount_yuan)}{item.status && ` · ${labels[item.status] || item.status}`}</b></div><p>备注：{item.note}</p>{item.failure_reason && <p>未执行原因：{item.failure_reason}</p>}{item.transaction_id && <small>交易编号 {item.transaction_id}</small>}</li>)}</ul>{details.warnings?.map((warning) => <p className="advanced-transfer__error" key={warning}>{warning}</p>)}<p>{details.notice}</p></>
}

function Authorization({ operation }: { operation: Operation }) {
  if (operation.type === 'recurring_cancel') {
    const details = operation.details as { plan_id: string; remaining: { id: string; execute_at: string }[]; notice: string }
    return <div className="advanced-transfer__authorization"><p>取消剩余 {details.remaining.length} 期。{details.notice}</p><ol className="advanced-transfer__periods">{details.remaining.map((item) => <li key={item.id}>{formatBankTime(item.execute_at)}</li>)}</ol><p>计划编号 {details.plan_id}</p></div>
  }
  return <div className="advanced-transfer__authorization">{operation.type === 'recurring_transfer' ? <RecurringFacts details={operation.details as RecurringDetails} /> : <BatchFacts details={operation.details as BatchDetails} />}<p>当前确认有效至 {formatBankTime(operation.expires_at)}（实际时间）。对象、金额、日期或备注改变后必须重新核对。</p></div>
}

function ContactSelect({ contacts, value, disabled, onChange }: { contacts: Contact[]; value: string; disabled: boolean; onChange: (value: string) => void }) {
  return <select value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)} required><option value="">请选择已验证联系人</option>{contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.name} · {contact.phone_masked}</option>)}</select>
}

function PanelContent({ sessionId, contacts, onChanged }: AdvancedTransfersPanelProps) {
  const [kind, setKind] = useState<'recurring' | 'batch'>('recurring')
  const [revision, setRevision] = useState(0)
  const key = `${sessionId}:${revision}`
  const [loaded, setLoaded] = useState<{ key: string; data?: Data; error?: string } | null>(null)
  const current = loaded?.key === key ? loaded : null
  const data = current?.data
  const [contactId, setContactId] = useState('')
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('周期转账')
  const [firstAt, setFirstAt] = useState('')
  const [day, setDay] = useState('5')
  const [count, setCount] = useState('3')
  const [rows, setRows] = useState<BatchRow[]>([{ key: 1, contact_id: '', amount_yuan: '', note: '转账' }, { key: 2, contact_id: '', amount_yuan: '', note: '转账' }])
  const rowKey = useRef(3)
  const [question, setQuestion] = useState('')
  const [interpretation, setInterpretation] = useState<Interpretation | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [focusId, setFocusId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const writes = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    const controller = new AbortController()
    const query = `session_id=${encodeURIComponent(sessionId)}`
    Promise.all([requestJson<Data['recurring']>(`/api/transfers/recurring?${query}`, undefined, controller.signal), requestJson<Data['batch']>(`/api/transfers/batch?${query}`, undefined, controller.signal)]).then(([recurring, batch]) => { if (!controller.signal.aborted) setLoaded({ key, data: { recurring, batch } }) }).catch((cause: unknown) => { if (!controller.signal.aborted) setLoaded({ key, error: cause instanceof Error ? cause.message : '转账计划读取失败。' }) })
    return () => controller.abort()
  }, [sessionId, key])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; writes.current?.abort() } }, [])
  function dirty() { setPreview(null); setOperation(null) }
  function refresh() { dirty(); setRevision((value) => value + 1) }
  async function mutate<T>(path: string, body: unknown, done: (value: T) => void) {
    if (writes.current) return
    const controller = new AbortController(); writes.current = controller
    setBusy(true); setError(''); setNotice(''); setOperation(null)
    try { const value = await requestJson<T>(path, body, controller.signal); if (!controller.signal.aborted) done(value) }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '请求未完成，请核对输入。') }
    finally { if (mounted.current && writes.current === controller) { writes.current = null; setBusy(false) } }
  }
  function interpret(event: FormEvent) { event.preventDefault(); dirty(); setInterpretation(null); void mutate<Omit<Interpretation, 'kind'>>('/api/transfers/interpret', { session_id: sessionId, message: question.trim(), kind }, (value) => setInterpretation({ ...value, kind })) }
  function syncDraft() {
    if (!interpretation) return
    dirty()
    if (interpretation.kind === 'recurring') {
      const draft = interpretation.draft as RecurringDraft
      setContactId(draft.contact_id || '')
      if (typeof draft.amount_yuan === 'string') setAmount(draft.amount_yuan)
      if (typeof draft.first_at === 'string') setFirstAt(draft.first_at.slice(0, 16))
      if (typeof draft.monthly_day === 'number') setDay(String(draft.monthly_day))
      if (typeof draft.count === 'number') setCount(String(draft.count))
      if (draft.note) setNote(draft.note)
    } else {
      const draft = interpretation.draft as BatchDraft
      if (draft.items?.length) setRows(draft.items.map((item) => ({ key: rowKey.current++, contact_id: item.contact_id || '', amount_yuan: item.amount_yuan || '', note: item.note || '转账', recipient: item.recipient })))
    }
    setNotice('草稿已填入。未确定的收款人需手动选择，核对所有字段后再预览。')
  }
  function previewRecurring(event: FormEvent) {
    event.preventDefault(); dirty()
    const body = { session_id: sessionId, contact_id: contactId, amount_yuan: amount.trim(), note: note.trim(), first_at: firstAt, monthly_day: Number(day), count: Number(count) }
    void mutate<RecurringDetails>('/api/transfers/recurring/preview', body, (details) => setPreview({ kind: 'recurring', details, body }))
  }
  function previewBatch(event: FormEvent) {
    event.preventDefault(); dirty()
    const body = { session_id: sessionId, items: rows.map((row) => ({ contact_id: row.contact_id, amount_yuan: row.amount_yuan.trim(), note: row.note.trim() })) }
    void mutate<BatchDetails>('/api/transfers/batch/preview', body, (details) => setPreview({ kind: 'batch', details, body }))
  }
  function prepare() { if (preview) void mutate<{ pending_action: Operation }>(`/api/transfers/${preview.kind}/prepare`, preview.body, (value) => { setOperation(value.pending_action); setPreview(null) }) }
  function cancel(id: string) { dirty(); void mutate<{ pending_action: Operation }>(`/api/transfers/recurring/${id}/cancel/prepare`, { session_id: sessionId }, (value) => setOperation(value.pending_action)) }
  function changeRow(key: number, field: 'contact_id' | 'amount_yuan' | 'note', value: string) { setRows((previous) => previous.map((row) => row.key === key ? { ...row, [field]: value } : row)); dirty() }
  function completed(value: Record<string, unknown>) { if (mounted.current) { setNotice(typeof value.message === 'string' ? value.message : '操作已处理，请核对逐项回执。'); setFocusId(typeof value.plan_id === 'string' ? value.plan_id : typeof value.batch_id === 'string' ? value.batch_id : null); refresh(); onChanged() } }
  return <section className="advanced-transfer" aria-label="周期与批量转账"><header><h2>周期与批量转账</h2><button type="button" className="advanced-transfer__quiet" disabled={busy} onClick={refresh}>刷新计划</button></header><div className="advanced-transfer__tabs"><button type="button" className="advanced-transfer__quiet" aria-pressed={kind === 'recurring'} disabled={busy} onClick={() => { setKind('recurring'); setInterpretation(null); dirty() }}>周期转账</button><button type="button" className="advanced-transfer__quiet" aria-pressed={kind === 'batch'} disabled={busy} onClick={() => { setKind('batch'); setInterpretation(null); dirty() }}>批量转账</button></div>
    <section className="advanced-transfer__section"><h3>一句话填写草稿</h3><form autoComplete="off" className="advanced-transfer__form" onSubmit={interpret}><label>{kind === 'recurring' ? '周期需求' : '批量需求'}<input value={question} disabled={busy} maxLength={500} onChange={(event) => { setQuestion(event.target.value); dirty() }} placeholder={kind === 'recurring' ? '每月5日给林悦300元，连续3期' : '给林悦100元，给陈晨200元'} /></label><button type="submit" className="advanced-transfer__primary" disabled={busy || !question.trim()}>提取草稿</button></form>{interpretation && <div className="advanced-transfer__draft"><p>{interpretation.message}</p>{interpretation.kind === 'recurring' ? <p>收款人：{contacts.find((contact) => contact.id === (interpretation.draft as RecurringDraft).contact_id)?.name || '待手动选择'} · 每笔 {(interpretation.draft as RecurringDraft).amount_yuan || '待填写'} 元 · 每月 {(interpretation.draft as RecurringDraft).monthly_day || '待填写'} 日 · {(interpretation.draft as RecurringDraft).count || '待填写'} 期</p> : <ul>{(interpretation.draft as BatchDraft).items?.map((item, index) => <li key={index}>{item.recipient} · {item.amount_yuan} 元{!item.contact_id && ' · 需选择具体联系人'}</li>)}</ul>}{interpretation.needs_review.map((line) => <p key={line}>{line}</p>)}<button type="button" className="advanced-transfer__quiet" disabled={busy} onClick={syncDraft}>将草稿填入下方表单</button></div>}<p className="advanced-transfer__muted">规则解析只生成草稿；完整首日时间与同名联系人由你明确核对。</p></section>
    {kind === 'recurring' ? <section className="advanced-transfer__section"><h3>有限次数的月度转账</h3><form autoComplete="off" onSubmit={previewRecurring}><div className="advanced-transfer__form"><label>收款人<ContactSelect contacts={contacts} value={contactId} disabled={busy} onChange={(value) => { setContactId(value); dirty() }} /></label><label>每笔金额（元）<input value={amount} disabled={busy} inputMode="decimal" required onChange={(event) => { setAmount(event.target.value); dirty() }} /></label><label>备注<input value={note} disabled={busy} maxLength={100} onChange={(event) => { setNote(event.target.value); dirty() }} /></label></div><div className="advanced-transfer__form"><label>首个执行时间（北京时间）<input type="datetime-local" value={firstAt} disabled={busy} required onChange={(event) => { setFirstAt(event.target.value); dirty() }} /></label><label>每月日号（1–31）<input type="number" min={1} max={31} step={1} value={day} disabled={busy} required onChange={(event) => { setDay(event.target.value); dirty() }} /></label><label>实际执行次数（1–12）<input type="number" min={1} max={12} step={1} value={count} disabled={busy} required onChange={(event) => { setCount(event.target.value); dirty() }} /></label></div><p className="advanced-transfer__muted">首日须与日号一致；某月没有该日则跳过。确认不预留资金，每期只在已授权的 10 分钟窗口内执行，失败不补扣。</p><button type="submit" className="advanced-transfer__primary" disabled={busy}>展开日期并预览</button></form></section> : <section className="advanced-transfer__section"><h3>逐行支付给不同联系人</h3><p className="advanced-transfer__muted">这是实际转出模拟资金的批量付款；请逐笔填写金额和备注，每批 2–10 位不同收款人。</p><form autoComplete="off" onSubmit={previewBatch}>{rows.map((row, index) => <div className="advanced-transfer__batch-row" key={row.key}><div><strong>第 {index + 1} 笔{row.recipient && !row.contact_id && ` · 原始对象：${row.recipient}`}</strong><button type="button" className="advanced-transfer__quiet" disabled={busy || rows.length <= 2} onClick={() => { setRows((previous) => previous.filter((item) => item.key !== row.key)); dirty() }}>删除此行</button></div><div className="advanced-transfer__form"><label>收款人<ContactSelect contacts={contacts} value={row.contact_id} disabled={busy} onChange={(value) => changeRow(row.key, 'contact_id', value)} /></label><label>金额（元）<input value={row.amount_yuan} disabled={busy} inputMode="decimal" required onChange={(event) => changeRow(row.key, 'amount_yuan', event.target.value)} /></label><label>备注<input value={row.note} disabled={busy} maxLength={100} onChange={(event) => changeRow(row.key, 'note', event.target.value)} /></label></div></div>)}<div className="advanced-transfer__actions"><button type="button" className="advanced-transfer__quiet" disabled={busy || rows.length >= 10} onClick={() => { const nextKey = rowKey.current++; setRows((previous) => [...previous, { key: nextKey, contact_id: '', amount_yuan: '', note: '转账' }]); dirty() }}>添加收款人</button><button type="submit" className="advanced-transfer__primary" disabled={busy || rows.length < 2}>逐行核对并预览</button></div></form></section>}
    {error && <p className="advanced-transfer__error" role="alert">{error}</p>}{notice && <p className="advanced-transfer__notice" role="status">{notice}</p>}
    {preview && <section className="advanced-transfer__section"><h3>计划预览 · 尚未授权</h3><div className="advanced-transfer__authorization">{preview.kind === 'recurring' ? <RecurringFacts details={preview.details} /> : <BatchFacts details={preview.details} />}{preview.details.available_now_yuan && <p>当前可用余额 {money(preview.details.available_now_yuan)}。实际执行时重新检查。</p>}</div><button type="button" className="advanced-transfer__primary" disabled={busy} onClick={prepare}>继续到独立确认</button></section>}
    {operation && <OperationConfirm key={operation.id} action={operation} sessionId={sessionId} title={operation.type === 'recurring_cancel' ? '确认取消剩余期次' : operation.type === 'recurring_transfer' ? '确认周期转账授权' : '确认批量转账授权'} onDone={completed}><Authorization operation={operation} /></OperationConfirm>}
    {!current && <p className="advanced-transfer__muted" role="status">正在读取已保存计划…</p>}{current?.error && <p className="advanced-transfer__error" role="alert">{current.error}<button type="button" onClick={refresh}>重试</button></p>}
    {data && <><section className="advanced-transfer__section"><h3>周期计划与每期回执</h3>{!data.recurring.plans.length && <p className="advanced-transfer__muted">暂无已确认的周期计划。</p>}{data.recurring.plans.map((plan) => <details className="advanced-transfer__history" key={plan.id} open={focusId === plan.id}><summary><strong>{plan.recipient} · 每笔 {money(plan.amount_yuan)} · {plan.count} 期</strong><span>{plan.status === 'cancelled' ? '剩余期次已取消' : labels[plan.status]}</span></summary><div className="advanced-transfer__authorization"><RecurringFacts details={plan} /><p>计划编号 {plan.id} · 创建于 {formatBankTime(plan.created_at)}（实际时间）</p></div>{plan.status === 'active' && plan.occurrences.some((item) => item.status === 'pending') && <button type="button" className="advanced-transfer__quiet" disabled={busy} onClick={() => cancel(plan.id)}>预览取消剩余期次</button>}</details>)}</section><section className="advanced-transfer__section"><h3>批量转账回执</h3>{!data.batch.batches.length && <p className="advanced-transfer__muted">暂无已执行的批量转账。</p>}{data.batch.batches.map((batch) => <details className="advanced-transfer__history" key={batch.id} open={focusId === batch.id}><summary><strong>{batch.count} 笔 · {money(batch.total_yuan)}</strong><span>{labels[batch.status]}</span></summary><div className="advanced-transfer__authorization"><BatchFacts details={batch} /><p>批次编号 {batch.id} · {formatBankTime(batch.created_at)}（实际时间）</p></div></details>)}</section></>}
  </section>
}

export function AdvancedTransfersPanel(props: AdvancedTransfersPanelProps) { return <PanelContent key={props.sessionId} {...props} /> }
