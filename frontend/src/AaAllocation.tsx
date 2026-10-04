import { aaCents, aaMoney, type AaAction, type AaPreview } from './aaApi'

export function AaAllocation({ preview, shares, onChange, onEqualize, onPrepare, onConfirm, action, busy, reviewed, requiresCustom, onReviewed }: {
  preview: AaPreview; shares: Record<string, string>; onChange: (id: string, amount: string) => void
  onEqualize: () => void; onPrepare: () => void; onConfirm: () => void; action: AaAction | null
  busy: boolean; reviewed: boolean; requiresCustom: boolean; onReviewed: (value: boolean) => void
}) {
  const values = preview.participants.map((row) => aaCents(shares[row.id] || ''))
  const valid = values.every((value) => value !== null)
  const total = aaCents(preview.total_yuan) || 0
  const sum = values.reduce<number>((amount, value) => amount + (value || 0), 0)
  const difference = total - sum
  const self = aaCents(shares.self || '')
  const positiveRequest = preview.participants.some((row) => row.id !== 'self' && (aaCents(shares[row.id] || '') || 0) > 0)
  const customSharesChanged = preview.participants.some((row) => aaCents(shares[row.id] || '') !== aaCents(row.amount_yuan))
  const balanced = valid && difference === 0 && (positiveRequest || self === total)
  const allocationLabel = preview.allocation_method === 'itemized' ? '按菜品分摊' : preview.allocation_method === 'proportional' ? '按比例分摊' : preview.allocation_method === 'manual' ? '手动份额' : '均分'
  const customIntentSatisfied = preview.allocation_method === 'itemized' || customSharesChanged || (preview.allocation_method === 'proportional' && new Set(Object.values(preview.share_ratios || {})).size > 1)
  return <section className="aa-allocation" aria-labelledby="aa-allocation-heading">
    <header className="aa-section-head"><div><h3 id="aa-allocation-heading">核对每个人的金额</h3><p>{allocationLabel}{preview.allocation_method !== 'manual' ? ' · 不足一分按稳定顺序分配' : ''}{preview.allocation_method === 'itemized' ? '；按菜品计算的金额锁定，如需改动请返回菜品明细。' : '；你也可以手动调整。'}</p></div>{preview.allocation_method !== 'itemized' && <button type="button" className="aa-quiet" disabled={busy} onClick={onEqualize}>恢复均分</button>}</header>
    {preview.allocation_method === 'itemized' && <section className="aa-itemized-preview" aria-label="菜品分摊核算结果"><h4>菜品核算</h4>{preview.itemized_items?.map((item, index) => <article key={`${item.description}-${index}`}><div><strong>{item.description}</strong><small>{item.participant_ids.map((id) => preview.participants.find((person) => person.id === id)?.name || '未知参与人').join('、')}</small></div><strong>{aaMoney(item.amount_yuan)}</strong><ul>{item.allocations.map((allocation) => <li key={allocation.participant_id}>{allocation.name} {aaMoney(allocation.amount_yuan)}</li>)}</ul></article>)}</section>}
    <div className="aa-share-table">
      {preview.participants.map((row) => <label className="aa-share-row" key={row.id}>
        <span><strong>{row.id === 'self' ? '我（本人）' : row.name}</strong><small>{row.id === 'self' ? '本人承担，不产生收款请求' : row.phone_masked}{preview.share_ratios && ` · 比例 ${preview.share_ratios[row.id]}`}{row.rounding_extra && shares[row.id] === row.amount_yuan ? ' · 余数 +0.01 元' : ''}</small></span>
        <span className="aa-money-field"><span aria-hidden="true">¥</span><input aria-label={`${row.id === 'self' ? '本人' : `${row.name} ${row.phone_masked}`}分摊金额`} inputMode="decimal" maxLength={15} value={shares[row.id] || ''} disabled={busy || preview.allocation_method === 'itemized'} onChange={(event) => onChange(row.id, event.target.value)} /></span>
      </label>)}
    </div>
    <div className="aa-balance-check" role="status">
      <span>总额 <strong>{aaMoney(preview.total_yuan)}</strong></span>
      {valid && self !== null && <span>应收他人 <strong>{aaMoney(((sum - self) / 100).toFixed(2))}</strong></span>}
      <span className={!balanced ? 'aa-warning-text' : ''}>{!valid ? '请输入非负金额，最多两位小数' : difference > 0 ? `还需分配 ${aaMoney((difference / 100).toFixed(2))}` : difference < 0 ? `超出 ${aaMoney((-difference / 100).toFixed(2))}` : !positiveRequest && self === total ? '本人已承担全部金额，无需向他人收款' : !positiveRequest ? '至少需要一位参与人付款' : '分摊合计与总额一致'}</span>
    </div>
    {requiresCustom && !action && !customIntentSatisfied && <p className="aa-warning-text aa-caption">你提出了非均分需求。请调整比例或逐人金额后再确认。</p>}
    {!action && <button type="button" className="page-primary aa-prepare" disabled={busy || !balanced || (requiresCustom && !customIntentSatisfied)} onClick={onPrepare}>{busy ? '正在核验…' : '生成收款确认单'}</button>}
    {action && <div className="aa-confirm-card">
      <div className="aa-section-head"><h3>确认建立 AA 收款单</h3><span className="tier tier--yellow">需确认</span></div>
      <dl className="aa-confirm-totals"><div><dt>总额</dt><dd>{aaMoney(action.details.total_yuan)}</dd></div><div><dt>本人承担</dt><dd>{aaMoney(action.details.self_yuan)}</dd></div><div><dt>向他人收取</dt><dd>{aaMoney(action.details.receivable_yuan)}</dd></div></dl>
      <p>{action.details.note} · {action.details.source_transaction ? `引用 ${action.details.source_transaction.counterparty} 的原始支出` : '用户填写的垫付金额'} · {action.details.allocation_method === 'itemized' ? '按菜品分摊' : action.details.allocation_method === 'proportional' ? '按比例分摊' : action.details.allocation_method === 'manual' ? '手动份额' : '均分'}</p>
      <label className="review-check"><input type="checkbox" checked={reviewed} disabled={busy} onChange={(event) => onReviewed(event.target.checked)} /><span>我已核对参与人、每人金额和用途，同意建立收款单。</span></label>
      <p className="aa-caption">确认不会扣款或增加余额。若仍有未收款项，将于 {action.details.reminder_on} 生成一次站内提醒，不发送消息。模拟付款到账后才计入余额；确认单 10 分钟内有效。</p>
      <button type="button" className="confirm-button" disabled={busy || !reviewed || action.tier === 'red'} onClick={onConfirm}>{busy ? '正在处理…' : `确认建单 · 应收 ${aaMoney(action.details.receivable_yuan)}`}</button>
    </div>}
  </section>
}
