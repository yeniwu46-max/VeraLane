import { useCallback, useEffect, useReducer, useState } from 'react'
import './TransferRemindersPanel.css'

type Reminder = {
  id: string
  title: string
  body: string
  due_on: string
  created_at: string
  status: 'pending' | 'due' | 'completed' | 'cancelled'
}

type Response = { items: Reminder[]; as_of: string }
type LoadState = { data: Response | null; error: string }
type LoadAction = { type: 'loaded'; data: Response } | { type: 'failed'; error: string } | { type: 'clear_error' }

function loadReducer(state: LoadState, action: LoadAction): LoadState {
  if (action.type === 'loaded') return { data: action.data, error: '' }
  if (action.type === 'failed') return { ...state, error: action.error }
  return { ...state, error: '' }
}

export function TransferRemindersPanel({ sessionId }: { sessionId: string }) {
  const [loaded, dispatchLoad] = useReducer(loadReducer, { data: null, error: '' })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const response = await fetch(`/api/transfer-reminders?session_id=${encodeURIComponent(sessionId)}`, { signal })
    const body = await response.json()
    if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '提醒读取失败，请重试。')
    dispatchLoad({ type: 'loaded', data: body as Response })
  }, [sessionId])

  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal).catch((cause: unknown) => {
      if (!controller.signal.aborted) dispatchLoad({ type: 'failed', error: cause instanceof Error ? cause.message : '提醒读取失败，请重试。' })
    })
    return () => controller.abort()
  }, [refresh, revision])

  async function changeStatus(reminder: Reminder, action: 'complete' | 'cancel') {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const response = await fetch(`/api/transfer-reminders/${encodeURIComponent(reminder.id)}/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: sessionId }),
      })
      const body = await response.json()
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '提醒更新失败，请重试。')
      setNotice(action === 'complete' ? '已标记为处理完成。' : '已取消这条提醒。')
      setRevision((value) => value + 1)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '提醒更新失败，请重试。')
    } finally {
      setBusy(false)
    }
  }

  const labels = { pending: '待提醒', due: '到期', completed: '已处理', cancelled: '已取消' }
  return <section className="transfer-reminders" aria-labelledby="transfer-reminders-title">
    <header className="transfer-reminders__header">
      <div><span className="transfer-reminders__eyebrow">站内提醒 · 不执行金融操作</span><h2 id="transfer-reminders-title">转账提醒</h2></div>
      <button type="button" className="page-secondary" disabled={busy} onClick={() => { setError(''); dispatchLoad({ type: 'clear_error' }); setRevision((value) => value + 1) }}>刷新</button>
    </header>
    <p className="transfer-reminders__notice">提醒按演示日期显示在本页；不会发系统通知、创建预约转账或移动资金。</p>
    {notice && <p className="transfer-reminders__status" role="status">{notice}</p>}
    {(error || loaded.error) && <p className="error-banner" role="alert">{error || loaded.error}</p>}
    {!error && !loaded.error && loaded.data && !loaded.data.items.length && <div className="transfer-reminders__empty"><strong>目前没有转账提醒</strong><span>在对话中说“提醒我明天核对给林悦的转账”即可创建日期提醒。</span></div>}
    <div className="transfer-reminders__list">
      {loaded.data?.items.map((reminder) => <article className={`transfer-reminder transfer-reminder--${reminder.status}`} key={reminder.id}>
        <div className="transfer-reminder__meta"><strong>{reminder.title}</strong><span>{labels[reminder.status]}</span></div>
        <p>{reminder.body}</p>
        <small>提醒日期 {reminder.due_on} · 演示日期 {loaded.data?.as_of}</small>
        {(reminder.status === 'pending' || reminder.status === 'due') && <div className="transfer-reminder__actions">
          {reminder.status === 'due' && <button type="button" className="page-secondary" disabled={busy} onClick={() => void changeStatus(reminder, 'complete')}>标记已处理</button>}
          <button type="button" className="transfer-reminder__cancel" disabled={busy} onClick={() => void changeStatus(reminder, 'cancel')}>取消提醒</button>
        </div>}
      </article>)}
    </div>
  </section>
}
