import { useEffect, useId, useState, type FormEvent } from 'react'
import { requestJson, type Operation } from './bankingApi'
import { OperationConfirm } from './OperationConfirm'
import './BillPreferencesPanel.css'

type Transaction = { id: string; posted_on: string; counterparty: string; amount_yuan: string; category: string; original_category: string; classification_reason: string | null; classification_version: number }
type Budget = { id: string; month: string; category: string | null; version: number; amount_yuan: string; spent_yuan: string; remaining_yuan: string; over_yuan: string }
type FutureItem = { id: string; label?: string; at: string; amount_yuan: string }
type Data = {
  current_month: string; period_start: string; period_end: string; transactions: Transaction[]; categories: string[]; budgets: Budget[]
  classification_history: { transaction_id: string; original_category: string; previous_category: string; category: string; reason: string; version: number }[]
  forecast: { from: string; to: string; balance_yuan: string; reserved_yuan: string; available_yuan: string; authorized_transfer_yuan: string; known_debit_yuan: string; after_authorized_yuan: string; after_known_debits_yuan: string; authorized_transfers: FutureItem[]; known_debits: FutureItem[]; pending_settlements: FutureItem[]; reservations: { id: string; purpose: string; remaining_yuan: string }[]; limitations: string[] }
  notice: string
}
type Props = { sid: string; period: string; onChanged?: () => void }

const currency = (value: string) => `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function FutureRows({ rows }: { rows: FutureItem[] }) {
  return rows.length ? <ul className="bill-preferences__future-list">{rows.map((row) => <li key={row.id}><span>{row.label || '待结算赎回'}<small>{row.at.replace('T', ' ').slice(0, 16)}</small></span><strong>{currency(row.amount_yuan)}</strong></li>)}</ul> : <p className="bill-preferences__muted">当前没有此类已保存记录。</p>
}

function Content({ sid, period, onChanged }: Props) {
  const categoryListId = useId()
  const [data, setData] = useState<Data>()
  const [transactionId, setTransactionId] = useState('')
  const [category, setCategory] = useState('')
  const [reason, setReason] = useState('')
  const [budgetId, setBudgetId] = useState<string>()
  const [budgetCategory, setBudgetCategory] = useState('')
  const [budgetAmount, setBudgetAmount] = useState('')
  const [action, setAction] = useState<Operation>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const path = `/api/bill-preferences?session_id=${encodeURIComponent(sid)}&period=${encodeURIComponent(period)}`

  useEffect(() => {
    const controller = new AbortController()
    requestJson<Data>(path, undefined, controller.signal).then(setData).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : '读取预算失败')
    })
    return () => controller.abort()
  }, [path])

  async function prepare(pathname: string, payload: Record<string, unknown>) {
    setBusy(true); setError(''); setNotice('')
    try {
      const response = await requestJson<{ pending_action: Operation }>(pathname, { session_id: sid, ...payload })
      setAction(response.pending_action)
    } catch (failure) { setError(failure instanceof Error ? failure.message : '生成确认失败') }
    finally { setBusy(false) }
  }

  async function discard() {
    if (!action) return
    setBusy(true); setError('')
    try { await requestJson(`/api/bill-preferences/actions/${action.id}/discard`, { session_id: sid }); setAction(undefined) }
    catch (failure) { setError(failure instanceof Error ? failure.message : '放弃编辑失败') }
    finally { setBusy(false) }
  }

  async function done(result: Record<string, unknown>) {
    setAction(undefined); setBudgetId(undefined); setBudgetCategory(''); setBudgetAmount(''); setTransactionId(''); setCategory(''); setReason('')
    setNotice(String(result.message || '已更新'))
    try { setData(await requestJson<Data>(path)); onChanged?.() }
    catch (failure) { setError(failure instanceof Error ? failure.message : '刷新账单失败') }
  }

  function classificationSubmit(event: FormEvent) {
    event.preventDefault()
    void prepare('/api/bill-preferences/classifications/prepare', { transaction_id: transactionId, category, reason })
  }

  function budgetSubmit(event: FormEvent) {
    event.preventDefault()
    if (data) void prepare('/api/bill-preferences/budgets/prepare', { month: data.current_month, category: budgetCategory || null, amount_yuan: budgetAmount, ...(budgetId ? { budget_id: budgetId } : {}) })
  }

  const disabled = busy || !!action
  const selected = data?.transactions.find((transaction) => transaction.id === transactionId)
  return <details className="bill-preferences">
    <summary>分类修正与预算 <span>用户确认后保存</span></summary>
    {data ? <>
      <p className="bill-preferences__muted">{data.notice}</p>
      <div className="bill-preferences__editors">
        <form onSubmit={classificationSubmit}>
          <h3>纠正一笔支出的分类</h3>
          <label>交易记录<select aria-label="需要归类的交易" value={transactionId} disabled={disabled} onChange={(event) => { const item = data.transactions.find((transaction) => transaction.id === event.target.value); setTransactionId(event.target.value); setCategory(item?.category || ''); setReason('') }}><option value="">选择当前报告中的交易</option>{data.transactions.map((transaction) => <option key={transaction.id} value={transaction.id}>{transaction.posted_on} · {transaction.counterparty} · {currency(transaction.amount_yuan)}</option>)}</select></label>
          {selected && <p className="bill-preferences__muted">原始分类：{selected.original_category}；当前统计：{selected.category}{selected.classification_reason && `；原因：${selected.classification_reason}`}</p>}
          <label>新分类<input aria-label="新统计分类" list={categoryListId} maxLength={20} value={category} disabled={disabled} onChange={(event) => setCategory(event.target.value)} /></label>
          <datalist id={categoryListId}>{data.categories.map((item) => <option key={item} value={item} />)}</datalist>
          <label>修正原因<input aria-label="归类修正原因" maxLength={200} placeholder="例如：这笔是出差交通" value={reason} disabled={disabled} onChange={(event) => setReason(event.target.value)} /></label>
          <button className="page-primary" disabled={disabled || !transactionId || !category.trim() || !reason.trim()}>核对分类修改</button>
        </form>
        <form onSubmit={budgetSubmit}>
          <h3>{data.current_month} 月度预算</h3>
          <label>消费分类<select aria-label="预算消费分类" value={budgetCategory} disabled={disabled} onChange={(event) => setBudgetCategory(event.target.value)}><option value="">全部支出</option>{data.categories.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
          <label>预算金额（元）<input aria-label="月度预算金额" inputMode="decimal" value={budgetAmount} disabled={disabled} placeholder="例如：1500.00" onChange={(event) => setBudgetAmount(event.target.value)} /></label>
          <p className="bill-preferences__muted">只对照本月已发生支出；不会把 AA 待收或预计收入当成剩余现金。</p>
          <button className="page-primary" disabled={disabled || !budgetAmount.trim()}>{budgetId ? '核对预算修改' : '核对预算'}</button>
          {budgetId && !action && <button type="button" className="page-secondary" disabled={busy} onClick={() => { setBudgetId(undefined); setBudgetCategory(''); setBudgetAmount('') }}>取消编辑</button>}
        </form>
      </div>
      {data.budgets.length > 0 && <ul className="bill-preferences__budgets">{data.budgets.map((budget) => <li key={budget.id}>
        <span><strong>{budget.month} · {budget.category || '全部支出'}</strong><small>预算 {currency(budget.amount_yuan)} · 已发生 {currency(budget.spent_yuan)}</small></span>
        <b className={Number(budget.remaining_yuan) < 0 ? 'bill-preferences__over' : ''}>{Number(budget.remaining_yuan) < 0 ? `超出 ${currency(budget.over_yuan)}` : `剩余 ${currency(budget.remaining_yuan)}`}</b>
        <button type="button" className="page-secondary" disabled={disabled || budget.month !== data.current_month} onClick={() => { setBudgetId(budget.id); setBudgetCategory(budget.category || ''); setBudgetAmount(budget.amount_yuan) }}>编辑</button>
        <button type="button" className="page-secondary" disabled={disabled} onClick={() => void prepare(`/api/bill-preferences/budgets/${budget.id}/delete/prepare`, {})}>删除</button>
      </li>)}</ul>}
      {action && <><OperationConfirm key={action.id} action={action} sessionId={sid} title={action.type === 'bill_classification' ? '确认分类修正' : action.type === 'bill_budget_delete' ? '确认删除预算' : '确认月度预算'} onDone={(result) => void done(result)}>{action.type === 'bill_classification' ? <><p>{String((action.details.transaction as Record<string, unknown>).counterparty)} · {currency(String((action.details.transaction as Record<string, unknown>).amount_yuan))}</p><p>{String(action.details.previous_category)} → <strong>{String(action.details.category)}</strong></p><p>原因：{String(action.details.reason)}</p></> : <p>{String(action.details.month)} · {String(action.details.category || '全部支出')} · {currency(String(action.details.amount_yuan))}</p>}</OperationConfirm><button type="button" className="page-secondary" disabled={busy} onClick={() => void discard()}>放弃这次编辑</button></>}
      <details className="bill-preferences__forecast"><summary>未来 30 天的资金场景 <span>分别列示已授权与估计</span></summary>
        <p className="bill-preferences__muted">{data.forecast.from.slice(0, 10)} 至 {data.forecast.to.slice(0, 10)}</p>
        <dl><div><dt>账面余额</dt><dd>{currency(data.forecast.balance_yuan)}</dd></div><div><dt>有效预留</dt><dd>{currency(data.forecast.reserved_yuan)}</dd></div><div><dt>当前可用</dt><dd>{currency(data.forecast.available_yuan)}</dd></div></dl>
        <details><summary>已确认未来转账 · {currency(data.forecast.authorized_transfer_yuan)}</summary><FutureRows rows={data.forecast.authorized_transfers} /></details>
        <details><summary>已知代扣估计 · {currency(data.forecast.known_debit_yuan)}</summary><FutureRows rows={data.forecast.known_debits} /></details>
        <details><summary>预留明细（已从可用余额扣除）</summary>{data.forecast.reservations.length ? <ul className="bill-preferences__future-list">{data.forecast.reservations.map((reservation) => <li key={reservation.id}><span>{reservation.purpose}</span><b>{currency(reservation.remaining_yuan)}</b></li>)}</ul> : <p className="bill-preferences__muted">暂无有效预留。</p>}</details>
        <details><summary>理财待到账（不计入当前可用）</summary><FutureRows rows={data.forecast.pending_settlements} /></details>
        <p>若已确认预约均执行：可用金额约 <strong>{currency(data.forecast.after_authorized_yuan)}</strong>。</p><p>若已知代扣也发生：可用金额约 <strong>{currency(data.forecast.after_known_debits_yuan)}</strong>。</p>
        <ul className="bill-preferences__limitations">{data.forecast.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>
      </details>
      {data.classification_history.length > 0 && <details className="bill-preferences__history"><summary>分类修正记录 · {data.classification_history.length} 次</summary><ul>{data.classification_history.map((entry) => <li key={`${entry.transaction_id}-${entry.version}`}>第 {entry.version} 版 · {entry.previous_category} → {entry.category}<small>原始：{entry.original_category}；{entry.reason}；交易 {entry.transaction_id}</small></li>)}</ul></details>}
    </> : !error && <p className="bill-preferences__muted">正在读取账单与预算…</p>}
    {notice && <p className="page-notice" role="status">{notice}</p>}{error && <p className="error-banner" role="alert">{error}</p>}
  </details>
}

export function BillPreferencesPanel(props: Props) { return <Content key={`${props.sid}:${props.period}`} {...props} /> }
export default BillPreferencesPanel
