import { useEffect, useRef, useState } from 'react'
import { formatBankTime } from './bankTime'
import { aaCents, aaMoney, aaRequest, aaStatusLabels, type AaCollection } from './aaApi'

const participantLabels = { self: '本人承担', not_required: '无需付款', pending: '待付款', partial: '部分到账', paid: '已到账', closed: '已关闭' }
type InstallmentDraft = { amount: string; idempotencyKey: string }

export function AaCollectionDialog({ collection, sessionId, demoEnabled, onClose, onUpdated }: {
  collection: AaCollection | null; sessionId: string; demoEnabled: boolean; onClose: () => void; onUpdated: (collection: AaCollection) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [busy, setBusy] = useState(false)
  const [confirmClose, setConfirmClose] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [installments, setInstallments] = useState<Record<string, InstallmentDraft>>({})
  useEffect(() => {
    if (collection && dialog.current && !dialog.current.open) dialog.current.showModal()
  }, [collection])
  async function pay(requestId: string) {
    if (busy || !collection) return
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await aaRequest<AaCollection>(`/api/aa/requests/${requestId}/simulate-payment`, { session_id: sessionId })
      onUpdated(next)
      const receipt = next.participants.find((row) => row.request_id === requestId)
      const latest = receipt?.payments.at(-1)
      setNotice(receipt?.status === 'paid' ? `${receipt.name}本次模拟到账 ${aaMoney(latest?.amount_yuan || receipt.amount_yuan)}；累计到账 ${aaMoney(receipt.received_yuan)}，可核对逐笔交易编号。` : '请求已返回，请核对当前收款状态。')
    } catch (cause) { setError(cause instanceof Error ? cause.message : '模拟付款失败') }
    finally { setBusy(false) }
  }
  async function payInstallment(requestId: string) {
    const draft = installments[requestId]
    if (busy || !collection || !draft || !aaCents(draft.amount) || aaCents(draft.amount)! <= 0) return
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await aaRequest<AaCollection>(`/api/aa/requests/${requestId}/installments`, {
        session_id: sessionId, amount_yuan: draft.amount.trim(), idempotency_key: draft.idempotencyKey,
      })
      onUpdated(next)
      const receipt = next.participants.find(row => row.request_id === requestId)
      setNotice(receipt ? `${receipt.name} 本次模拟回款 ${aaMoney(draft.amount)}；累计到账 ${aaMoney(receipt.received_yuan)}，尚余 ${aaMoney(receipt.outstanding_yuan)}。` : '分次回款已记入，请核对当前状态。')
      setInstallments(current => ({ ...current, [requestId]: { amount: '', idempotencyKey: crypto.randomUUID() } }))
    } catch (cause) { setError(cause instanceof Error ? cause.message : '分次模拟回款失败') }
    finally { setBusy(false) }
  }
  async function closeCollection() {
    if (busy || !collection) return
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await aaRequest<AaCollection>(`/api/aa/collections/${collection.id}/close`, { session_id: sessionId })
      onUpdated(next); setConfirmClose(false)
      setNotice(next.status === 'closed' ? '剩余收款请求已关闭，已到账记录予以保留。' : `当前收款单${aaStatusLabels[next.status]}，请核对明细。`)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '关闭收款单失败') }
    finally { setBusy(false) }
  }
  const canClose = collection?.status === 'pending' || collection?.status === 'partial'
  return <dialog className="transaction-dialog aa-dialog" ref={dialog} aria-label="AA收款单明细" onClose={() => { setConfirmClose(false); setError(''); setNotice(''); onClose() }}>
    {collection && <div>
      <div className="transaction-dialog__head"><div><small>模拟银行 · AA 收款回执</small><h2>{collection.note || 'AA收款'}</h2></div><button type="button" onClick={() => dialog.current?.close()} aria-label="关闭AA收款明细">×</button></div>
      <div className="aa-receipt-summary"><div><span>已收款 / 应收款</span><strong>{aaMoney(collection.received_yuan)} <small>/ {aaMoney(collection.receivable_yuan)}</small></strong></div><span className={`aa-status aa-status--${collection.status}`}>{aaStatusLabels[collection.status]}</span></div>
      <dl><div><dt>垫付总额</dt><dd>{aaMoney(collection.total_yuan)}</dd></div><div><dt>本人承担</dt><dd>{aaMoney(collection.self_yuan)}</dd></div><div><dt>{collection.status === 'closed' ? '未收金额' : '待收金额'}</dt><dd>{aaMoney(collection.outstanding_yuan)}</dd></div><div><dt>当前净垫付</dt><dd>{aaMoney(collection.net_advance_yuan)}<small className="aa-dl-note">垫付总额 − 已到账回款</small></dd></div><div><dt>金额来源</dt><dd>{collection.source_transaction ? `${collection.source_transaction.counterparty} · ${collection.source_transaction.posted_on}` : '用户填写的垫付金额'}{collection.source_transaction && <small className="aa-dl-note">{collection.source_transaction.id}</small>}</dd></div><div><dt>收款单编号</dt><dd>{collection.id}</dd></div><div><dt>创建时间</dt><dd>{formatBankTime(collection.created_at)}（实际时间）</dd></div>{collection.closed_at && <div><dt>关闭时间</dt><dd>{formatBankTime(collection.closed_at)}（演示时间）</dd></div>}</dl>
      <section className="aa-receipts" aria-label="逐人分摊与到账记录"><h3>参与人明细</h3>{collection.participants.map((row) => <article className="aa-receipt-row" key={row.id}><div><span><strong>{row.id === 'self' ? '我（本人）' : row.name}</strong><small>{row.phone_masked}</small></span><strong>{aaMoney(row.amount_yuan)}</strong><span className={`aa-status aa-status--${row.status}`}>{participantLabels[row.status]}</span></div>{row.request_id && <p>已收 {aaMoney(row.received_yuan)} · 待收 {aaMoney(row.outstanding_yuan)}</p>}{row.payments.map(payment => <p key={payment.transaction_id}>到账 {aaMoney(payment.amount_yuan)} · {formatBankTime(payment.paid_at)} · 交易 {payment.transaction_id}</p>)}{demoEnabled && canClose && row.request_id && ['pending', 'partial'].includes(row.status) && <div className="aa-installment"><label>分次模拟回款<input inputMode="decimal" placeholder={`最多 ${aaMoney(row.outstanding_yuan)}`} value={installments[row.request_id]?.amount || ''} disabled={busy} onChange={event => setInstallments(current => ({ ...current, [row.request_id!]: { amount: event.target.value, idempotencyKey: crypto.randomUUID() } }))} /></label><button type="button" className="aa-quiet" disabled={busy || !aaCents(installments[row.request_id]?.amount || '') || (aaCents(installments[row.request_id]?.amount || '') || 0) <= 0} onClick={() => void payInstallment(row.request_id!)}>确认分次到账</button></div>}</article>)}</section>
      {error && <p className="error-banner" role="alert">{error}</p>}
      {notice && <p className="aa-notice" role="status">{notice}</p>}
      {demoEnabled && canClose && <details className="aa-demo"><summary>演示：模拟参与人付清余额</summary><p>在本地模拟环境一次性记录该参与人尚未支付的余额。不会扣除联系人真实资金。</p><div>{collection.participants.filter((row) => ['pending', 'partial'].includes(row.status) && row.request_id).map((row) => <button type="button" className="aa-quiet" disabled={busy} key={row.id} onClick={() => void pay(row.request_id!)}>模拟 {row.name}（{row.phone_masked}）付清 {aaMoney(row.outstanding_yuan)}</button>)}</div></details>}
      <div className="aa-dialog-actions">{canClose && (confirmClose ? <div className="aa-close-confirm"><p>关闭后，剩余参与人不能继续付款；已到账金额与回执会保留。</p><button type="button" className="aa-danger" disabled={busy} onClick={() => void closeCollection()}>{busy ? '正在处理…' : '确认关闭剩余请求'}</button><button type="button" className="aa-quiet" disabled={busy} onClick={() => setConfirmClose(false)}>继续收款</button></div> : <button type="button" className="aa-quiet" disabled={busy} onClick={() => setConfirmClose(true)}>关闭剩余收款请求</button>)}</div>
      <p className="transaction-dialog__foot">建单不会计入收入。回款独立入账，原消费支出保留；净垫付用于核对个人承担。</p>
    </div>}
  </dialog>
}

