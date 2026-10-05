import { useEffect, useRef, useState, type FormEvent } from 'react'
import { OperationConfirm } from './OperationConfirm'
import { requestJson, type Operation } from './bankingApi'
import { formatBankTime } from './bankTime'
import './LifePanel.css'

type Product = { id: string; kind: 'flowers' | 'cake'; name: string; merchant: string; version: number; price_yuan: string; fee_yuan: string }
type LifeDraft = { goal: string; birthday: string; budget_yuan: string; recipient_label: string; delivery_note: string }
type ParsedDraft = { [Field in keyof LifeDraft]: string | null }
type BirthdayDetails = LifeDraft & { total_yuan: string; remaining_yuan: string; fee_yuan: string; products: Product[]; order_at: string; order_expires_at: string; delivery_at: string; delivery_expires_at: string; steps: string[]; notice: string }
type LifeOrder = { id: string; task_id: string; product: Product; recipient_label: string; delivery_note: string; delivery_at: string; amount_yuan: string; refunded_yuan: string; status: 'placed' | 'delivered' | 'delivery_failed' | 'cancelled'; placed_at: string; delivered_at: string | null; transaction_id: string; refund_transaction_id: string | null; can_cancel: boolean }
type LifeTask = BirthdayDetails & { id: string; status: string; reservation_id: string; failure_reason: string | null; created_at: string; reserved_remaining_yuan: string; spent_yuan: string; refunded_yuan: string; net_spent_yuan: string; can_cancel: boolean; orders: LifeOrder[] }
type Data = { catalog: { products: Product[]; notice: string }; tasks: { tasks: LifeTask[]; notice: string } }
export type LifePanelProps = { sessionId: string; onChanged: () => void; initialMessage?: string }
const taskLabels: Record<string, string> = { scheduled: '已预留，待下单', ordered: '已下单，待送达', completed: '执行结束', cancelled: '任务已取消', failed: '执行未完成', expired: '执行窗口已过期' }
const orderLabels = { placed: '已模拟下单', delivered: '已模拟送达', delivery_failed: '送达未完成', cancelled: '订单已取消' }
const emptyDraft: LifeDraft = { goal: '', birthday: '', budget_yuan: '', recipient_label: '', delivery_note: '' }
const fieldLabels: Record<keyof LifeDraft, string> = { goal: '目标', birthday: '生日日期', budget_yuan: '预算（元）', recipient_label: '收货人', delivery_note: '虚构配送信息' }
const money = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function LifeAuthorization({ operation }: { operation: Operation }) {
  if (operation.type === 'birthday_cancel') return <div className="life-panel__authorization"><p>取消任务：{String(operation.details.goal)}</p><p>释放未花预算 {money(String(operation.details.reserved_remaining_yuan))}。</p><p>{String(operation.details.notice)}</p></div>
  if (operation.type === 'birthday_order_cancel') return <div className="life-panel__authorization"><p>取消 {String(operation.details.product_name)}，原额退回模拟账户 {money(String(operation.details.amount_yuan))}。</p><p>订单编号 {String(operation.details.order_id)}</p><p>退款在确认后单独记账；已送达订单不能取消。</p></div>
  const d = operation.details as BirthdayDetails
  return <div className="life-panel__authorization"><dl><div><dt>目标与生日</dt><dd>{d.goal} · {d.birthday}</dd></div><div><dt>收货人</dt><dd>{d.recipient_label}</dd></div><div><dt>虚构配送信息</dt><dd>{d.delivery_note}</dd></div><div><dt>确认后预留预算</dt><dd>{money(d.budget_yuan)}</dd></div><div><dt>到期支出 / 费用</dt><dd>{money(d.total_yuan)} / {money(d.fee_yuan)}</dd></div><div><dt>下单后释放余款</dt><dd>{money(d.remaining_yuan)}</dd></div></dl><ul>{d.products.map((product) => <li key={product.id}>{product.name} · {product.merchant} · {money(product.price_yuan)} · 商品 v{product.version}</li>)}</ul><p>下单窗口：{formatBankTime(d.order_at)} 至 {formatBankTime(d.order_expires_at)}（演示时间）。</p><p>送达窗口：{formatBankTime(d.delivery_at)} 至 {formatBankTime(d.delivery_expires_at)}（演示时间）。</p><p>授权按固定商品、价格和对象执行；商品变化或错过窗口会停止。预留降低可用余额，确认建任务时不扣账面余额。</p><p>当前确认有效至 {formatBankTime(operation.expires_at)}（实际时间）。</p></div>
}

function TaskDialog({ task, busy, error, onPrepare, onClose }: { task: LifeTask; busy: boolean; error: string; onPrepare: (path: string) => void; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => { dialog.current?.showModal() }, [])
  return <dialog ref={dialog} className="transaction-dialog life-panel__dialog" aria-labelledby="life-task-title" onClose={onClose}><div className="transaction-dialog__head"><div><small>生日任务 · {taskLabels[task.status] || task.status}</small><h2 id="life-task-title">{task.goal}</h2></div><button type="button" onClick={() => dialog.current?.close()} aria-label="关闭生日任务明细">×</button></div>
    <div className="life-panel__metrics"><div><span>总预算</span><strong>{money(task.budget_yuan)}</strong></div><div><span>仍预留</span><strong>{money(task.reserved_remaining_yuan)}</strong></div><div><span>已支出</span><strong>{money(task.spent_yuan)}</strong></div><div><span>已退款</span><strong>{money(task.refunded_yuan)}</strong></div><div><span>净支出</span><strong>{money(task.net_spent_yuan)}</strong></div></div>
    <dl><div><dt>生日</dt><dd>{task.birthday}</dd></div><div><dt>收货人</dt><dd>{task.recipient_label}</dd></div><div><dt>虚构配送信息</dt><dd>{task.delivery_note}</dd></div><div><dt>下单窗口</dt><dd>{formatBankTime(task.order_at)}<small>至 {formatBankTime(task.order_expires_at)}</small></dd></div><div><dt>送达窗口</dt><dd>{formatBankTime(task.delivery_at)}<small>至 {formatBankTime(task.delivery_expires_at)}</small></dd></div><div><dt>计划商品</dt><dd>{task.products.map((product) => `${product.name} ${money(product.price_yuan)}`).join('；')}</dd></div></dl>
    {task.failure_reason && <p className="life-panel__error">{task.failure_reason}</p>}
    {error && <p className="life-panel__error" role="alert">{error}</p>}
    <section className="life-panel__orders"><h3>订单与资金回执</h3>{!task.orders.length && <p className="life-panel__muted">尚未生成采购订单，当前没有商品扣款。</p>}{task.orders.map((order) => <article key={order.id}><div><strong>{order.product.name}</strong><span>{orderLabels[order.status]}</span></div><p>{order.product.merchant} · 支付 {money(order.amount_yuan)} · 已退款 {money(order.refunded_yuan)}</p><p>下单 {formatBankTime(order.placed_at)}{order.delivered_at && ` · 模拟送达 ${formatBankTime(order.delivered_at)}`}</p><small>订单编号 {order.id}<br />原扣款 {order.transaction_id}{order.refund_transaction_id && <><br />退款流水 {order.refund_transaction_id}</>}</small>{order.can_cancel && <button type="button" className="life-panel__quiet" disabled={busy} onClick={() => onPrepare(`/api/life/orders/${order.id}/cancel/prepare`)}>预览取消此订单并退款</button>}</article>)}</section>
    {task.can_cancel && <button type="button" className="life-panel__quiet" disabled={busy} onClick={() => onPrepare(`/api/life/tasks/${task.id}/cancel/prepare`)}>预览取消任务并释放未花预算</button>}
    <p className="life-panel__muted">取消任务不撤销已下单商品。每笔订单的取消退款需单独确认；已送达商品不可退款。</p><p className="life-panel__foot">所有下单与送达日期使用演示时间。任务编号 {task.id} · 创建于 {formatBankTime(task.created_at)}（实际时间）。</p>
  </dialog>
}

function LifePanelContent({ sessionId, onChanged, initialMessage }: LifePanelProps) {
  const [revision, setRevision] = useState(0)
  const key = `${sessionId}:${revision}`
  const [loaded, setLoaded] = useState<{ key: string; data?: Data; error?: string } | null>(null)
  const current = loaded?.key === key ? loaded : null
  const data = current?.data
  const [draft, setDraft] = useState<LifeDraft>(emptyDraft)
  const [question, setQuestion] = useState(initialMessage || '')
  const [clarification, setClarification] = useState('')
  const [parsedDraft, setParsedDraft] = useState<ParsedDraft | null>(null)
  const [needsReview, setNeedsReview] = useState<string[]>([])
  const [resetNext, setResetNext] = useState(true)
  const [flowerId, setFlowerId] = useState('')
  const [cakeId, setCakeId] = useState('')
  const [detailId, setDetailId] = useState<string | null>(null)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const writes = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    const controller = new AbortController()
    Promise.all([requestJson<Data['catalog']>('/api/life/catalog', undefined, controller.signal), requestJson<Data['tasks']>(`/api/life/tasks?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal)])
      .then(([catalog, tasks]) => { if (!controller.signal.aborted) setLoaded({ key, data: { catalog, tasks } }) })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setLoaded({ key, error: cause instanceof Error ? cause.message : '生日任务读取失败。' }) })
    return () => controller.abort()
  }, [sessionId, key])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; writes.current?.abort() } }, [])
  function refresh() { setOperation(null); setRevision((value) => value + 1) }
  function change(field: keyof LifeDraft, value: string) { setDraft((previous) => ({ ...previous, [field]: value })); setOperation(null) }
  async function mutate<T>(path: string, body: unknown, done: (value: T) => void) {
    if (writes.current) return
    const controller = new AbortController(); writes.current = controller
    setBusy(true); setError(''); setNotice(''); setOperation(null)
    try { const value = await requestJson<T>(path, body, controller.signal); if (!controller.signal.aborted) done(value) }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '请求未完成。') }
    finally { if (mounted.current && writes.current === controller) { writes.current = null; setBusy(false) } }
  }
  function interpret(event: FormEvent) {
    event.preventDefault(); setClarification('')
    void mutate<{ message: string; draft: ParsedDraft; needs_review: string[] }>('/api/life/interpret', { session_id: sessionId, message: question.trim(), reset: resetNext }, (value) => {
      setClarification(value.message); setParsedDraft(value.draft); setNeedsReview(value.needs_review); setResetNext(false)
    })
  }
  function syncDraft() {
    if (!parsedDraft) return
    setDraft((previous) => {
      const next = { ...previous }
      for (const field of Object.keys(emptyDraft) as (keyof LifeDraft)[]) if (parsedDraft[field] !== null) next[field] = parsedDraft[field]
      return next
    })
    setOperation(null); setNotice('已明确的信息已填入表单，未提供的字段保留手动填写内容。请继续核对商品与执行时间。')
  }
  function prepare(event: FormEvent) {
    event.preventDefault()
    void mutate<{ pending_action: Operation }>('/api/life/tasks/prepare', { session_id: sessionId, ...draft, product_ids: [flowerId, cakeId].filter(Boolean) }, (value) => setOperation(value.pending_action))
  }
  function cancelPreview(path: string) { void mutate<{ pending_action: Operation }>(path, { session_id: sessionId }, (value) => { setDetailId(null); setOperation(value.pending_action) }) }
  function completed(value: Record<string, unknown>) {
    if (!mounted.current) return
    const taskId = typeof value.task_id === 'string' ? value.task_id : operation?.details.task_id
    setNotice(typeof value.message === 'string' ? value.message : '操作已处理，请核对任务与订单回执。')
    refresh(); onChanged(); if (typeof taskId === 'string') setDetailId(taskId)
  }
  const detail = data?.tasks.tasks.find((task) => task.id === detailId)
  return <section className="life-panel" aria-label="生日资金与服务计划"><header><div><h2>生日计划</h2><p>预算预留 → 生日前两天下单 → 生日当天模拟送达。</p></div><button type="button" className="life-panel__quiet" disabled={busy} onClick={refresh}>刷新任务</button></header>
    <section className="life-panel__section"><form autoComplete="off" className="life-panel__form" onSubmit={interpret}><label>先说出生日目标<input disabled={busy} value={question} maxLength={500} onChange={(event) => { setQuestion(event.target.value); setOperation(null) }} placeholder="2026年11月20日是爱人生日，预留1000元买鲜花和蛋糕" /></label><button type="submit" className="life-panel__primary" disabled={busy || !question.trim()}>{resetNext ? '提取计划草稿' : '继续补充草稿'}</button>{!resetNext && <button type="button" className="life-panel__quiet" disabled={busy} onClick={() => { setResetNext(true); setQuestion(''); setParsedDraft(null); setNeedsReview([]); setClarification('') }}>开始新的草稿对话</button>}</form><p className="life-panel__muted">可继续一句话补充年份、收货人与虚构地址。解析不会预留资金、下单或替你选商品。</p>{clarification && <p className="life-panel__notice" role="status">{clarification}</p>}{parsedDraft && <div className="life-panel__parsed"><dl>{(Object.keys(emptyDraft) as (keyof LifeDraft)[]).map((field) => <div key={field}><dt>{fieldLabels[field]}</dt><dd>{parsedDraft[field] || '待补充'}</dd></div>)}</dl>{needsReview.length > 0 && <ul>{needsReview.map((line) => <li key={line}>{line}</li>)}</ul>}<button type="button" className="life-panel__quiet" disabled={busy || !Object.values(parsedDraft).some(Boolean)} onClick={syncDraft}>将已明确字段填入表单</button></div>}</section>
    {!current && <p className="life-panel__muted" role="status">正在读取模拟商品与生日任务…</p>}{current?.error && <p className="life-panel__error" role="alert">{current.error}<button type="button" onClick={refresh}>重试</button></p>}
    {error && <p className="life-panel__error" role="alert">{error}</p>}{notice && <p className="life-panel__notice" role="status">{notice}</p>}
    {data && <><section className="life-panel__section"><h3>完善生日计划</h3><form autoComplete="off" onSubmit={prepare}><div className="life-panel__form"><label>目标名称<input disabled={busy} value={draft.goal} maxLength={120} onChange={(event) => change('goal', event.target.value)} placeholder="例如：林悦生日安排" required /></label><label>生日日期（含年份）<input disabled={busy} type="date" value={draft.birthday} onChange={(event) => change('birthday', event.target.value)} required /></label><label>预留预算（元）<input disabled={busy} inputMode="decimal" value={draft.budget_yuan} onChange={(event) => change('budget_yuan', event.target.value)} required /></label><label>收货人称呼<input disabled={busy} value={draft.recipient_label} maxLength={60} onChange={(event) => change('recipient_label', event.target.value)} placeholder="由你明确填写" required /></label></div><label className="life-panel__delivery">虚构配送信息<textarea disabled={busy} value={draft.delivery_note} maxLength={200} onChange={(event) => change('delivery_note', event.target.value)} placeholder="例如：演示市示例街1号，使用虚构地址" required /></label><div className="life-panel__form">{(['flowers', 'cake'] as const).map((kind) => <label key={kind}>{kind === 'flowers' ? '鲜花' : '蛋糕'}（各最多一件）<select disabled={busy} value={kind === 'flowers' ? flowerId : cakeId} onChange={(event) => { if (kind === 'flowers') setFlowerId(event.target.value); else setCakeId(event.target.value); setOperation(null) }}><option value="">暂不选择</option>{data.catalog.products.filter((product) => product.kind === kind).map((product) => <option key={product.id} value={product.id}>{product.name} · {money(product.price_yuan)} · 费用 {money(product.fee_yuan)}</option>)}</select></label>)}</div><p className="life-panel__muted">至少选择一件商品。固定在生日前两天 09:00 下单、生日当天 09:00 模拟送达，每次窗口 10 分钟；请结合上方演示时钟选择日期。</p><button type="submit" className="life-panel__primary" disabled={busy || !flowerId && !cakeId}>核对预算与执行时间</button></form><p className="life-panel__foot">{data.catalog.notice}</p></section>
      <section className="life-panel__section"><h3>已保存任务 <span>{data.tasks.tasks.length} 项</span></h3>{!data.tasks.tasks.length && <p className="life-panel__muted">确认计划后，预算预留和每个执行阶段会保存到这里。</p>}{data.tasks.tasks.map((task) => <button type="button" className="life-panel__task" key={task.id} onClick={() => setDetailId(task.id)}><span><strong>{task.goal}</strong><small>{task.birthday} · {task.recipient_label} · 预算 {money(task.budget_yuan)}</small></span><span>{taskLabels[task.status] || task.status}<small>查看计划与订单 ↗</small></span></button>)}</section>
    </>}
    {operation && <OperationConfirm key={operation.id} action={operation} sessionId={sessionId} title={operation.type === 'birthday_task' ? '确认生日预算与执行计划' : operation.type === 'birthday_cancel' ? '确认取消任务并释放预算' : '确认取消订单与退款'} onDone={completed}><LifeAuthorization operation={operation} /></OperationConfirm>}
    {detail && <TaskDialog task={detail} busy={busy} error={error} onPrepare={cancelPreview} onClose={() => setDetailId(null)} />}
  </section>
}

export function LifePanel(props: LifePanelProps) { return <LifePanelContent key={props.sessionId} {...props} /> }
