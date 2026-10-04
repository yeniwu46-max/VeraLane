import { useEffect, useRef, useState } from 'react'
import { formatBankTime } from './bankTime'
import { aaCents, aaMoney, aaRequest, aaStatusLabels, type AaCollection } from './aaApi'
import { OperationConfirm } from './OperationConfirm'
import type { Operation } from './bankingApi'

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
  const [refundAmounts, setRefundAmounts] = useState<Record<string, string>>({})
  const [refundAction, setRefundAction] = useState<Operation | null>(null)
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
  async function prepareRefund(requestId: string, sourceTransactionId: string) {
    const amount = refundAmounts[sourceTransactionId]?.trim()
    if (busy || !collection || !amount || !aaCents(amount) || aaCents(amount)! <= 0) return
    setBusy(true); setError(''); setNotice(''); setRefundAction(null)
    try {
      const result = await aaRequest<{ pending_action: Operation }>(`/api/aa/requests/${requestId}/refund/prepare`, {
        session_id: sessionId, source_transaction_id: sourceTransactionId, amount_yuan: amount,
      })
      if (result.pending_action.type !== 'aa_refund') throw new Error('没有收到退款确认计划，请刷新收款单。')
      setRefundAction(result.pending_action)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '退款预览失败') }
    finally { setBusy(false) }
  }
  async function refundCompleted(result: Record<string, unknown>) {
    setRefundAction(null)
    setNotice(typeof result.message === 'string' ? result.message : '退款已处理，请核对模拟流水。')
    if (!collection) return
    try {
      const latest = await aaRequest<AaCollection>(`/api/aa/collections/${collection.id}?session_id=${encodeURIComponent(sessionId)}`)
      onUpdated(latest)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '退款已完成，但收款单刷新失败；请重新打开单据核对。') }
  }
  const canClose = collection?.status === 'pending' || collection?.status === 'partial'
  return <dialog className="transaction-dialog aa-dialog" ref={dialog} aria-label="AA收款单明细" onClose={() => { setConfirmClose(false); setError(''); setNotice(''); onClose() }}>
    {collection && <div>
      <div className="transaction-dialog__head"><div><small>模拟银行 · AA 收款回执</small><h2>{collection.note || 'AA收款'}</h2></div><button type="button" onClick={() => dialog.current?.close()} aria-label="关闭AA收款明细">×</button></div>
      <div className="aa-receipt-summary"><div><span>已到账总额 / 应收</span><strong>{aaMoney(collection.received_yuan)} <small>/ {aaMoney(collection.receivable_yuan)}</small></strong><small>已退款 {aaMoney(collection.refunded_yuan)} · 净回款 {aaMoney(collection.net_received_yuan)}</small></div><span className={`aa-status aa-status--${collection.status}`}>{aaStatusLabels[collection.status]}</span></div>
      <dl><div><dt>垫付总额</dt><dd>{aaMoney(collection.total_yuan)}</dd></div><div><dt>本人承担</dt><dd>{aaMoney(collection.self_yuan)}</dd></div><div><dt>{collection.status === 'closed' ? '未收金额' : '待收金额'}</dt><dd>{aaMoney(collection.outstanding_yuan)}</dd></div><div><dt>分摊方式</dt><dd>{collection.allocation_method === 'proportional' ? `按比例 · ${collection.participants.map((row) => collection.share_ratios?.[row.id] ?? 0).join(' : ')}` : collection.allocation_method === 'manual' ? '手动份额' : collection.allocation_method === 'equal' ? '均分' : '既有收款单'}</dd></div><div><dt>当前净垫付</dt><dd>{aaMoney(collection.net_advance_yuan)}<small className="aa-dl-note">垫付总额 − 净回款</small></dd></div><div><dt>金额来源</dt><dd>{collection.source_transaction ? `${collection.source_transaction.counterparty} · ${collection.source_transaction.posted_on}` : '用户填写的垫付金额'}{collection.source_transaction && <small className="aa-dl-note">{collection.source_transaction.id}</small>}</dd></div><div><dt>收款单编号</dt><dd>{collection.id}</dd></div><div><dt>创建时间</dt><dd>{formatBankTime(collection.created_at)}（实际时间）</dd></div>{collection.closed_at && <div><dt>关闭时间</dt><dd>{formatBankTime(collection.closed_at)}（演示时间）</dd></div>}</dl>
      <section className="aa-receipts" aria-label="逐人分摊与到账记录"><h3>参与人明细</h3>{collection.participants.map((row) => <article className="aa-receipt-row" key={row.id}><div><span><strong>{row.id === 'self' ? '我（本人）' : row.name}</strong><small>{row.phone_masked}</small></span><strong>{aaMoney(row.amount_yuan)}</strong><span className={`aa-status aa-status--${row.status}`}>{participantLabels[row.status]}</span></div>{row.request_id && <p>到账 {aaMoney(row.received_yuan)} · 已退 {aaMoney(row.refunded_yuan)} · 净回款 {aaMoney(row.net_received_yuan)} · 待收 {aaMoney(row.outstanding_yuan)}</p>}{row.payments.map(payment => <div className="aa-payment-receipt" key={payment.transaction_id}><p>到账 {aaMoney(payment.amount_yuan)} · 已退 {aaMoney(payment.refunded_yuan)} · 净回款 {aaMoney(payment.net_received_yuan)} · {formatBankTime(payment.paid_at)} · 交易 {payment.transaction_id}</p>{payment.refunds.map(refund => <small className="aa-dl-note" key={refund.transaction_id}>退款 {aaMoney(refund.amount_yuan)} · {formatBankTime(refund.refunded_at)} · 交易 {refund.transaction_id}</small>)}{demoEnabled && row.request_id && row.status === 'paid' && Number(payment.refundable_yuan) > 0 && <div className="aa-refund-controls"><label>退回本笔回款<input inputMode="decimal" placeholder={`最多 ${aaMoney(payment.refundable_yuan)}`} value={refundAmounts[payment.transaction_id] || ''} disabled={busy} onChange={event => setRefundAmounts(current => ({ ...current, [payment.transaction_id]: event.target.value }))} /></label><button type="button" className="aa-quiet" disabled={busy || !aaCents(refundAmounts[payment.transaction_id] || '') || (aaCents(refundAmounts[payment.transaction_id] || '') || 0) <= 0} onClick={() => void prepareRefund(row.request_id!, payment.transaction_id)}>预览退款</button></div>}</div>)}{demoEnabled && canClose && row.request_id && ['pending', 'partial'].includes(row.status) && <div className="aa-installment"><label>分次模拟回款<input inputMode="decimal" placeholder={`最多 ${aaMoney(row.outstanding_yuan)}`} value={installments[row.request_id]?.amount || ''} disabled={busy} onChange={event => setInstallments(current => ({ ...current, [row.request_id!]: { amount: event.target.value, idempotencyKey: crypto.randomUUID() } }))} /></label><button type="button" className="aa-quiet" disabled={busy || !aaCents(installments[row.request_id]?.amount || '') || (aaCents(installments[row.request_id]?.amount || '') || 0) <= 0} onClick={() => void payInstallment(row.request_id!)}>确认分次到账</button></div>}</article>)}</section>
      {error && <p className="error-banner" role="alert">{error}</p>}
      {notice && <p className="aa-notice" role="status">{notice}</p>}
      {demoEnabled && canClose && <details className="aa-demo"><summary>演示：模拟参与人付清余额</summary><p>在本地模拟环境一次性记录该参与人尚未支付的余额。不会扣除联系人真实资金。</p><div>{collection.participants.filter((row) => ['pending', 'partial'].includes(row.status) && row.request_id).map((row) => <button type="button" className="aa-quiet" disabled={busy} key={row.id} onClick={() => void pay(row.request_id!)}>模拟 {row.name}（{row.phone_masked}）付清 {aaMoney(row.outstanding_yuan)}</button>)}</div></details>}
      <div className="aa-dialog-actions">{canClose && (confirmClose ? <div className="aa-close-confirm"><p>关闭后，剩余参与人不能继续付款；已到账金额与回执会保留。</p><button type="button" className="aa-danger" disabled={busy} onClick={() => void closeCollection()}>{busy ? '正在处理…' : '确认关闭剩余请求'}</button><button type="button" className="aa-quiet" disabled={busy} onClick={() => setConfirmClose(false)}>继续收款</button></div> : <button type="button" className="aa-quiet" disabled={busy} onClick={() => setConfirmClose(true)}>关闭剩余收款请求</button>)}</div>
      {refundAction && <OperationConfirm key={refundAction.id} action={refundAction} sessionId={sessionId} title="核对 AA 回款退款" onDone={refundCompleted}><dl><div><dt>退回给</dt><dd>{String(refundAction.details.recipient_name)}（{String(refundAction.details.recipient_phone_masked)}）</dd></div><div><dt>退款金额</dt><dd>{aaMoney(String(refundAction.details.amount_yuan))}</dd></div><div><dt>原到账流水</dt><dd>{String(refundAction.details.source_transaction_id)}</dd></div><div><dt>本次授权时可退余额</dt><dd>{aaMoney(String(refundAction.details.remaining_refundable_yuan))}</dd></div></dl><p className="aa-caption">退款会扣减当前可用余额，已预留资金不可动用。它只退回此笔模拟 AA 回款，不会撤销原始消费。</p></OperationConfirm>}
      <p className="transaction-dialog__foot">建单不会计入收入。回款独立入账；退款记为新的支出，原回款与消费流水均保留。</p>
    </div>}
  </dialog>
}

