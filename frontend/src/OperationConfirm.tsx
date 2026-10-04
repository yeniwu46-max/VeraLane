import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ApiError, requestJson, type Operation } from './bankingApi'

export type VerificationResult = { verified: true; expires_at: string }
type Challenge = { challenge_id: string; demo_code: string; expires_at: string }
type VerificationProps = {
  actionId: string; sessionId: string; onVerified: (result: VerificationResult) => void
  onReset?: () => void; onBusyChange?: (busy: boolean) => void; disabled?: boolean
}

function VerificationContent({ actionId, sessionId, onVerified, onReset, onBusyChange, disabled = false }: VerificationProps) {
  const [challenge, setChallenge] = useState<Challenge>()
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [verified, setVerified] = useState(false)
  const [expired, setExpired] = useState(false)
  const [error, setError] = useState('')
  const lock = useRef(false)
  const pending = useRef<AbortController | null>(null)
  const callbacks = useRef({ onReset, onBusyChange })
  useEffect(() => { callbacks.current = { onReset, onBusyChange } }, [onReset, onBusyChange])
  useEffect(() => () => pending.current?.abort(), [])
  useEffect(() => {
    if (!challenge) return
    const timer = setTimeout(() => {
      setExpired(true); setVerified(false); setCode(''); callbacks.current.onReset?.()
    }, Math.max(0, Date.parse(challenge.expires_at) - Date.now()))
    return () => clearTimeout(timer)
  }, [challenge])

  async function run(work: (signal: AbortSignal) => Promise<void>) {
    if (disabled || lock.current) return
    lock.current = true
    const controller = new AbortController()
    pending.current = controller
    setBusy(true); setError(''); callbacks.current.onBusyChange?.(true)
    try { await work(controller.signal) }
    catch (cause) { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '验证请求失败，请重试。') }
    finally {
      lock.current = false
      if (!controller.signal.aborted) { setBusy(false); callbacks.current.onBusyChange?.(false) }
    }
  }
  function generate() {
    if (disabled || lock.current) return
    callbacks.current.onReset?.(); setVerified(false); setExpired(false); setChallenge(undefined); setCode('')
    void run(async signal => {
      const result = await requestJson<Challenge>(`/api/actions/${actionId}/challenge`, { session_id: sessionId }, signal)
      if (!signal.aborted) setChallenge(result)
    })
  }
  function verify() {
    if (!challenge || expired || verified || !/^\d{6}$/.test(code)) return
    void run(async signal => {
      const result = await requestJson<VerificationResult>(`/api/actions/${actionId}/verify`, { session_id: sessionId, challenge_id: challenge.challenge_id, code }, signal)
      if (!signal.aborted) { setVerified(true); setCode(''); onVerified(result) }
    })
  }
  return <div className="operation-verification">
    <p>{verified ? `模拟验证已通过，有效至 ${new Date(challenge!.expires_at).toLocaleTimeString('zh-CN')}。执行时仍会检查业务条件。` : '此操作需要独立模拟验证，验证码仅适用于当前计划。'}</p>
    {expired && <p role="status" className="page-notice">模拟验证已过期，请重新验证并核对确认。</p>}
    <button type="button" className="page-secondary" disabled={busy || disabled} onClick={generate}>{busy ? '正在处理验证…' : verified ? '重新验证当前计划' : challenge ? '重新生成验证码' : '生成模拟验证码'}</button>
    {challenge && !verified && !expired && <>
      <details><summary>查看演示验证码（非真实身份认证）</summary><p>{challenge.demo_code}</p></details>
      <label>输入六位验证码<input aria-label="模拟验证码" inputMode="numeric" autoComplete="off" maxLength={6} disabled={busy || disabled} value={code} onChange={event => setCode(event.target.value)} /></label>
      <button type="button" className="page-secondary" disabled={busy || disabled || !/^\d{6}$/.test(code)} onClick={verify}>验证当前计划</button>
    </>}
    {error && <p role="alert" className="error-banner">{error}</p>}
  </div>
}

export function DemoVerification(props: VerificationProps) {
  return <VerificationContent key={`${props.sessionId}:${props.actionId}`} {...props} />
}

type ConfirmProps = { action: Operation; sessionId: string; title: string; children?: ReactNode; onDone: (result: Record<string, unknown>) => void }

function ConfirmContent({ action, sessionId, title, children, onDone }: ConfirmProps) {
  const [reviewed, setReviewed] = useState(false)
  const [verified, setVerified] = useState(false)
  const [verificationVersion, setVerificationVersion] = useState(0)
  const [verificationBusy, setVerificationBusy] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [completed, setCompleted] = useState(false)
  const [expired, setExpired] = useState(false)
  const [outcomeUnknown, setOutcomeUnknown] = useState(false)
  const lock = useRef(false)
  const done = useRef(false)
  const verification = useRef<VerificationResult | null>(null)
  const pending = useRef<AbortController | null>(null)
  useEffect(() => () => pending.current?.abort(), [])
  useEffect(() => {
    const timer = setTimeout(() => setExpired(true), Math.max(0, Date.parse(action.expires_at) - Date.now()))
    return () => clearTimeout(timer)
  }, [action.expires_at])
  function resetVerification() { verification.current = null; setVerified(false); setReviewed(false) }

  async function confirm(retryOriginal = false) {
    if (lock.current || done.current || verificationBusy) return
    if (retryOriginal ? !outcomeUnknown : (!reviewed || expired || (action.tier === 'red' && !verification.current))) return
    lock.current = true
    const controller = new AbortController()
    pending.current = controller
    setBusy(true); setError('')
    let result: Record<string, unknown> | undefined
    try {
      result = await requestJson<Record<string, unknown>>(`/api/actions/${action.id}/confirm`, { session_id: sessionId }, controller.signal)
      if (controller.signal.aborted) return
      done.current = true; setCompleted(true); setOutcomeUnknown(false)
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : '执行失败，请核对原操作。')
        setOutcomeUnknown(cause instanceof ApiError && cause.outcomeUnknown)
        if (cause instanceof ApiError && ['VERIFICATION_REQUIRED', 'VERIFICATION_EXPIRED', 'VERIFICATION_INVALID'].includes(cause.code)) {
          resetVerification(); setVerificationVersion(value => value + 1)
        }
        if (cause instanceof ApiError && cause.code === 'ACTION_EXPIRED') { resetVerification(); setExpired(true) }
      }
    } finally {
      lock.current = false
      if (!controller.signal.aborted) setBusy(false)
    }
    // Parent refresh failures must not turn an acknowledged commit into a retryable write.
    if (result && !controller.signal.aborted) onDone(result)
  }
  return <section className={`action-card ${action.tier === 'red' ? 'action-card--red' : ''}`}>
    <div className="action-card__head"><h3>{title}</h3><span className={`tier tier--${action.tier}`}>{action.tier === 'red' ? '强验证' : '需确认'}</span></div>
    {children}
    {action.tier === 'red' && !completed && !expired && <DemoVerification key={verificationVersion} actionId={action.id} sessionId={sessionId} disabled={busy || outcomeUnknown} onBusyChange={setVerificationBusy} onReset={resetVerification} onVerified={value => { verification.current = value; setVerified(true); setReviewed(false); setError('') }} />}
    {expired && !completed && !outcomeUnknown && <p className="page-notice" role="status">此操作的确认有效期已结束，请返回表单核对后重新生成操作。</p>}
    <label className="review-check"><input type="checkbox" disabled={busy || verificationBusy || completed || expired || outcomeUnknown} checked={reviewed} onChange={event => setReviewed(event.target.checked)} /><span>我已核对对象、金额与影响，确认执行此模拟操作。</span></label>
    {outcomeUnknown ? <>
      <p className="page-notice" role="status">当前结果尚未确认。重试会使用相同操作编号：已完成时读取原回执，未执行时仍按原授权检查。</p>
      <button type="button" className="confirm-button" disabled={busy || verificationBusy} onClick={() => void confirm(true)}>{busy ? '正在核对原操作…' : '重试原操作并核对结果'}</button>
    </> : <button type="button" className="confirm-button" disabled={!reviewed || busy || verificationBusy || completed || expired || (action.tier === 'red' && !verified)} onClick={() => void confirm()}>{completed ? '已处理' : busy ? '正在执行…' : '确认执行'}</button>}
    {error && <p className="error-banner" role="alert">{error}</p>}
  </section>
}

export function OperationConfirm(props: ConfirmProps) {
  return <ConfirmContent key={`${props.sessionId}:${props.action.id}`} {...props} />
}
