import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import type { BillTransaction } from './BillVisuals'
import { fetchInsight, type InsightChange, type InsightReply, type InsightReport } from './insightApi'
import './InsightPanel.css'

type Props = { sessionId: string; period: string; initialQuestion?: string; onTransaction: (transaction: BillTransaction) => void }
const examples = ['上个月餐饮花了多少？', '本月为什么花得多？', '找出超过200元的消费']

function money(value: string) {
  return `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function delta(value: string) {
  return `${Number(value) > 0 ? '+' : Number(value) < 0 ? '−' : ''}${money(String(Math.abs(Number(value))))}`
}

function EvidenceList({ transactions, onTransaction }: { transactions: BillTransaction[]; onTransaction: Props['onTransaction'] }) {
  return <ul className="bill-insight__evidence">
    {transactions.map((tx) => <li key={tx.id}>
      <button type="button" onClick={() => onTransaction(tx)} aria-label={`查看${tx.posted_on}${tx.counterparty}${money(tx.amount_yuan)}交易明细`}>
        <span><strong>{tx.counterparty}</strong><small>{tx.posted_on} · {tx.category}</small></span>
        <b>{money(tx.amount_yuan)}</b><span aria-hidden="true">↗</span>
      </button>
    </li>)}
  </ul>
}

function ChangeTable({ rows, transactions, onTransaction }: { rows: InsightChange[]; transactions: BillTransaction[]; onTransaction: Props['onTransaction'] }) {
  if (!rows.length) return <p className="bill-insight__muted">比较期间没有可对照的支出记录。</p>
  return <div className="bill-insight__changes">
    <div className="bill-insight__change bill-insight__change--head" aria-hidden="true"><span>项目</span><span>本期</span><span>对比期</span><span>变化</span></div>
    {rows.map((row) => {
      const evidence = transactions.filter((tx) => row.transaction_ids.includes(tx.id))
      return <details key={row.name} className="bill-insight__change-item">
        <summary className="bill-insight__change">
          <strong>{row.name}<small>{evidence.length ? `${evidence.length} 笔依据` : '查看统计'}</small></strong>
          <span aria-label={`本期${money(row.current_yuan)}`}>{money(row.current_yuan)}</span>
          <span aria-label={`对比期${money(row.previous_yuan)}`}>{money(row.previous_yuan)}</span>
          <b aria-label={`变化${delta(row.delta_yuan)}`}>{delta(row.delta_yuan)}</b>
        </summary>
        {evidence.length ? <EvidenceList transactions={evidence} onTransaction={onTransaction} /> : <p className="bill-insight__muted">本期没有匹配交易；对比期金额见本行统计。</p>}
      </details>
    })}
  </div>
}

function ReportView({ report, onTransaction }: { report: InsightReport; onTransaction: Props['onTransaction'] }) {
  const comparison = report.comparison
  const filters = [report.filters.category, report.filters.merchant, report.filters.min_amount_yuan && `金额下限 ${money(report.filters.min_amount_yuan)}`, report.filters.max_amount_yuan && `金额上限 ${money(report.filters.max_amount_yuan)}`].filter(Boolean)
  return <div className="bill-insight__report">
    <div className="bill-insight__answer">
      <span className="bill-insight__badge">账本计算 · {report.rule_version}</span>
      <p>{report.summary}</p>
      <div className="bill-insight__total"><strong>{money(report.total_yuan)}</strong><span>{report.transaction_count} 笔支出</span></div>
      <p className="bill-insight__scope">{report.start_date} 至 {report.end_date}{filters.length > 0 && ` · ${filters.join(' · ')}`}</p>
    </div>
    {report.transaction_count === 0 && <p className="bill-insight__muted">所选期间与条件下没有支出记录，可以调整期间或查询条件。</p>}
    <details className="bill-insight__section" open>
      <summary>消费变化 <span>{delta(comparison.delta_yuan)}{comparison.delta_percent !== null && ` · ${Number(comparison.delta_percent) > 0 ? '+' : ''}${comparison.delta_percent}%`}</span></summary>
      <p className="bill-insight__scope">本期 {comparison.current_start}—{comparison.current_end}；对比 {comparison.previous_start}—{comparison.previous_end}。两期均为 {comparison.equal_days} 天。</p>
      {comparison.delta_percent === null && <p className="bill-insight__muted">对比期为零或缺少可比基数，未计算增长百分比。</p>}
      <ChangeTable rows={comparison.category_changes} transactions={comparison.transactions} onTransaction={onTransaction} />
      {comparison.merchant_changes.length > 0 && <details className="bill-insight__merchants"><summary>按商户查看变化</summary><ChangeTable rows={comparison.merchant_changes} transactions={comparison.transactions} onTransaction={onTransaction} /></details>}
    </details>
    <details className="bill-insight__section" open={report.alerts.length > 0}>
      <summary>待复核线索 <span>{report.alerts.length} 项</span></summary>
      {report.alerts.length === 0 ? <p className="bill-insight__muted">现有记录未触发复核规则；这不代表所有交易都已核实。</p> : <>
        <p className="bill-insight__muted">以下为规则识别的线索，请结合明细核对。</p>
        {report.alerts.map((alert) => <details className="bill-insight__alert" key={alert.id}>
          <summary>{alert.title}<span>查看 {alert.evidence.length} 笔依据</span></summary>
          <p>{alert.reason}</p><EvidenceList transactions={alert.evidence} onTransaction={onTransaction} />
        </details>)}
      </>}
    </details>
    {report.transactions.length > 0 && <details className="bill-insight__section"><summary>查询结果明细 <span>{report.transactions.length} 笔</span></summary><EvidenceList transactions={report.transactions} onTransaction={onTransaction} /></details>}
    <details className="bill-insight__coverage"><summary>数据范围与统计说明</summary>
      <p>本次匹配记录覆盖 {report.coverage.first_date || '暂无'} 至 {report.coverage.last_date || '暂无'}，共 {report.coverage.months_with_data} 个有记录的月份。{!report.coverage.complete_history && '历史样本不完整，不据此推断全部消费习惯。'}</p>
      {report.limitations.length > 0 && <ul>{report.limitations.map((limitation) => <li key={limitation}>{limitation}</li>)}</ul>}
    </details>
  </div>
}

function InsightPanelContent({ sessionId, period, initialQuestion, onTransaction }: Props) {
  const inputId = useId()
  const controller = useRef<AbortController | null>(null)
  const [question, setQuestion] = useState(initialQuestion || '')
  const context = `${sessionId}:${period}`
  const [result, setResult] = useState<{ context: string; reply?: InsightReply; error?: string; question?: string } | null>(null)

  async function requestInsight(message?: string) {
    controller.current?.abort()
    const request = new AbortController()
    controller.current = request
    try {
      const reply = await fetchInsight(period, request.signal, message ? { sessionId, message } : undefined)
      if (!request.signal.aborted) setResult({ context, reply, question: message })
    } catch (failure) {
      if (!request.signal.aborted) setResult({ context, error: failure instanceof Error ? failure.message : '读取失败，请重试。', question: message })
    }
  }

  useEffect(() => {
    controller.current?.abort()
    const request = new AbortController()
    controller.current = request
    fetchInsight(period, request.signal, initialQuestion ? {sessionId, message: initialQuestion} : undefined).then((reply) => {
      if (!request.signal.aborted) setResult({ context, reply })
    }).catch((failure: unknown) => {
      if (!request.signal.aborted) setResult({ context, error: failure instanceof Error ? failure.message : '读取失败，请重试。' })
    })
    return () => controller.current?.abort()
  }, [context, period, initialQuestion, sessionId])

  function load(message?: string) {
    setResult(null)
    void requestInsight(message)
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (question.trim()) load(question.trim())
  }

  const current = result?.context === context ? result : null
  const loading = current === null
  const reply = current?.reply
  return <section className="bill-insight" aria-labelledby={`${inputId}-title`}>
    <div className="bill-insight__heading"><h2 id={`${inputId}-title`}>智能账单洞察</h2><span>规则解析 · 精确计算</span></div>
    <form autoComplete="off" className="bill-insight__form" onSubmit={submit}>
      <label htmlFor={inputId}>想查哪笔支出，或了解什么变化？</label>
      <div className="bill-insight__input"><input id={inputId} value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={500} placeholder={`${period}餐饮花了多少？`} /><button type="submit" disabled={!question.trim()}>查询</button></div>
    </form>
    <div className="bill-insight__examples">{examples.map((example) => <button type="button" key={example} onClick={() => { setQuestion(example); void load(example) }}>{example}</button>)}</div>
    <p className="bill-insight__hint">未指定期间时按“{period}”查询。金额由账本计算，当前使用规则解析。</p>
    <div aria-live="polite" aria-busy={loading}>
      {loading && <p className="bill-insight__loading" role="status">正在核对交易与比较期间…</p>}
      {current?.error && <div className="bill-insight__error" role="alert"><span>{current.error}</span><button type="button" onClick={() => load(current.question)}>重试</button></div>}
      {!loading && reply?.status === 'needs_clarification' && <p className="bill-insight__clarification">{reply.clarification || reply.message}</p>}
      {!loading && reply?.report && <ReportView report={reply.report} onTransaction={onTransaction} />}
    </div>
  </section>
}

export function InsightPanel(props: Props) {
  return <InsightPanelContent key={`${props.sessionId}:${props.period}:${props.initialQuestion || ''}`} {...props} />
}
