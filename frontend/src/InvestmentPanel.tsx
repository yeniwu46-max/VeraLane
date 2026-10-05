import { useEffect, useRef, useState, type FormEvent } from 'react'
import { OperationConfirm } from './OperationConfirm'
import { requestJson, type Operation } from './bankingApi'
import { formatBankTime } from './bankTime'
import './InvestmentPanel.css'

type Assessment = { id: string; version: string; score: number; risk_level: number; assessed_on: string; expires_on: string; valid: boolean }
type Questionnaire = { version: string; questions: { id: string; title: string; options: { value: number; label: string }[] }[]; scoring: string; notice: string }
type Product = { id: string; name: string; version: number; risk_level: number; nav_yuan: string; min_purchase_yuan: string; lock_days: number; redemption_days: number; buy_fee_bps: number; redeem_fee_bps: number; status: string; matched: boolean; reasons: string[] }
type Products = { products: Product[]; assessment: Assessment | null; notice: string }
type Holding = { id: string; product_id: string; product_name: string; units: string; redeemable_units: string; nav_yuan: string; estimated_value_yuan: string; acquired_on: string; unlock_on: string }
type Order = { id: string; kind: 'buy' | 'redeem'; status: 'completed' | 'pending_settlement'; product_name: string; amount_yuan: string; fee_yuan: string; units: string; settles_on: string | null; transaction_id: string | null; created_at: string; holding_id: string }
type Portfolio = { assessment: Assessment | null; available_yuan: string; holdings: Holding[]; orders: Order[]; notice: string }
type Data = { questionnaire: Questionnaire; products: Products; portfolio: Portfolio }
type Interpretation = { status: string; message: string; pending_action?: Operation; products?: Product[]; choices?: { id: string; name?: string }[] }
type InvestmentDetails = { product: Product; assessment: Assessment | null; amount_yuan: string; gross_yuan?: string; fee_yuan: string; units: string; settles_on: string | null; rounding_policy: string; holding_id?: string; horizon_days?: number; liquidity_days?: number }
export type InvestmentPanelProps = { sessionId: string; onChanged: () => void; initialMessage?: string }
const money = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
const fee = (value: number) => `${(value / 100).toFixed(2)}%`

function AuthorizationDetails({ operation }: { operation: Operation }) {
  const details = operation.details as InvestmentDetails
  const buy = operation.type === 'investment_buy'
  return <div className="invest-panel__authorization"><dl>
    <div><dt>虚构产品</dt><dd>{details.product.name} · v{details.product.version} · 风险 {details.product.risk_level} 级</dd></div>
    <div><dt>演示净值</dt><dd>¥{details.product.nav_yuan} / 份</dd></div>
    <div><dt>{buy ? '申购总扣款（含费用）' : '预计到账净额'}</dt><dd>{money(details.amount_yuan)}</dd></div>
    {details.gross_yuan && <div><dt>赎回总额</dt><dd>{money(details.gross_yuan)}</dd></div>}
    <div><dt>手续费</dt><dd>{money(details.fee_yuan)}</dd></div><div><dt>{buy ? '获得份额' : '赎回份额'}</dt><dd>{details.units} 份</dd></div>
    <div><dt>{buy ? '持仓锁定期' : '预计到账日'}</dt><dd>{buy ? `${details.product.lock_days} 天` : details.settles_on}</dd></div>
    {buy && <div><dt>用户选择的资金需求</dt><dd>持有 {details.horizon_days} 天 · 可接受 {details.liquidity_days} 个演示工作日到账</dd></div>}
    {details.holding_id && <div><dt>持仓批次</dt><dd>{details.holding_id}</dd></div>}
  </dl><p>{details.rounding_policy}</p><p>授权到期 {formatBankTime(operation.expires_at)}（实际时间）。执行时重新检查产品、可用资金和持仓。</p></div>
}

function InvestmentPanelContent({ sessionId, onChanged, initialMessage }: InvestmentPanelProps) {
  const [revision, setRevision] = useState(0)
  const [criteria, setCriteria] = useState({ horizon: 30, liquidity: 2 })
  const [horizon, setHorizon] = useState('30')
  const [liquidity, setLiquidity] = useState('2')
  const loadKey = `${sessionId}:${criteria.horizon}:${criteria.liquidity}:${revision}`
  const [loaded, setLoaded] = useState<{ key: string; data?: Data; error?: string } | null>(null)
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [question, setQuestion] = useState(initialMessage || '')
  const [interpretation, setInterpretation] = useState<Interpretation | null>(null)
  const [productId, setProductId] = useState('')
  const [amount, setAmount] = useState('')
  const [redeemUnits, setRedeemUnits] = useState<Record<string, string>>({})
  const [operation, setOperation] = useState<Operation | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const writes = useRef<AbortController | null>(null)
  const mounted = useRef(true)
  const current = loaded?.key === loadKey ? loaded : null
  const data = current?.data
  const assessment = data?.portfolio.assessment
  const product = data?.products.products.find((item) => item.id === productId)
  const criteriaDirty = horizon !== String(criteria.horizon) || liquidity !== String(criteria.liquidity)
  useEffect(() => {
    const controller = new AbortController()
    const suffix = `session_id=${encodeURIComponent(sessionId)}`
    Promise.all([requestJson<Questionnaire>('/api/investments/questions', undefined, controller.signal), requestJson<Products>(`/api/investments/products?${suffix}&horizon_days=${criteria.horizon}&liquidity_days=${criteria.liquidity}`, undefined, controller.signal), requestJson<Portfolio>(`/api/investments/portfolio?${suffix}`, undefined, controller.signal)])
      .then(([questionnaire, products, portfolio]) => { if (!controller.signal.aborted) setLoaded({ key: loadKey, data: { questionnaire, products, portfolio } }) })
      .catch((cause: unknown) => { if (!controller.signal.aborted) setLoaded({ key: loadKey, error: cause instanceof Error ? cause.message : '理财记录读取失败。' }) })
    return () => controller.abort()
  }, [sessionId, criteria.horizon, criteria.liquidity, loadKey])
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; writes.current?.abort() } }, [])

  function refresh() { setOperation(null); setRevision((value) => value + 1) }
  async function mutate<T>(path: string, body: unknown, done: (value: T) => void) {
    if (writes.current) return
    const controller = new AbortController(); writes.current = controller
    setBusy(true); setError(''); setNotice(''); setOperation(null)
    try { const value = await requestJson<T>(path, body, controller.signal); if (!controller.signal.aborted) done(value) }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '请求未完成，请核对输入。') }
    finally { if (mounted.current && writes.current === controller) { writes.current = null; setBusy(false) } }
  }
  function assess(event: FormEvent) {
    event.preventDefault()
    if (!data || data.questionnaire.questions.some((item) => !answers[item.id])) return
    void mutate<{ message: string }>('/api/investments/assessment', { session_id: sessionId, answers: data.questionnaire.questions.map((item) => Number(answers[item.id])) }, (value) => { setNotice(value.message); refresh() })
  }
  function filter(event: FormEvent) {
    event.preventDefault()
    if (!/^\d+$/.test(horizon) || Number(horizon) > 3650 || !/^\d+$/.test(liquidity) || Number(liquidity) > 365) { setError('持有期限须为 0–3650 天，赎回到账要求须为 0–365 个演示工作日。'); return }
    setError(''); setOperation(null); setCriteria({ horizon: Number(horizon), liquidity: Number(liquidity) }); setRevision((value) => value + 1)
  }
  function interpret(event: FormEvent) {
    event.preventDefault()
    if (!question.trim()) return
    setInterpretation(null)
    void mutate<Interpretation>('/api/investments/interpret', { session_id: sessionId, message: question.trim() }, (value) => {
      setInterpretation(value)
      if (value.products || value.pending_action?.type === 'investment_buy') { setHorizon('30'); setLiquidity('2'); setCriteria({ horizon: 30, liquidity: 2 }) }
      if (value.products) setRevision((current) => current + 1)
      if (value.pending_action) setOperation(value.pending_action)
    })
  }
  function buy(event: FormEvent) {
    event.preventDefault()
    if (!product || !amount.trim() || criteriaDirty) return
    void mutate<{ pending_action: Operation }>('/api/investments/buy/prepare', { session_id: sessionId, product_id: product.id, amount_yuan: amount.trim(), horizon_days: criteria.horizon, liquidity_days: criteria.liquidity }, (value) => setOperation(value.pending_action))
  }
  function redeem(event: FormEvent, holdingId: string) {
    event.preventDefault()
    if (!redeemUnits[holdingId]?.trim()) return
    void mutate<{ pending_action: Operation }>('/api/investments/redeem/prepare', { session_id: sessionId, holding_id: holdingId, units: redeemUnits[holdingId].trim() }, (value) => setOperation(value.pending_action))
  }
  function completed(value: Record<string, unknown>) {
    if (!mounted.current) return
    setNotice(typeof value.message === 'string' ? value.message : '操作已处理，请核对持仓与订单状态。')
    setAmount(''); setRedeemUnits({}); setInterpretation(null); refresh(); onChanged()
  }

  return <section className="invest-panel" aria-label="模拟理财助手">
    <header className="invest-panel__heading"><div><h1>理财助手</h1><p>虚构产品目录 · 规则测评与适当性核对 · 独立模拟验证</p></div><button type="button" className="invest-panel__quiet" disabled={busy} onClick={refresh}>刷新记录</button></header>
    <section className="invest-panel__section"><h2>一句话了解或办理</h2><form autoComplete="off" className="invest-panel__form" onSubmit={interpret}><label>理财需求<input disabled={busy} value={question} onChange={(event) => { setQuestion(event.target.value); setOperation(null) }} maxLength={500} placeholder="比较理财产品 / 申购稳享100元 / 赎回稳享10份" /></label><button type="submit" className="invest-panel__primary" disabled={busy || !question.trim()}>理解需求</button></form><p className="invest-panel__muted">快捷语句默认持有 30 天、可接受 2 个演示工作日到账。需要其他期限时，使用下方筛选与申购表单。</p>{interpretation && <p className="invest-panel__notice" role="status">{interpretation.message}{interpretation.choices?.some((item) => item.name) && <span> 可选产品：{interpretation.choices.filter((item) => item.name).map((item) => item.name).join('、')}。</span>}</p>}</section>
    {!current && <p className="invest-panel__muted" role="status">正在读取产品、测评与持仓…</p>}{current?.error && <p className="invest-panel__error" role="alert">{current.error}<button type="button" onClick={refresh}>重试</button></p>}
    {error && <p className="invest-panel__error" role="alert">{error}</p>}{notice && <p className="invest-panel__notice" role="status">{notice}</p>}
    {data && <>
      <details className="invest-panel__section" open={!assessment?.valid}><summary>风险测评 <span>{assessment ? `${assessment.risk_level} 级 · ${assessment.valid ? '有效' : '已过期'}` : '尚未完成'}</span></summary>
        {assessment && <div className="invest-panel__risk-result"><strong>风险承受 {assessment.risk_level} 级</strong><span>得分 {assessment.score} · 测评日 {assessment.assessed_on} · 到期日 {assessment.expires_on}</span></div>}
        <form autoComplete="off" onSubmit={assess}>{data.questionnaire.questions.map((item, index) => <label className="invest-panel__question" key={item.id}><span>{index + 1}. {item.title}</span><select value={answers[item.id] || ''} disabled={busy} onChange={(event) => { setAnswers((previous) => ({ ...previous, [item.id]: event.target.value })); setOperation(null) }}><option value="">请选择</option>{item.options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select></label>)}<button type="submit" className="invest-panel__primary" disabled={busy || data.questionnaire.questions.some((item) => !answers[item.id])}>提交本次测评</button></form>
        <p className="invest-panel__source">规则版本 {data.questionnaire.version} · {data.questionnaire.scoring}</p>
      </details>
      <section className="invest-panel__section"><h2>产品匹配与对比</h2><form autoComplete="off" className="invest-panel__form" onSubmit={filter}><label>计划持有天数<input disabled={busy} inputMode="numeric" value={horizon} onChange={(event) => { setHorizon(event.target.value); setOperation(null) }} /></label><label>可接受赎回到账工作日<input disabled={busy} inputMode="numeric" value={liquidity} onChange={(event) => { setLiquidity(event.target.value); setOperation(null) }} /></label><button type="submit" className="invest-panel__primary" disabled={busy}>应用筛选</button></form>
        {criteriaDirty && <p className="invest-panel__notice">期限或到账要求已修改，请先应用筛选，再预览申购。</p>}<p className="invest-panel__muted">当前匹配条件：持有 {criteria.horizon} 天，接受 {criteria.liquidity} 个演示工作日到账。到账要求不包含产品锁定期；演示工作日只排除周末。</p>
        <div className="invest-panel__table-wrap" role="region" aria-label="虚构产品对比，可横向滚动" tabIndex={0}><table className="invest-panel__table"><thead><tr><th scope="col">产品 / 匹配结果</th><th scope="col">风险</th><th scope="col">净值 / 起购</th><th scope="col">锁定 / 赎回到账</th><th scope="col">申购 / 赎回费率</th><th scope="col">操作</th></tr></thead><tbody>{data.products.products.map((item) => <tr key={item.id}><td><strong>{item.name} <small>产品 v{item.version} · {item.status === 'active' ? '可申购' : '暂停申购'}</small></strong><span className="invest-panel__status">{item.matched ? '符合当前条件' : '暂不匹配'}</span><small>{item.reasons.join('；')}</small></td><td>{item.risk_level} 级</td><td><b>¥{item.nav_yuan}</b><small>{money(item.min_purchase_yuan)} 起购</small></td><td>{item.lock_days} 天锁定<small>{item.redemption_days} 个工作日到账</small></td><td>{fee(item.buy_fee_bps)}<small>{fee(item.redeem_fee_bps)}</small></td><td><button type="button" className="invest-panel__quiet" disabled={busy || !item.matched} onClick={() => { setProductId(item.id); setAmount(''); setOperation(null) }}>选择申购</button></td></tr>)}</tbody></table></div>
        {product && <div className="invest-panel__selection"><h3>模拟申购 {product.name}</h3><p>起购 {money(product.min_purchase_yuan)}；当前模拟可用资金 {money(data.portfolio.available_yuan)}。</p><form autoComplete="off" className="invest-panel__form" onSubmit={buy}><label>申购总金额（元，包含手续费）<input disabled={busy} inputMode="decimal" value={amount} onChange={(event) => { setAmount(event.target.value); setOperation(null) }} placeholder={product.min_purchase_yuan} /></label><button type="submit" className="invest-panel__primary" disabled={busy || criteriaDirty || !amount.trim() || !product.matched}>预览申购</button></form></div>}
        <p className="invest-panel__source">{data.products.notice}</p>
      </section>
      <section className="invest-panel__section"><h2>持仓与赎回</h2><p className="invest-panel__muted">可用资金 {money(data.portfolio.available_yuan)}。待到账赎回款未计入；持仓估值不等于可用现金。</p>{!data.portfolio.holdings.length && <p className="invest-panel__muted">尚无持仓。申购确认后按批次记录份额。</p>}{data.portfolio.holdings.map((holding) => <article className="invest-panel__position" key={holding.id}><div><strong>{holding.product_name}</strong><b>{holding.units} 份 · 估值 {money(holding.estimated_value_yuan)}</b></div><p>买入日 {holding.acquired_on} · 解锁日 {holding.unlock_on} · 当前可赎 {holding.redeemable_units} 份</p><small className="invest-panel__source">批次 {holding.id}</small>{Number(holding.redeemable_units) > 0 ? <form autoComplete="off" className="invest-panel__form" onSubmit={(event) => redeem(event, holding.id)}><label>赎回份额（最多四位小数）<input disabled={busy} inputMode="decimal" value={redeemUnits[holding.id] || ''} onChange={(event) => { setRedeemUnits((previous) => ({ ...previous, [holding.id]: event.target.value })); setOperation(null) }} placeholder={`可赎 ${holding.redeemable_units} 份`} /></label><button type="submit" className="invest-panel__quiet" disabled={busy || !redeemUnits[holding.id]?.trim()}>预览赎回</button></form> : <p className="invest-panel__muted">{Number(holding.units) > 0 ? '此批持仓仍在锁定期。' : '此批持仓已无剩余份额。'}</p>}</article>)}</section>
      <details className="invest-panel__section" open={data.portfolio.orders.some((order) => order.status === 'pending_settlement')}><summary>交易订单 <span>{data.portfolio.orders.length} 笔</span></summary>{!data.portfolio.orders.length && <p className="invest-panel__muted">暂无理财订单。</p>}{data.portfolio.orders.map((order) => <article className="invest-panel__order" key={order.id}><div><strong>{order.product_name} · {order.kind === 'buy' ? '申购' : '赎回'} {order.units} 份</strong><span className="invest-panel__status">{order.status === 'pending_settlement' ? '待到账' : order.kind === 'buy' ? '已成交' : '已到账'}</span></div><p>{order.kind === 'buy' ? '总扣款（含费用）' : '到账净额'} {money(order.amount_yuan)} · 手续费 {money(order.fee_yuan)}{order.settles_on && ` · ${order.status === 'pending_settlement' ? '预计' : '约定'}到账日 ${order.settles_on}`}</p><small>{formatBankTime(order.created_at)}（实际时间） · 订单 {order.id}{order.transaction_id && <><br />交易编号 {order.transaction_id}</>}</small></article>)}</details>
    </>}
    {operation && <OperationConfirm key={operation.id} action={operation} sessionId={sessionId} title={operation.type === 'investment_buy' ? '核对模拟申购' : '核对模拟赎回'} onDone={completed}><AuthorizationDetails operation={operation} /></OperationConfirm>}
  </section>
}

export function InvestmentPanel(props: InvestmentPanelProps) {
  return <InvestmentPanelContent key={props.sessionId} {...props} />
}
