import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react'
import { ReportCard, type BillTransaction, type ChartView, type Report } from './BillVisuals'
import { SchedulePanel } from './SchedulePanel'
import { AaPanel } from './AaPanel'
import type { AaDraft, AaSeed, AaSource } from './aaApi'
import { formatBankTime } from './bankTime'
import './App.css'
import './night.css'

type Tier = 'yellow' | 'red'

type PendingAction = {
  id: string
  type: 'transfer' | 'scheduled_transfer' | 'subscription_cancel'
  tier: Tier
  status: 'pending'
  expires_at: string
  details: Record<string, string | number>
}

type View = 'chat' | 'transfer' | 'bills' | 'subscriptions'
type Contact = { id: string; name: string; phone_masked: string }
type BillPeriod = '本月' | '上个月' | '今年' | '去年'

type AgentReply = {
  session_id: string
  message: string
  mode: 'deepseek' | 'offline'
  pending_action?: PendingAction
  report?: Report
  choices?: { id: string; name: string; phone: string }[]
  aa_draft?: AaDraft
}

type ConversationMessage = {
  id: string
  role: 'user' | 'assistant'
  text: string
  reply?: AgentReply
}

type ChatSize = { width: number; height: number }
type ResizeOrigin = ChatSize & { x: number; y: number }

type Overview = {
  demo_date: string
  demo_now: string
  model_configured: boolean
  account: { label: string; balance_yuan: string }
  transactions: {
    id: string
    posted_on: string
    direction: 'in' | 'out'
    amount_yuan: string
    counterparty: string
    category: string
  }[]
  subscriptions: {
    id: string
    merchant: string
    amount_yuan: string
    renewal_on: string
    status: 'active' | 'cancelled'
  }[]
  subscription_signals: {
    merchant: string
    evidence_ids: string[]
    last_amount_yuan: string
    renewal_on: string | null
    days_until_renewal: number | null
    reminder: boolean
  }[]
  audit: { at: string; event: string; details: Record<string, string> }[]
}

const suggestions = [
  { label: '查账户余额', prompt: '我的账户余额是多少？' },
  { label: '分析本月账单', prompt: '分析一下我本月的账单' },
  { label: '给林悦转账', prompt: '转给林悦300元，备注房租' },
  { label: '管理自动续费', prompt: '查看我正在扣费的订阅' },
]

const navigation: { view: View; label: string }[] = [
  { view: 'chat', label: '对话工作台' },
  { view: 'transfer', label: '智能转账' },
  { view: 'bills', label: '账单分析' },
  { view: 'subscriptions', label: '订阅管理' },
]

function NavIcon({ view }: { view: View }) {
  const common = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const }
  return <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" {...common}>
    {view === 'chat' && <><rect x="3" y="4" width="18" height="15" rx="2" /><path d="M7 9h10M7 13h7M8 19v2" /></>}
    {view === 'transfer' && <><path d="M4 17 17 4M8 4h9v9" /><path d="M4 8v12h12" /></>}
    {view === 'bills' && <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 8h8M8 12h8M8 16h5" /></>}
    {view === 'subscriptions' && <><path d="M6 7a8 8 0 0 1 13-1l2 2M18 17a8 8 0 0 1-13 1l-2-2" /><path d="M21 3v5h-5M3 21v-5h5" /></>}
  </svg>
}

function viewFromHash(): View {
  const candidate = window.location.hash.replace(/^#\/?/, '')
  return navigation.find((item) => item.view === candidate)?.view || 'chat'
}

const eventLabels: Record<string, string> = {
  demo_seeded: '模拟数据已载入',
  intent_parsed: '理解用户意图',
  action_prepared: '生成待确认操作',
  action_completed: '执行已确认操作',
  action_expired: '操作确认已过期',
  strong_verification_required: '已拦截强验证操作',
  schedule_authorized: '已确认预约转账',
  schedule_completed: '预约转账已执行',
  schedule_failed: '预约转账检查未通过',
  schedule_expired: '预约已超时失效',
  schedule_cancelled: '预约已取消',
  demo_clock_advanced: '演示时间已推进',
  aa_collection_created: '已建立AA收款单',
  aa_payment_received: 'AA回款已到账',
  aa_collection_closed: '剩余AA请求已关闭',
}

function getSessionId(): string {
  const previous = window.localStorage.getItem('veralane-session-id')
  if (previous) return previous
  const next = window.crypto.randomUUID()
  window.localStorage.setItem('veralane-session-id', next)
  return next
}

function currency(value: string): string {
  return `¥${Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

async function apiJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options?.headers || {}) },
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '请求失败，请检查服务是否运行')
  return data as T
}

function ActionCard({
  action,
  onConfirm,
  busy,
  completed,
}: {
  action: PendingAction
  onConfirm: (id: string) => void
  busy: boolean
  completed: boolean
}) {
  const isScheduled = action.type === 'scheduled_transfer'
  const isTransfer = action.type === 'transfer' || isScheduled
  const [reviewed, setReviewed] = useState(false)
  const target = isTransfer ? String(action.details.recipient) : String(action.details.merchant)
  const confirmLabel = isScheduled ? `确认预约 ${currency(String(action.details.amount_yuan))}` : isTransfer
    ? `确认模拟转出 ${currency(String(action.details.amount_yuan))}`
    : `确认取消 ${target}`
  return (
    <div className={`action-card ${action.tier === 'red' ? 'action-card--red' : ''}`}>
      <div className="action-card__head">
        <span className="action-card__title">{isScheduled ? '预约转账计划' : isTransfer ? '转账计划' : '取消代扣计划'}</span>
        <span className={`tier tier--${action.tier}`}>
          {action.tier === 'red' ? '强验证' : '需确认'}
        </span>
      </div>
      {isTransfer ? (
        <dl className="action-card__details">
          <div><dt>收款人</dt><dd>{action.details.recipient} · {action.details.phone_masked}</dd></div>
          <div><dt>金额</dt><dd className="action-card__amount">{currency(String(action.details.amount_yuan))}</dd></div>
          <div><dt>备注</dt><dd>{action.details.note}</dd></div>
          {isScheduled && <><div><dt>执行时间</dt><dd>{formatBankTime(String(action.details.execute_at))}（北京时间）</dd></div><div><dt>执行窗口截至</dt><dd>{formatBankTime(String(action.details.window_expires_at))}</dd></div></>}
        </dl>
      ) : (
        <dl className="action-card__details">
          <div><dt>商户</dt><dd>{action.details.merchant}</dd></div>
          <div><dt>每期扣费</dt><dd>{currency(String(action.details.amount_yuan))}</dd></div>
          <div><dt>下次扣费</dt><dd>{action.details.renewal_on}</dd></div>
        </dl>
      )}
      <div className="action-card__footer">
        <div className="action-card__review">
          {action.tier === 'red' ? (
            <p>已触发强验证。当前演示环境不能继续执行此操作。</p>
          ) : completed ? (
            <p>{isScheduled ? '预约授权已保存。前往智能转账页“我的预约”查看实时执行状态。' : '操作已完成。结果已写入模拟账本与操作记录。'}</p>
          ) : (
            <>
              <label className="review-check"><input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} /><span>我已核对{isScheduled ? '收款人、金额、备注与执行时间，授权到期自动转账' : isTransfer ? '收款人、金额与备注，同意执行此操作' : '商户及代扣协议，同意执行此操作'}</span></label>
              {isScheduled && <p className="schedule-consent">到期后 10 分钟内通过检查后自动执行，无需再次确认；不提前冻结余额。超时或检查失败均不自动重试。</p>}
              <p>待确认计划有效期 10 分钟；执行时会再次检查权限与状态。</p>
            </>
          )}
        </div>
        <button
          type="button"
          className="confirm-button"
          disabled={busy || completed || action.tier === 'red' || !reviewed}
          onClick={() => onConfirm(action.id)}
        >
          {completed ? isScheduled ? '已预约' : '已执行' : action.tier === 'red' ? '需要强验证' : busy ? '正在处理…' : confirmLabel}
        </button>
      </div>
    </div>
  )
}

function Provenance({ reply }: { reply: AgentReply }) {
  const aiUsed = reply.mode === 'deepseek'
  return <details className="provenance">
    <summary>
      <span className={`provenance__badge ${aiUsed ? 'provenance__badge--ai' : ''}`}>{aiUsed ? 'AI' : '规则'}</span>
      <span>{aiUsed ? 'DeepSeek 识别意图' : '本地规则识别意图'} · 银行工具核验</span>
      <span className="provenance__more">查看处理依据</span>
    </summary>
    <div className="provenance__detail">
      <p>{aiUsed ? 'DeepSeek 用于理解你的自然语言需求；账户余额、交易与操作权限由银行工具核验。' : '本地规则识别当前需求；账户余额、交易与操作权限由银行工具核验。'}</p>
      {reply.report && <p>账单统计来自 {reply.report.transaction_ids.length} 笔模拟交易，交易编号：{reply.report.transaction_ids.join('、') || '无'}。</p>}
      {reply.pending_action && <p>已生成待确认计划（{reply.pending_action.tier === 'red' ? '强验证拦截' : '需用户确认'}）。点击执行前会再次校验。</p>}
      <p>当前演示使用虚构账户与交易数据。</p>
    </div>
  </details>
}

function App() {
  const [sessionId] = useState(getSessionId)
  const [view, setView] = useState<View>(viewFromHash)
  const [transferTab, setTransferTab] = useState<'transfer' | 'aa'>('transfer')
  const [aaSeed, setAaSeed] = useState<AaSeed | undefined>()
  const [overview, setOverview] = useState<Overview | null>(null)
  const [contacts, setContacts] = useState<Contact[]>([])
  const [transferContactId, setTransferContactId] = useState('')
  const [transferAmount, setTransferAmount] = useState('')
  const [transferNote, setTransferNote] = useState('')
  const [transferAction, setTransferAction] = useState<PendingAction | null>(null)
  const [transferNotice, setTransferNotice] = useState('')
  const [smartTransferInput, setSmartTransferInput] = useState('')
  const [smartTransferReply, setSmartTransferReply] = useState<AgentReply | null>(null)
  const [smartTransferNotice, setSmartTransferNotice] = useState('')
  const [billPeriod, setBillPeriod] = useState<BillPeriod>('本月')
  const [billReport, setBillReport] = useState<Report | null>(null)
  const [billChart, setBillChart] = useState<ChartView>('bars')
  const [selectedTransaction, setSelectedTransaction] = useState<BillTransaction | null>(null)
  const [cancelAction, setCancelAction] = useState<PendingAction | null>(null)
  const [cancelNotice, setCancelNotice] = useState('')
  const [messages, setMessages] = useState<ConversationMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      text: '你好，我是 VeraLane。你可以直接说想办什么银行业务。我会先核对数据和权限，资金或协议变更会请你确认。',
    },
  ])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [completedActions, setCompletedActions] = useState<string[]>([])
  const [scheduleRevision, setScheduleRevision] = useState(0)
  const [showInsights, setShowInsights] = useState(false)
  const [chatSize, setChatSize] = useState<ChatSize | null>(null)
  const threadEnd = useRef<HTMLDivElement>(null)
  const chatPanel = useRef<HTMLElement>(null)
  const resizeOrigin = useRef<ResizeOrigin | null>(null)
  const transactionDialog = useRef<HTMLDialogElement>(null)

  function openAa(options: { reply?: AgentReply; source?: AaSource } = {}) {
    setAaSeed({ id: crypto.randomUUID(), draft: options.reply?.aa_draft, source: options.source, message: options.reply?.message, mode: options.reply?.mode })
    setTransferTab('aa')
    window.location.assign('#/transfer')
    setView('transfer')
    setError('')
  }

  function beginChatResize(event: ReactPointerEvent<HTMLButtonElement>) {
    const panel = chatPanel.current
    if (!panel) return
    const bounds = panel.getBoundingClientRect()
    resizeOrigin.current = { x: event.clientX, y: event.clientY, width: bounds.width, height: bounds.height }
    event.currentTarget.setPointerCapture(event.pointerId)
    event.preventDefault()
  }

  function moveChatResize(event: ReactPointerEvent<HTMLButtonElement>) {
    const origin = resizeOrigin.current
    if (!origin) return
    const availableWidth = chatPanel.current?.parentElement?.clientWidth || window.innerWidth
    const minimumWidth = Math.min(360, availableWidth)
    setChatSize({
      width: Math.max(minimumWidth, Math.min(availableWidth, origin.width + event.clientX - origin.x)),
      height: Math.max(360, Math.min(1600, origin.height + event.clientY - origin.y)),
    })
  }

  function endChatResize(event: ReactPointerEvent<HTMLButtonElement>) {
    resizeOrigin.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  function resizeChatWithKeyboard(event: ReactKeyboardEvent<HTMLButtonElement>) {
    const directions: Record<string, [number, number]> = {
      ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1],
    }
    const direction = directions[event.key]
    if (!direction || !chatPanel.current) return
    event.preventDefault()
    const panel = chatPanel.current
    const bounds = panel.getBoundingClientRect()
    const availableWidth = panel.parentElement?.clientWidth || window.innerWidth
    const step = event.shiftKey ? 80 : 24
    setChatSize({
      width: Math.max(Math.min(360, availableWidth), Math.min(availableWidth, bounds.width + direction[0] * step)),
      height: Math.max(360, Math.min(1600, bounds.height + direction[1] * step)),
    })
  }

  async function refreshOverview() {
    try {
      setOverview(await apiJson<Overview>('/api/overview'))
    } catch {
      setError('无法连接本地 API。请先启动后端服务。')
    }
  }

  useEffect(() => {
    void apiJson<Overview>('/api/overview')
      .then(setOverview)
      .catch(() => setError('无法连接本地 API。请先启动后端服务。'))
    void apiJson<Contact[]>('/api/contacts')
      .then(setContacts)
      .catch(() => setError('无法读取收款人。请检查后端服务。'))
  }, [])
  useEffect(() => {
    const onHashChange = () => { setView(viewFromHash()); setError('') }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])
  useEffect(() => {
    if (view !== 'bills') return
    const controller = new AbortController()
    void apiJson<Report>(`/api/bills?period=${encodeURIComponent(billPeriod)}`, { signal: controller.signal })
      .then(setBillReport)
      .catch((cause) => {
        if (cause instanceof Error && cause.name === 'AbortError') return
        setError(cause instanceof Error ? cause.message : '账单加载失败')
      })
    return () => controller.abort()
  }, [view, billPeriod])
  useEffect(() => { threadEnd.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])
  useEffect(() => {
    const dialog = transactionDialog.current
    if (selectedTransaction && dialog && !dialog.open) dialog.showModal()
  }, [selectedTransaction])

  async function prepareDirectTransfer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy || !transferContactId) return
    setBusy(true)
    setError('')
    setTransferAction(null)
    try {
      const result = await apiJson<AgentReply>('/api/transfers/prepare', {
        method: 'POST',
        body: JSON.stringify({
          session_id: sessionId, contact_id: transferContactId,
          amount_yuan: transferAmount.trim(), note: transferNote.trim() || '转账',
        }),
      })
      setTransferAction(result.pending_action || null)
      setTransferNotice(result.message)
      await refreshOverview()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '转账计划生成失败')
    } finally {
      setBusy(false)
    }
  }

  async function submitSmartTransfer(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const message = smartTransferInput.trim()
    if (!message || busy) return
    setBusy(true)
    setError('')
    setSmartTransferInput('')
    setSmartTransferReply(null)
    setSmartTransferNotice('')
    try {
      const result = await apiJson<AgentReply>('/api/chat', {
        method: 'POST', body: JSON.stringify({ session_id: sessionId, message }),
      })
      setSmartTransferReply(result)
      await refreshOverview()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '未能理解转账需求')
    } finally {
      setBusy(false)
    }
  }

  async function chooseSmartRecipient(contactId: string) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const result = await apiJson<AgentReply>('/api/transfers/resolve', {
        method: 'POST', body: JSON.stringify({ session_id: sessionId, contact_id: contactId }),
      })
      setSmartTransferReply(result)
      await refreshOverview()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '无法选择收款人')
    } finally {
      setBusy(false)
    }
  }

  async function prepareDirectCancel(subscriptionId: string) {
    if (busy) return
    setBusy(true)
    setError('')
    setCancelAction(null)
    try {
      const result = await apiJson<AgentReply>(`/api/subscriptions/${subscriptionId}/prepare-cancel`, {
        method: 'POST', body: JSON.stringify({ session_id: sessionId }),
      })
      setCancelAction(result.pending_action || null)
      setCancelNotice(result.message)
      await refreshOverview()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '取消计划生成失败')
    } finally {
      setBusy(false)
    }
  }

  async function sendMessage(value: string) {
    const message = value.trim()
    if (!message || busy) return
    setDraft('')
    setError('')
    setMessages((current) => [...current, { id: crypto.randomUUID(), role: 'user', text: message }])
    setBusy(true)
    try {
      const result = await apiJson<AgentReply>('/api/chat', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId, message }),
      })
      setMessages((current) => [...current, { id: crypto.randomUUID(), role: 'assistant', text: result.message, reply: result }])
      await refreshOverview()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '请求失败')
    } finally {
      setBusy(false)
    }
  }

  async function confirmAction(actionId: string) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const result = await apiJson<{ message: string }>(`/api/actions/${actionId}/confirm`, {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId }),
      })
      setCompletedActions((current) => [...current, actionId])
      setScheduleRevision((current) => current + 1)
      if (transferAction?.id !== actionId && cancelAction?.id !== actionId && smartTransferReply?.pending_action?.id !== actionId) {
        setMessages((current) => [...current, { id: crypto.randomUUID(), role: 'assistant', text: result.message }])
      }
      if (transferAction?.id === actionId) setTransferNotice(result.message)
      if (cancelAction?.id === actionId) setCancelNotice(result.message)
      if (smartTransferReply?.pending_action?.id === actionId) setSmartTransferNotice(result.message)
      await refreshOverview()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '确认失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={`app-shell ${showInsights ? 'app-shell--insights-open' : ''}`}>
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark">V</div><div><strong>VeraLane</strong><span>Banking, verified.</span></div></div>
        <div className="sidebar-section">
          <span className="sidebar-label">工作区</span>
          {navigation.map((item) => (
            <a
              key={item.view}
              className={`nav-item ${view === item.view ? 'nav-item--active' : ''}`}
              href={`#/${item.view}`}
              aria-current={view === item.view ? 'page' : undefined}
            ><span className="nav-symbol"><NavIcon view={item.view} /></span>{item.label}</a>
          ))}
        </div>
      </aside>

      <main className={`main-column ${view === 'chat' ? 'main-column--chat' : 'main-column--page'}`}>
        <header className="topbar"><span>VeraLane / {navigation.find((item) => item.view === view)?.label}</span><div className="topbar__actions"><span className="topbar__demo">模拟环境</span><button type="button" className="topbar__toggle" aria-expanded={showInsights} onClick={() => { setShowInsights((current) => !current); setChatSize(null) }}>{showInsights ? '收起概览' : '打开概览'}</button></div></header>
        {view === 'chat' ? <>
        <section className="conversation-panel" aria-label="银行智能体对话" ref={chatPanel} style={chatSize ? { width: chatSize.width, height: chatSize.height } : undefined}>
          <div className="conversation-panel__header"><div><h1>VeraLane 对话</h1><p>账户与操作来自模拟银行环境</p></div><span className="status-pill"><i />{overview ? '服务就绪' : error ? '连接中断' : '连接中'}</span></div>
          <div className="thread">
            {messages.map((item) => (
              <div className={`message message--${item.role}`} key={item.id}>
                {item.role === 'assistant' && <div className="avatar">V</div>}
                <div className="message__body">
                  <div className="bubble">{item.text}</div>
                  {item.reply?.report && <ReportCard report={item.reply.report} />}
                  {item.reply?.pending_action && (
                    <ActionCard
                      key={item.reply.pending_action.id}
                      action={item.reply.pending_action}
                      onConfirm={confirmAction}
                      busy={busy}
                      completed={completedActions.includes(item.reply.pending_action.id)}
                    />
                  )}
                  {item.reply?.choices && <div className="choice-note">{item.reply.choices.map((choice) => `${choice.name} ${choice.phone}`).join('　/　')}</div>}
                  {item.reply?.aa_draft && <button type="button" className="aa-quiet aa-chat-entry" onClick={() => openAa({ reply: item.reply })}>核对 AA 分摊草稿 ↗</button>}
                  {item.reply && <Provenance reply={item.reply} />}
                </div>
              </div>
            ))}
            {busy && <div className="thinking">正在核对请求…</div>}
            <div ref={threadEnd} />
          </div>
          <div className="composer-wrap">
            {error && <p className="error-banner" role="alert">{error}</p>}
            <div className="suggestions">{suggestions.map((item) => <button type="button" onClick={() => void sendMessage(item.prompt)} disabled={busy} key={item.label}>{item.label}</button>)}</div>
            <form className="composer" onSubmit={(event) => { event.preventDefault(); void sendMessage(draft) }}>
              <input aria-label="输入银行业务需求" placeholder="例如：转给林悦300元，备注房租" maxLength={500} value={draft} onChange={(event) => setDraft(event.target.value)} />
              <button type="submit" disabled={busy || !draft.trim()}>发送 <span>↗</span></button>
            </form>
          </div>
          <div className="conversation-panel__resize"><span>拖动右下角调整宽高；方向键可微调</span><button type="button" className="resize-reset" onClick={() => setChatSize(null)} disabled={!chatSize}>重置大小</button><button type="button" className="resize-handle" aria-label="调整对话框宽度和高度" title="拖动调整宽高；方向键微调" onPointerDown={beginChatResize} onPointerMove={moveChatResize} onPointerUp={endChatResize} onPointerCancel={endChatResize} onKeyDown={resizeChatWithKeyboard}><span aria-hidden="true">⤡</span></button></div>
        </section>
        </> : <section className="workspace-page">
          {error && <p className="error-banner" role="alert">{error}</p>}

          {view === 'transfer' && <>
            <div className="page-header"><span>即时转账 / 智能预约 / AA 分账</span><h1>智能转账</h1><p>{transferTab === 'aa' ? '从一句话或一笔消费开始分摊，逐人核对收款进度。' : '说出转给谁、多少钱、什么时候；核对计划后，立即办理或预约到期执行。'}</p></div>
            <div className="transfer-tabs" role="group" aria-label="转账业务类型"><button type="button" aria-pressed={transferTab === 'transfer'} onClick={() => setTransferTab('transfer')}>转账 / 预约</button><button type="button" aria-pressed={transferTab === 'aa'} onClick={() => setTransferTab('aa')}>AA 收款</button></div>
            {transferTab === 'aa' ? <AaPanel sessionId={sessionId} contacts={contacts} seed={aaSeed} onChanged={() => void refreshOverview()} onCreated={() => setAaSeed(undefined)} /> : <>
            {overview?.demo_now && <p className="transfer-clock">当前演示时间：{formatBankTime(overview.demo_now)}（北京时间） · “明天”等表达以此为准</p>}
            <section className="smart-transfer-card">
              <div className="smart-transfer-card__head"><div><span>自然语言入口</span><h2>一句话，生成转账计划</h2></div><strong>01 / 理解并核验</strong></div>
              <form className="smart-transfer-form" onSubmit={(event) => void submitSmartTransfer(event)}>
                <label htmlFor="smart-transfer-input">描述你要办理的转账</label>
                <textarea id="smart-transfer-input" rows={2} maxLength={500} placeholder="例如：转给林悦 300 元，备注房租" value={smartTransferInput} onChange={(event) => setSmartTransferInput(event.target.value)} />
                <div className="smart-transfer-form__footer"><span>姓名或完整手机号均可；不会直接扣款。</span><button type="submit" disabled={busy || !smartTransferInput.trim()}>{busy ? '正在理解…' : '理解并生成计划 ↗'}</button></div>
              </form>
              <div className="smart-examples"><span>试试</span><button type="button" onClick={() => setSmartTransferInput('明天晚上8点给林悦转300元，备注房租')}>预约明晚转账</button><button type="button" onClick={() => setSmartTransferInput('转给林悦 300 元，备注房租')}>立即转账</button><button type="button" onClick={() => setSmartTransferInput('明天转给王明100元，备注聚餐')}>补齐预约信息</button></div>
              {smartTransferReply && <div className="smart-transfer-result" role="status"><p>{smartTransferReply.message}</p><Provenance reply={smartTransferReply} />{smartTransferReply.choices && <div className="recipient-choices">{smartTransferReply.choices.map((choice) => <button type="button" key={choice.id} disabled={busy} onClick={() => void chooseSmartRecipient(choice.id)}>{choice.name} · {choice.phone}<span>选择此人 ↗</span></button>)}</div>}{smartTransferReply.pending_action && <ActionCard key={smartTransferReply.pending_action.id} action={smartTransferReply.pending_action} onConfirm={confirmAction} busy={busy} completed={completedActions.includes(smartTransferReply.pending_action.id)} />}</div>}
              {smartTransferNotice && <p className="page-notice" role="status">{smartTransferNotice}</p>}
              {smartTransferReply?.aa_draft && <button type="button" className="aa-quiet aa-chat-entry" onClick={() => openAa({ reply: smartTransferReply })}>核对 AA 分摊草稿 ↗</button>}
            </section>
            <SchedulePanel sessionId={sessionId} revision={scheduleRevision} onChanged={() => void refreshOverview()} />
            <details className="manual-transfer"><summary>手动填写转账信息 <span>备选方式</span></summary><div className="manual-transfer__content">
                <form className="transfer-form" onSubmit={(event) => void prepareDirectTransfer(event)}>
                  <label>收款人<select aria-label="选择收款人" required value={transferContactId} onChange={(event) => { setTransferContactId(event.target.value); setTransferAction(null); setTransferNotice('') }}><option value="">请选择收款人</option>{contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.name} · {contact.phone_masked}</option>)}</select></label>
                  <label>转账金额 <small>人民币</small><input aria-label="转账金额" required inputMode="decimal" placeholder="例如 300.00" value={transferAmount} onChange={(event) => { setTransferAmount(event.target.value); setTransferAction(null); setTransferNotice('') }} /></label>
                  <label>转账备注 <small>选填</small><input aria-label="转账备注" maxLength={100} placeholder="例如 房租" value={transferNote} onChange={(event) => { setTransferNote(event.target.value); setTransferAction(null); setTransferNotice('') }} /></label>
                  <button className="page-primary" type="submit" disabled={busy || !transferContactId || !transferAmount.trim()}>{busy ? '正在核验…' : '生成转账计划'} <span>↗</span></button>
                </form>
                {transferNotice && <p className="page-notice" role="status">{transferNotice}</p>}
                {transferAction && <div className="page-action"><ActionCard key={transferAction.id} action={transferAction} onConfirm={confirmAction} busy={busy} completed={completedActions.includes(transferAction.id)} /></div>}
              </div></details>
            </>}
          </>}

          {view === 'bills' && <>
            <div className="page-header"><span>消费洞察 · 来源可追溯</span><h1>账单分析</h1><p>从模拟交易账本计算支出，按分类展示，并标明需要人工复核的金额。</p></div>
            <div className="bill-toolbar"><div className="period-tabs" role="group" aria-label="账单期间">{(['本月', '上个月', '今年', '去年'] as BillPeriod[]).map((period) => <button type="button" key={period} aria-pressed={billPeriod === period} className={billPeriod === period ? 'period-tabs__active' : ''} onClick={() => { if (period === billPeriod) return; setBillReport(null); setSelectedTransaction(null); setBillPeriod(period); setError('') }}>{period}</button>)}</div><div className="bill-export" aria-label="导出账单"><a href={`/api/bills/export?period=${encodeURIComponent(billPeriod)}&format=csv`} download>导出 CSV</a><a href={`/api/bills/export?period=${encodeURIComponent(billPeriod)}&format=json`} download>导出 JSON</a><button type="button" disabled={!billReport} onClick={() => window.print()}>打印 / PDF</button></div></div>
            {billReport ? <>
              <div className="chart-switch" role="group" aria-label="图表呈现方式">{([['bars', '分类对比'], ['donut', '占比环图'], ['trend', '日期趋势']] as [ChartView, string][]).map(([chart, label]) => <button type="button" key={chart} aria-pressed={billChart === chart} className={billChart === chart ? 'chart-switch__active' : ''} onClick={() => setBillChart(chart)}>{label}</button>)}</div>
              <ReportCard report={billReport} view={billChart} />
              <section className="page-card ledger-card"><div className="page-card__head"><span>↗</span><div><h2>构成这份报告的交易</h2><p>统计范围与上方报告一致，可逐笔核对。</p></div></div>
                {billReport.transactions.length ? <div className="ledger-list">{billReport.transactions.map((tx) => <button type="button" className="ledger-row" key={tx.id} onClick={() => setSelectedTransaction(tx)} aria-label={`查看 ${tx.posted_on} ${tx.counterparty} ${currency(tx.amount_yuan)} 的交易明细`}><span>{tx.posted_on}</span><strong>{tx.counterparty}</strong><small>{tx.category}</small><b>−{currency(tx.amount_yuan)}</b><i aria-hidden="true">↗</i></button>)}</div> : <p className="empty-note">这一期间没有支出记录。</p>}
              </section>
            </> : !error && <p className="loading-note">正在读取账本并计算报告…</p>}
            <dialog className="transaction-dialog" ref={transactionDialog} onClose={() => setSelectedTransaction(null)} aria-label="交易明细">{selectedTransaction && <div><div className="transaction-dialog__head"><div><small>模拟账本 · 交易明细</small><h2>{selectedTransaction.counterparty}</h2></div><button type="button" onClick={() => transactionDialog.current?.close()} aria-label="关闭交易明细">×</button></div><strong className="transaction-dialog__amount">−{currency(selectedTransaction.amount_yuan)}</strong><dl><div><dt>交易日期</dt><dd>{selectedTransaction.posted_on}</dd></div><div><dt>交易分类</dt><dd>{selectedTransaction.category}</dd></div><div><dt>备注</dt><dd>{selectedTransaction.note || '无'}</dd></div><div><dt>交易编号</dt><dd>{selectedTransaction.id}</dd></div><div><dt>归属账单</dt><dd>{billReport?.period}</dd></div></dl>{billReport?.alerts.find((alert) => alert.transaction_id === selectedTransaction.id) && <p className="transaction-dialog__alert">规则复核：{billReport.alerts.find((alert) => alert.transaction_id === selectedTransaction.id)?.reason}</p>}<p className="transaction-dialog__foot">金额与分类来自模拟交易账本，可在当前报告中核对。</p><button type="button" className="aa-quiet aa-bill-entry" onClick={() => { const source = selectedTransaction; transactionDialog.current?.close(); openAa({ source }) }}>用这笔支出发起 AA 分摊 ↗</button></div>}</dialog>
          </>}

          {view === 'subscriptions' && <>
            <div className="page-header"><span>周期扣费 · 协议可核对</span><h1>订阅管理</h1><p>对照连续扣费记录与代扣协议，查看续费日；取消前先确认具体协议。</p></div>
            <div className="subscription-grid">{overview?.subscriptions.map((sub) => {
              const signal = overview.subscription_signals.find((item) => item.merchant === sub.merchant)
              return <article className="subscription-card" key={sub.id}><div className="subscription-card__top"><span className="subscription-card__icon">◎</span><span className={`subscription-state ${sub.status === 'active' ? '' : 'subscription-state--off'}`}>{sub.status === 'active' ? '生效中' : '已取消'}</span></div><h2>{sub.merchant}</h2><p className="subscription-card__amount">{currency(sub.amount_yuan)} <small>/ 期</small></p><div className="subscription-card__facts"><span>下次扣费</span><strong>{sub.status === 'active' ? sub.renewal_on : '已停止'}</strong></div><div className="subscription-card__evidence">{signal ? `连续两个月有扣费 · 证据 ${signal.evidence_ids.join('、')}` : '暂无连续扣费证据'}{signal?.reminder && sub.status === 'active' ? ` · ${signal.days_until_renewal} 天后续费` : ''}</div><button type="button" className="page-secondary" disabled={busy || sub.status !== 'active'} onClick={() => void prepareDirectCancel(sub.id)}>{sub.status === 'active' ? '查看取消计划' : '协议已取消'}</button></article>
            })}</div>
            {cancelNotice && <p className="page-notice" role="status">{cancelNotice}</p>}
            {cancelAction && <div className="page-action"><ActionCard key={cancelAction.id} action={cancelAction} onConfirm={confirmAction} busy={busy} completed={completedActions.includes(cancelAction.id)} /></div>}
          </>}
        </section>}
      </main>

      {showInsights && <aside className="insight-column" aria-label="账户概览">
        <section className="account-panel"><div className="panel-heading"><span>日常账户</span><span className="small-dot" /></div><p>当前余额</p><strong>{overview ? currency(overview.account.balance_yuan) : '—'}</strong><small>模拟账户 · 人民币</small></section>
        <section className="rail-panel"><h3>执行边界</h3><div className="policy-row"><span className="policy-indicator policy-indicator--green" /><div><strong>查询直接执行</strong><p>余额、账单与订阅查询</p></div></div><div className="policy-row"><span className="policy-indicator policy-indicator--amber" /><div><strong>变更先确认</strong><p>小额转账、取消代扣</p></div></div><div className="policy-row"><span className="policy-indicator policy-indicator--red" /><div><strong>高风险强验证</strong><p>日累计超 ¥1,000 的转账</p></div></div></section>
        <section className="rail-panel"><div className="rail-panel__title"><h3>最近交易</h3><span>模拟账本</span></div><div className="transactions">{overview?.transactions.slice(0, 4).map((tx) => <div className="transaction" key={tx.id}><span className="transaction__icon">{tx.direction === 'in' ? '↙' : '↗'}</span><div><strong>{tx.counterparty}</strong><small>{tx.posted_on} · {tx.category}</small></div><b className={tx.direction === 'in' ? 'positive' : ''}>{tx.direction === 'in' ? '+' : '−'}{currency(tx.amount_yuan)}</b></div>)}</div></section>
        <section className="rail-panel"><div className="rail-panel__title"><h3>订阅线索</h3><span>连续扣费识别</span></div>{overview?.subscription_signals.map((signal) => <div className="subscription-signal" key={signal.merchant}><div><strong>{signal.merchant}</strong><small>{signal.evidence_ids.length} 笔历史扣费 · 最近 {currency(signal.last_amount_yuan)}</small></div>{signal.reminder && <span>{signal.days_until_renewal} 天后续费</span>}</div>)}</section>
        <section className="rail-panel audit-panel"><div className="rail-panel__title"><h3>操作记录</h3><span>可追溯</span></div>{overview?.audit.slice(0, 4).map((event, index) => <div className="audit-row" key={`${event.at}-${index}`}><i /><span>{eventLabels[event.event] || event.event}</span></div>)}</section>
      </aside>}
    </div>
  )
}

export default App
