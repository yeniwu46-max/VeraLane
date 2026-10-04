export type BillTransaction = {
  id: string
  posted_on: string
  category: string
  counterparty: string
  amount_yuan: string
  note: string
  original_category?: string
  classification_reason?: string
  classification_version?: number
}

export type Report = {
  period: string
  total_yuan: string
  categories: { name: string; amount_yuan: string }[]
  transaction_ids: string[]
  transactions: BillTransaction[]
  alerts: { transaction_id: string; counterparty: string; amount_yuan: string; reason: string }[]
}

export type ChartView = 'bars' | 'donut' | 'trend'

const colors = ['#6bb3ff', '#bda2ff', '#edbe80', '#78c8d8', '#e895b9', '#9fb7e5', '#d8a4a4']

function money(value: string | number): string {
  return `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function Bars({ report }: { report: Report }) {
  const max = Math.max(...report.categories.map((row) => Number(row.amount_yuan)), 1)
  return <div className="chart-bars">
    {report.categories.map((row, index) => (
      <div className="report-row" key={row.name}>
        <span>{row.name}</span>
        <div className="report-row__track"><i style={{ width: `${(Number(row.amount_yuan) / max) * 100}%`, background: colors[index % colors.length] }} /></div>
        <b>{money(row.amount_yuan)}</b>
      </div>
    ))}
  </div>
}

function Donut({ report }: { report: Report }) {
  const total = Number(report.total_yuan)
  if (!total) return <p className="chart-empty">本期没有支出，暂无分类占比。</p>
  const segments = report.categories.map((row, index) => {
    const start = report.categories.slice(0, index).reduce((sum, item) => sum + Number(item.amount_yuan), 0) / total * 100
    const end = start + Number(row.amount_yuan) / total * 100
    return `${colors[index % colors.length]} ${start}% ${end}%`
  })
  return <div className="donut-layout">
    <div className="donut-ring" role="img" aria-label={`${report.period}分类支出占比`} style={{ background: `conic-gradient(${segments.join(', ')})` }}>
      <div><small>总支出</small><strong>{money(report.total_yuan)}</strong></div>
    </div>
    <div className="donut-legend">
      {report.categories.map((row, index) => <div key={row.name}><i style={{ background: colors[index % colors.length] }} /><span>{row.name}</span><strong>{((Number(row.amount_yuan) / total) * 100).toFixed(1)}%</strong><small>{money(row.amount_yuan)}</small></div>)}
    </div>
  </div>
}

function Trend({ report }: { report: Report }) {
  const totals = new Map<string, number>()
  for (const tx of report.transactions) totals.set(tx.posted_on, (totals.get(tx.posted_on) || 0) + Number(tx.amount_yuan))
  const days = [...totals.entries()].sort(([a], [b]) => a.localeCompare(b))
  if (!days.length) return <p className="chart-empty">本期没有支出，暂无日期趋势。</p>
  const max = Math.max(...days.map(([, value]) => value), 1)
  const first = Date.parse(days[0][0])
  const last = Date.parse(days[days.length - 1][0])
  const points = days.map(([day, value]) => ({
    day, value,
    x: first === last ? 360 : 38 + ((Date.parse(day) - first) / (last - first)) * 644,
    y: 188 - (value / max) * 142,
  }))
  const line = points.map((point) => `${point.x},${point.y}`).join(' ')
  const area = `${points[0].x},188 ${line} ${points[points.length - 1].x},188`
  return <div className="trend-layout">
    <svg viewBox="0 0 720 215" role="img" aria-label={`${report.period}交易日支出趋势，最高单日${money(max)}`}>
      <line x1="38" y1="188" x2="682" y2="188" className="trend-axis" />
      <line x1="38" y1="117" x2="682" y2="117" className="trend-grid" />
      <line x1="38" y1="46" x2="682" y2="46" className="trend-grid" />
      <polygon points={area} className="trend-area" />
      <polyline points={line} className="trend-line" />
      {points.map((point) => <circle key={point.day} cx={point.x} cy={point.y} r="5" className="trend-point"><title>{point.day} · {money(point.value)}</title></circle>)}
      <text x="38" y="207" className="trend-label">{days[0][0]}</text>
      {days.length > 1 && <text x="682" y="207" textAnchor="end" className="trend-label">{days[days.length - 1][0]}</text>}
    </svg>
    <p className="trend-caption">按交易日汇总；横轴覆盖本报告最早至最晚交易日。最高单日 {money(max)}。</p>
  </div>
}

export function ReportCard({ report, view = 'bars' }: { report: Report; view?: ChartView }) {
  return <div className="report-card">
    <div className="report-card__top"><span>{report.period} 支出</span><strong>{money(report.total_yuan)}</strong></div>
    {view === 'bars' && <Bars report={report} />}
    {view === 'donut' && <Donut report={report} />}
    {view === 'trend' && <Trend report={report} />}
    <p className="report-card__source"><span>账本计算</span>依据 {report.transaction_ids.length} 笔模拟交易 · 可追溯至交易记录</p>
    {report.alerts.map((alert) => <p className="report-alert" key={alert.transaction_id}>复核提醒：{alert.counterparty} {money(alert.amount_yuan)}。{alert.reason}</p>)}
  </div>
}
