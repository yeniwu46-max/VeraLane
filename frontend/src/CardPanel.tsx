import { useEffect, useRef, useState, type FormEvent } from 'react'
import { OperationConfirm } from './OperationConfirm'
import { requestJson, type Operation } from './bankingApi'
import { formatBankTime } from './bankTime'
import './CardPanel.css'

type Card = { id: string; name: string; last4: string; status: 'active' | 'locked' | 'lost'; online_enabled: boolean; payment_limit_yuan: string; credit_limit_yuan: string; version: number }
type Application = { id: string; kind: 'new_card' | 'credit_increase'; card_id: string | null; status: string; decision: string | null; created_at: string; details: { product_id?: string; applicant?: string; income_yuan?: string; amount_yuan?: string; name?: string; last4?: string } }
type Snapshot = { cards: Card[]; applications: Application[]; products: { id: string; name: string; condition: string; fee_yuan: string }[]; notice: string }
type Interpretation = { status: string; message: string; choices?: Card[]; pending_action?: Operation }
export type CardPanelProps = { sessionId: string; onChanged: () => void; initialMessage?: string }
const statusLabels = { active: '正常', locked: '临时锁定', lost: '已挂失' }
const applicationLabels: Record<string, string> = { issued: '已发放模拟卡片', submitted: '已提交，待审批', manual_review: '待模拟人工审核', approved_pending_issue: '审批通过，待发卡', declined: '未通过模拟审批' }
const operationLabels: Record<string, string> = { lock: '临时锁卡', unlock: '解锁卡片', online_off: '关闭线上支付', online_on: '开启线上支付', report_loss: '正式挂失', payment_limit: '调低日支付限额', credit_increase: '提交授信提额申请' }
const money = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function CardAuthorization({ operation }: { operation: Operation }) {
  const d = operation.details
  const application = operation.type === 'card_application'
  const payment = operation.type === 'card_payment'
  return <div className="card-manager__authorization"><dl>
    <div><dt>操作</dt><dd>{application ? '提交新卡申请' : payment ? '模拟刷卡扣款' : operationLabels[String(d.operation)]}</dd></div>
    {d.name !== undefined && <div><dt>卡片</dt><dd>{String(d.name)} · 尾号 {String(d.last4)}</dd></div>}
    {d.amount_yuan !== undefined && <div><dt>{payment ? '扣款金额' : d.operation === 'credit_increase' ? '申请授信额度' : '新日支付限额'}</dt><dd>{money(String(d.amount_yuan))}</dd></div>}
    {payment && <><div><dt>支付通道</dt><dd>{d.channel === 'online' ? '线上支付' : 'POS 刷卡'}</dd></div><div><dt>虚构商户</dt><dd>{String(d.merchant)}</dd></div></>}
    {application && <><div><dt>申请卡种</dt><dd>{d.product_id === 'credit' ? 'Vera 青年信用卡' : 'Vera 日常借记卡'}</dd></div><div><dt>虚构申请人</dt><dd>{String(d.applicant)}</dd></div><div><dt>虚构月收入</dt><dd>{money(String(d.income_yuan))}</dd></div></>}
  </dl><p>{payment ? '确认后扣减模拟账户的可用现金。授信额度不会作为本次刷卡资金。' : application || d.operation === 'credit_increase' ? '这里只提交申请；审批、额度变化和发卡状态独立展示。' : d.operation === 'report_loss' ? '正式挂失后不能普通解锁恢复，需要申请新卡。' : '控制仅作用于模拟刷卡通道；账户转账和账户代扣保留各自授权。'}</p><p>有效至 {formatBankTime(operation.expires_at)}（实际时间）。</p></div>
}

function CardPanelContent({ sessionId, onChanged, initialMessage }: CardPanelProps) {
  const [revision, setRevision] = useState(0)
  const key = `${sessionId}:${revision}`
  const [loaded, setLoaded] = useState<{ key: string; data?: Snapshot; error?: string } | null>(null)
  const current = loaded?.key === key ? loaded : null
  const data = current?.data
  const [selectedId, setSelectedId] = useState('')
  const selected = data?.cards.find((card) => card.id === selectedId) || data?.cards[0]
  const [question, setQuestion] = useState(initialMessage || '')
  const [interpretation, setInterpretation] = useState<Interpretation | null>(null)
  const [change, setChange] = useState('lock')
  const [limit, setLimit] = useState('')
  const [productId, setProductId] = useState('debit')
  const [applicant, setApplicant] = useState('')
  const [income, setIncome] = useState('')
  const [paymentAmount, setPaymentAmount] = useState('')
  const [merchant, setMerchant] = useState('演示商店')
  const [channel, setChannel] = useState('online')
  const [operation, setOperation] = useState<Operation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const effectiveChange = change === 'lock' && selected?.status === 'locked' ? 'unlock' : change === 'unlock' && selected?.status === 'active' ? 'lock' : change
  const writes = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    const controller = new AbortController()
    requestJson<Snapshot>(`/api/cards?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal).then((value) => { if (!controller.signal.aborted) setLoaded({ key, data: value }) }).catch((cause: unknown) => { if (!controller.signal.aborted) setLoaded({ key, error: cause instanceof Error ? cause.message : '卡片读取失败。' }) })
    return () => controller.abort()
  }, [sessionId, key])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; writes.current?.abort() } }, [])
  function dirty() { setOperation(null) }
  function refresh() { dirty(); setRevision((value) => value + 1) }
  function selectCard(id: string) { setSelectedId(id); setChange(data?.cards.find((card) => card.id === id)?.status === 'locked' ? 'unlock' : 'lock'); setLimit(''); dirty() }
  async function mutate<T>(path: string, body: unknown, done: (value: T) => void) {
    if (writes.current) return
    const controller = new AbortController(); writes.current = controller
    setBusy(true); setError(''); setNotice(''); dirty()
    try { const value = await requestJson<T>(path, body, controller.signal); if (!controller.signal.aborted) done(value) }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '请求未完成。') }
    finally { if (mounted.current && writes.current === controller) { writes.current = null; setBusy(false) } }
  }
  function interpret(event: FormEvent) { event.preventDefault(); setInterpretation(null); void mutate<Interpretation>('/api/cards/interpret', { session_id: sessionId, message: question.trim() }, (value) => { setInterpretation(value); if (value.pending_action) setOperation(value.pending_action) }) }
  function prepareChange(event: FormEvent) { event.preventDefault(); if (selected) void mutate<{ pending_action: Operation }>(`/api/cards/${selected.id}/prepare`, { session_id: sessionId, operation: effectiveChange, ...(effectiveChange === 'payment_limit' || effectiveChange === 'credit_increase' ? { amount_yuan: limit.trim() } : {}) }, (value) => setOperation(value.pending_action)) }
  function apply(event: FormEvent) { event.preventDefault(); void mutate<{ pending_action: Operation }>('/api/cards/applications/prepare', { session_id: sessionId, product_id: productId, applicant: applicant.trim(), income_yuan: income.trim() }, (value) => setOperation(value.pending_action)) }
  function pay(event: FormEvent) { event.preventDefault(); if (selected) void mutate<{ pending_action: Operation }>(`/api/cards/${selected.id}/payment/prepare`, { session_id: sessionId, amount_yuan: paymentAmount.trim(), channel, merchant: merchant.trim() }, (value) => setOperation(value.pending_action)) }
  function issue(id: string) { void mutate<{message:string}>(`/api/cards/applications/${id}/simulate-issue`,{session_id:sessionId},value=>{setNotice(value.message);refresh();onChanged()}) }
  function review(id: string) { void mutate<{ message: string }>(`/api/cards/applications/${id}/simulate-review`, { session_id: sessionId }, (value) => { setNotice(value.message); refresh(); onChanged() }) }
  function completed(value: Record<string, unknown>) { if (mounted.current) { setNotice(typeof value.message === 'string' ? value.message : '操作已处理，请核对卡片与申请状态。'); setInterpretation(null); refresh(); onChanged() } }
  const needsAmount = change === 'payment_limit' || change === 'credit_increase'
  return <section className="card-manager" aria-label="智能卡片管理"><header><div><h1>卡片管理</h1><p>核对尾号、控制支付通道，跟踪模拟申请与审批。</p></div><button type="button" className="card-manager__quiet" disabled={busy} onClick={refresh}>刷新记录</button></header>
    <section className="card-manager__section"><h2>一句话管理卡片</h2><form className="card-manager__form" onSubmit={interpret}><label>卡片需求<input disabled={busy} value={question} maxLength={500} onChange={(event) => { setQuestion(event.target.value); dirty() }} placeholder="锁卡6018 / 关闭线上支付9026 / 挂失6018" /></label><button type="submit" className="card-manager__primary" disabled={busy || !question.trim()}>理解需求</button></form>{interpretation && <p className="card-manager__notice" role="status">{interpretation.message}</p>}{interpretation?.choices && <div className="card-manager__choices">{interpretation.choices.map((card) => <button type="button" className="card-manager__quiet" disabled={busy} key={card.id} onClick={() => { selectCard(card.id); setQuestion((value) => `${value.replace(/\s+$/, '')} ${card.last4}`); setInterpretation(null) }}>选择 {card.name} · {card.last4}</button>)}</div>}</section>
    {!current && <p className="card-manager__muted" role="status">正在读取卡片与申请记录…</p>}{current?.error && <p className="card-manager__error" role="alert">{current.error}<button type="button" onClick={refresh}>重试</button></p>}
    {error && <p className="card-manager__error" role="alert">{error}</p>}{notice && <p className="card-manager__notice" role="status">{notice}</p>}
    {data && <><p className="card-manager__muted">{data.notice}</p><div className="card-manager__cards">{data.cards.map((card) => <button type="button" className="card-manager__card" key={card.id} disabled={busy} aria-pressed={selected?.id === card.id} onClick={() => selectCard(card.id)}><span>{Number(card.credit_limit_yuan) > 0 ? '信用卡' : '借记卡'} · {statusLabels[card.status]}</span><strong>{card.name} <b>···· {card.last4}</b></strong><small>线上支付：{card.online_enabled ? '开启' : '关闭'} · 日支付限额 {money(card.payment_limit_yuan)}</small><small>授信额度 {money(card.credit_limit_yuan)} · 控制版本 v{card.version}</small></button>)}</div>
      {selected && <section className="card-manager__section"><h2>调整 {selected.name} · {selected.last4}</h2>{selected.status === 'lost' ? <p className="card-manager__muted">此卡已挂失，不能普通解锁恢复。可在下方提交新卡申请。</p> : <form className="card-manager__form" onSubmit={prepareChange}><label>操作<select value={effectiveChange} disabled={busy} onChange={(event) => { setChange(event.target.value); setLimit(''); dirty() }}><option value="lock" disabled={selected.status !== 'active'}>临时锁卡</option><option value="unlock" disabled={selected.status !== 'locked'}>解锁卡片</option><option value="online_off">关闭线上支付</option><option value="online_on">开启线上支付</option><option value="payment_limit">调低日支付限额</option><option value="credit_increase" disabled={Number(selected.credit_limit_yuan) === 0}>提交授信提额申请</option><option value="report_loss">正式挂失（强验证）</option></select></label>{needsAmount && <label>{change === 'payment_limit' ? '新的日支付限额（元）' : '申请授信额度（元）'}<input disabled={busy} inputMode="decimal" value={limit} onChange={(event) => { setLimit(event.target.value); dirty() }} /></label>}<button type="submit" className="card-manager__primary" disabled={busy || needsAmount && !limit.trim()}>预览操作</button></form>}<p className="card-manager__muted">日支付限额约束该卡累计刷卡金额；授信提额只生成申请，授信额度不作为本原型可花费现金。</p></section>}
      <details className="card-manager__section"><summary>申请新卡 <span>申请、审批与发卡分开</span></summary><form className="card-manager__form" onSubmit={apply}><label>卡产品<select disabled={busy} value={productId} onChange={(event) => { setProductId(event.target.value); dirty() }}>{data.products.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>虚构申请人<input disabled={busy} value={applicant} maxLength={60} onChange={(event) => { setApplicant(event.target.value); dirty() }} placeholder="请填写演示姓名" /></label><label>虚构月收入（元）<input disabled={busy} value={income} inputMode="decimal" onChange={(event) => { setIncome(event.target.value); dirty() }} /></label><button type="submit" className="card-manager__primary" disabled={busy || !applicant.trim() || !income.trim()}>预览申请</button></form><p className="card-manager__muted">{data.products.find((item) => item.id === productId)?.condition}。请使用虚构资料；此流程不申请真实银行卡。</p></details>
      <details className="card-manager__section" open={data.applications.some((item) => item.status === 'submitted')}><summary>申请进度 <span>{data.applications.length} 项</span></summary>{!data.applications.length && <p className="card-manager__muted">暂无申请记录。</p>}{data.applications.map((item) => <article className="card-manager__application" key={item.id}><div><strong>{item.kind === 'new_card' ? `${item.details.product_id === 'credit' ? '信用卡' : '借记卡'}申请` : `${item.details.name} · ${item.details.last4} 提额申请`}</strong><span>{applicationLabels[item.status] || item.status}</span></div>{item.details.amount_yuan && <p>申请授信 {money(item.details.amount_yuan)}</p>}{item.decision && <p>{item.decision}</p>}<small>{formatBankTime(item.created_at)}（实际时间） · 申请编号 {item.id}</small>{item.status === 'approved_pending_issue' && <button className="card-manager__quiet" disabled={busy} onClick={()=>issue(item.id)}>模拟发卡（不产生真实账户）</button>}{item.status === 'submitted' && <details className="card-manager__demo"><summary>演示操作：模拟审批</summary><p>按固定规则更新此申请状态，不发放真实卡片或授信。</p><button type="button" className="card-manager__quiet" disabled={busy} onClick={() => review(item.id)}>运行此申请的模拟审批</button></details>}</article>)}</details>
      {selected && <details className="card-manager__section"><summary>验证卡片控制 <span>模拟刷卡测试</span></summary><p className="card-manager__muted">使用当前选择的 {selected.name}（尾号 {selected.last4}）。锁卡、挂失、线上开关和限额均由服务端实际检查；通过后仍须确认才扣款。</p><form className="card-manager__form" onSubmit={pay}><label>支付通道<select disabled={busy} value={channel} onChange={(event) => { setChannel(event.target.value); dirty() }}><option value="online">线上支付</option><option value="pos">POS 刷卡</option></select></label><label>虚构商户<input disabled={busy} value={merchant} maxLength={60} onChange={(event) => { setMerchant(event.target.value); dirty() }} /></label><label>金额（元）<input disabled={busy} value={paymentAmount} inputMode="decimal" onChange={(event) => { setPaymentAmount(event.target.value); dirty() }} /></label><button type="submit" className="card-manager__primary" disabled={busy || !merchant.trim() || !paymentAmount.trim()}>检查并预览刷卡</button></form></details>}
    </>}
    {operation && <OperationConfirm key={operation.id} action={operation} sessionId={sessionId} title="核对卡片操作" onDone={completed}><CardAuthorization operation={operation} /></OperationConfirm>}
  </section>
}

export function CardPanel(props: CardPanelProps) { return <CardPanelContent key={props.sessionId} {...props} /> }
