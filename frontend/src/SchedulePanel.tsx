import { useEffect, useRef, useState } from 'react'
import { formatBankTime } from './bankTime'
import './SchedulePanel.css'

type ScheduleTask = {
  id: string
  status: 'pending' | 'completed' | 'failed' | 'cancelled' | 'expired'
  recipient: string
  phone_masked: string
  amount_yuan: string
  note: string
  execute_at: string
  expires_at: string
  authorized_at: string
  finished_at: string | null
  failure_reason: string | null
  transaction_id: string | null
  result: { message: string } | null
}
type ScheduleData = {
  clock: { now: string; mode: string; controls_enabled: boolean }
  tasks: ScheduleTask[]
}
const statusLabels: Record<ScheduleTask['status'], string> = {
  pending: '待执行', completed: '已转账', failed: '执行失败', cancelled: '已取消', expired: '已失效',
}

async function request<T>(url: string, body?: object): Promise<T> {
  const response = await fetch(url, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : undefined)
  const data = await response.json()
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请重试')
  return data as T
}

export function SchedulePanel({ sessionId, revision, onChanged }: {
  sessionId: string; revision: number; onChanged: () => void
}) {
  const [data, setData] = useState<ScheduleData | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [reload, setReload] = useState(0)
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const previousState = useRef('')
  const onChangedRef = useRef(onChanged)
  useEffect(() => { onChangedRef.current = onChanged }, [onChanged])
  useEffect(() => {
    let active = true
    let loading = false
    async function load() {
      if (loading) return
      loading = true
      try {
        const next = await request<ScheduleData>(`/api/schedules?session_id=${encodeURIComponent(sessionId)}`)
        if (!active) return
        const signature = JSON.stringify([next.clock.now, next.tasks.map((task) => [task.id, task.status])])
        if (previousState.current !== signature && next.tasks.some((task) => task.status !== 'pending')) setHistoryOpen(true)
        if (previousState.current && previousState.current !== signature) onChangedRef.current()
        previousState.current = signature
        setData(next)
        setError('')
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : '预约加载失败')
      } finally {
        loading = false
      }
    }
    void load()
    const timer = window.setInterval(() => { if (!document.hidden) void load() }, 4000)
    return () => { active = false; window.clearInterval(timer) }
  }, [sessionId, revision, reload])
  useEffect(() => {
    if (selectedId && dialog.current && !dialog.current.open) dialog.current.showModal()
  }, [selectedId])

  const selected = data?.tasks.find((task) => task.id === selectedId)
  const pending = data?.tasks.filter((task) => task.status === 'pending') || []
  const history = data?.tasks.filter((task) => task.status !== 'pending') || []

  async function cancel() {
    if (!selected || busy) return
    setBusy(true)
    setError('')
    try {
      const task = await request<ScheduleTask>(`/api/schedules/${selected.id}/cancel`, { session_id: sessionId })
      setData((current) => current ? { ...current, tasks: current.tasks.map((row) => row.id === task.id ? task : row) } : current)
      setConfirmCancel(false)
      setHistoryOpen(true)
      setNotice(task.status === 'cancelled' ? '预约已取消，不会执行。' : `预约当前${statusLabels[task.status]}，请核对结果。`)
      onChangedRef.current()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '取消失败')
    } finally { setBusy(false) }
  }

  async function advance() {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const next = await request<ScheduleData>('/api/demo/clock/advance-next', { session_id: sessionId })
      setData(next)
      setHistoryOpen(true)
      setNotice(`演示时间已推进到 ${formatBankTime(next.clock.now)}，请查看预约执行结果。`)
      onChangedRef.current()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '推进时间失败')
    } finally { setBusy(false) }
  }

  function taskRow(task: ScheduleTask) {
    return <button type="button" className="schedule-row" key={task.id} onClick={() => { setSelectedId(task.id); setConfirmCancel(false); setNotice('') }} aria-label={`查看${task.recipient} ${task.amount_yuan}元 ${statusLabels[task.status]}的预约明细`}>
      <span className="schedule-row__date">{formatBankTime(task.execute_at)}<small>北京时间</small></span>
      <span className="schedule-row__person"><strong>{task.recipient}</strong><small>{task.note}</small></span>
      <strong className="schedule-row__amount">¥{task.amount_yuan}</strong>
      <span className={`schedule-status schedule-status--${task.status}`}>{statusLabels[task.status]}</span>
      <span className="schedule-row__arrow" aria-hidden="true">↗</span>
    </button>
  }

  return <section className="schedule-panel" aria-labelledby="schedule-heading">
    <header className="schedule-panel__head"><div><h2 id="schedule-heading">我的预约 <span>{pending.length} 笔待执行</span></h2><p>单次转账 · 确认后到期自动检查并执行</p></div><button type="button" className="schedule-quiet" disabled={busy} onClick={() => setReload((value) => value + 1)}>刷新</button></header>
    {error && !selectedId && <p className="error-banner" role="alert">{error}</p>}
    {!data && !error && <p className="empty-note" role="status">正在读取预约…</p>}
    {data && <>
      <div className="schedule-clock"><span>演示时间</span><time dateTime={data.clock.now}>{formatBankTime(data.clock.now)}</time><small>北京时间 · 手动推进</small></div>
      {pending.length ? <div className="schedule-list">{pending.map(taskRow)}</div> : <p className="schedule-empty">暂无待执行预约。试试“明天晚上8点给林悦转300元”。</p>}
      {history.length > 0 && <details className="schedule-history" open={historyOpen} onToggle={(event) => setHistoryOpen(event.currentTarget.open)}><summary>历史预约 <span>{history.length}</span></summary><div className="schedule-list">{history.slice().reverse().map(taskRow)}</div></details>}
      {notice && !selectedId && <p className="schedule-notice" role="status">{notice}</p>}
      {data.clock.controls_enabled && <details className="schedule-demo"><summary>演示时间控制</summary><p>推进全局模拟时间，处理下一时刻所有已授权的到期预约。账单日期和每日限额随之变化；不会修改电脑时间。后端停止期间不执行。</p><button type="button" className="schedule-quiet" disabled={busy || !pending.length} onClick={() => void advance()}>{busy ? '正在处理…' : '推进到下一预约时间'}</button></details>}
    </>}
    <dialog className="transaction-dialog schedule-dialog" ref={dialog} aria-label="预约转账明细" onClose={() => { setSelectedId(null); setConfirmCancel(false); setNotice('') }}>
      {selected && <div className="schedule-dialog__body">
        <div className="transaction-dialog__head"><div><small>模拟银行 · 单次预约</small><h2>转给{selected.recipient}</h2></div><button type="button" onClick={() => dialog.current?.close()} aria-label="关闭预约明细">×</button></div>
        <div className="schedule-dialog__amount"><strong className="transaction-dialog__amount">¥{selected.amount_yuan}</strong><span className={`schedule-status schedule-status--${selected.status}`}>{statusLabels[selected.status]}</span></div>
        <dl><div><dt>收款人</dt><dd>{selected.recipient} · {selected.phone_masked}</dd></div><div><dt>预约时间</dt><dd>{formatBankTime(selected.execute_at)}（北京时间）</dd></div><div><dt>执行窗口截至</dt><dd>{formatBankTime(selected.expires_at)}</dd></div><div><dt>备注</dt><dd>{selected.note}</dd></div><div><dt>预约编号</dt><dd>{selected.id}</dd></div><div><dt>授权时间</dt><dd>{formatBankTime(selected.authorized_at)}（实际时间）</dd></div>{selected.finished_at && <div><dt>处理时间</dt><dd>{formatBankTime(selected.finished_at)}（演示时间）</dd></div>}{selected.transaction_id && <div><dt>交易编号</dt><dd>{selected.transaction_id}</dd></div>}</dl>
        {selected.failure_reason && <p className="transaction-dialog__alert">{selected.failure_reason}</p>}
        {selected.result && <p className="schedule-notice">{selected.result.message}</p>}
        {selected.status === 'pending' && <p className="transaction-dialog__foot">不会提前冻结余额。到期后 10 分钟内通过余额、收款人及日累计限额检查后自动转账；无需再次确认。超时失效或检查失败均不自动重试。</p>}
        {error && <p className="error-banner" role="alert">{error}</p>}
        {notice && <p className="schedule-notice" role="status">{notice}</p>}
        <div className="schedule-dialog__actions">
          {selected.transaction_id && <a className="schedule-quiet" href="#/bills" onClick={() => dialog.current?.close()}>查看账单 ↗</a>}
          {selected.status === 'pending' && (confirmCancel ? <><span>确定取消这笔预约？</span><button type="button" className="schedule-danger" disabled={busy} onClick={() => void cancel()}>{busy ? '正在取消…' : '确认取消预约'}</button><button type="button" className="schedule-quiet" disabled={busy} onClick={() => setConfirmCancel(false)}>保留预约</button></> : <button type="button" className="schedule-quiet" disabled={busy} onClick={() => setConfirmCancel(true)}>取消预约</button>)}
          {(selected.status === 'failed' || selected.status === 'expired') && <button type="button" className="schedule-quiet" onClick={() => { dialog.current?.close(); document.getElementById('smart-transfer-input')?.focus() }}>重新填写预约</button>}
        </div>
      </div>}
    </dialog>
  </section>
}
