import { useEffect, useRef, useState } from 'react'
import { formatBankTime } from './bankTime'
import { aaMoney, aaRequest, aaStatusLabels, type AaCollection } from './aaApi'

const participantLabels = { self: '本人承担', not_required: '无需付款', pending: '待付款', paid: '已到账', closed: '已关闭' }

export function AaCollectionDialog({ collection, sessionId, demoEnabled, onClose, onUpdated }: {
  collection: AaCollection | null; sessionId: string; demoEnabled: boolean; onClose: () => void; onUpdated: (collection: AaCollection) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [busy, setBusy] = useState(false)
  const [confirmClose, setConfirmClose] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
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
      setNotice(receipt?.status === 'paid' ? `${receipt.name}的 ${aaMoney(receipt.amount_yuan)} 已模拟到账，可核对下方交易编号。` : '请求已返回，请核对当前收款状态。')
    } catch (cause) { setError(cause instanceof Error ? cause.message : '模拟付款失败') }
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
      <section className="aa-receipts" aria-label="逐人分摊与到账记录"><h3>参与人明细</h3>{collection.participants.map((row) => <article className="aa-receipt-row" key={row.id}><div><span><strong>{row.id === 'self' ? '我（本人）' : row.name}</strong><small>{row.phone_masked}</small></span><strong>{aaMoney(row.amount_yuan)}</strong><span className={`aa-status aa-status--${row.status}`}>{participantLabels[row.status]}</span></div>{row.paid_at && <p>到账时间：{formatBankTime(row.paid_at)}（演示时间）</p>}{row.transaction_id && <p>交易编号：{row.transaction_id}</p>}</article>)}</section>
      {error && <p className="error-banner" role="alert">{error}</p>}
      {notice && <p className="aa-notice" role="status">{notice}</p>}
      {demoEnabled && canClose && <details className="aa-demo"><summary>演示：模拟参与人付款</summary><p>在本地模拟环境记录已确认金额的回款，计入模拟余额。不会扣除联系人真实资金。</p><div>{collection.participants.filter((row) => row.status === 'pending' && row.request_id).map((row) => <button type="button" className="aa-quiet" disabled={busy} key={row.id} onClick={() => void pay(row.request_id!)}>模拟 {row.name}（{row.phone_masked}）支付 {aaMoney(row.amount_yuan)}</button>)}</div></details>}
      <div className="aa-dialog-actions">{canClose && (confirmClose ? <div className="aa-close-confirm"><p>关闭后，剩余参与人不能继续付款；已到账金额与回执会保留。</p><button type="button" className="aa-danger" disabled={busy} onClick={() => void closeCollection()}>{busy ? '正在处理…' : '确认关闭剩余请求'}</button><button type="button" className="aa-quiet" disabled={busy} onClick={() => setConfirmClose(false)}>继续收款</button></div> : <button type="button" className="aa-quiet" disabled={busy} onClick={() => setConfirmClose(true)}>关闭剩余收款请求</button>)}</div>
      <p className="transaction-dialog__foot">建单不会计入收入。回款独立入账，原消费支出保留；净垫付用于核对个人承担。</p>
    </div>}
  </dialog>
}

