import { useState } from 'react'
import { aaCents, aaMoney, aaRequest, type AaContact } from './aaApi'

type ExpenseDraft = { id: string; payer_id: string; amount_yuan: string; note: string }
type SettlementPreview = {
  note: string; total_yuan: string; expense_count: number; notice: string
  participants: { id: string; name: string; paid_yuan: string; share_yuan: string; net_yuan: string; direction: 'receive' | 'pay' | 'settled' }[]
  expenses: { payer_id: string; payer_name: string; amount_yuan: string; note: string }[]
  transfers: { from_id: string; from_name: string; to_id: string; to_name: string; amount_yuan: string }[]
}
const aaAbsoluteMoney = (value: string) => aaMoney(String(Math.abs(Number(value))))

export function AaSettlementPanel({ sessionId, contacts }: { sessionId: string; contacts: AaContact[] }) {
  const [participantIds, setParticipantIds] = useState<string[]>([])
  const [expenses, setExpenses] = useState<ExpenseDraft[]>([{ id: crypto.randomUUID(), payer_id: 'self', amount_yuan: '', note: '' }])
  const [note, setNote] = useState('多人垫付结算')
  const [preview, setPreview] = useState<SettlementPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const payerOptions = [{ id: 'self', name: '我（本人）' }, ...contacts.filter(person => participantIds.includes(person.id)).map(person => ({ id: person.id, name: person.name }))]
  function invalidate() { setPreview(null); setError('') }
  function toggleParticipant(contactId: string) {
    invalidate()
    const next = participantIds.includes(contactId) ? participantIds.filter(id => id !== contactId) : [...participantIds, contactId]
    setParticipantIds(next)
    setExpenses(rows => rows.map(row => !next.includes(row.payer_id) && row.payer_id !== 'self' ? { ...row, payer_id: 'self' } : row))
  }
  function updateExpense(id: string, field: 'payer_id' | 'amount_yuan' | 'note', value: string) {
    invalidate()
    setExpenses(current => current.map(row => row.id === id ? { ...row, [field]: value } : row))
  }
  async function calculate() {
    if (busy || participantIds.length < 1 || expenses.some(row => !aaCents(row.amount_yuan) || !payerOptions.some(person => person.id === row.payer_id))) return
    setBusy(true); setError(''); setPreview(null)
    try {
      const result = await aaRequest<SettlementPreview>('/api/aa/settlements/preview', {
        session_id: sessionId, contact_ids: participantIds, note,
        expenses: expenses.map(({ payer_id, amount_yuan, note: expenseNote }) => ({ payer_id, amount_yuan: amount_yuan.trim(), note: expenseNote.trim() })),
      })
      setPreview(result)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '结算计算失败') }
    finally { setBusy(false) }
  }
  const canCalculate = participantIds.length >= 1 && participantIds.length <= 7 && expenses.length > 0 && expenses.length <= 100 && expenses.every(row => (aaCents(row.amount_yuan) || 0) > 0 && payerOptions.some(person => person.id === row.payer_id))

  return <section className="aa-editor aa-settlement" aria-labelledby="aa-settlement-heading" aria-busy={busy}>
    <header className="aa-section-head"><div><h2 id="aa-settlement-heading">多人垫付抵消</h2><p>录入本次共同消费和每笔实际付款人，按人头均分后抵消应收应付。</p></div><span className="aa-step">本地计算 · 不发起转账</span></header>
    <label className="aa-settlement-title">结算名称<input value={note} maxLength={100} onChange={event => { setNote(event.target.value); invalidate() }} /></label>
    <fieldset className="aa-settlement-members"><legend>参与人（包含本人，共 2–8 人）</legend><div>{contacts.map(person => <label key={person.id}><input type="checkbox" checked={participantIds.includes(person.id)} onChange={() => toggleParticipant(person.id)} />{person.name} · {person.phone_masked}</label>)}</div></fieldset>
    <div className="aa-section-head aa-expenses-heading"><div><h3>垫付明细</h3><p>金额精确到分；最多 100 笔，总额不超过 ¥100,000。</p></div><button type="button" className="aa-quiet" disabled={busy || expenses.length >= 100} onClick={() => { invalidate(); setExpenses(rows => [...rows, { id: crypto.randomUUID(), payer_id: 'self', amount_yuan: '', note: '' }]) }}>添加一笔</button></div>
    <div className="aa-expense-list">{expenses.map((expense, index) => <div className="aa-expense-row" key={expense.id}><span className="aa-order">{String(index + 1).padStart(2, '0')}</span><label>付款人<select value={expense.payer_id} onChange={event => updateExpense(expense.id, 'payer_id', event.target.value)}>{payerOptions.map(person => <option key={person.id} value={person.id}>{person.name}</option>)}</select></label><label>金额（元）<input inputMode="decimal" value={expense.amount_yuan} placeholder="0.00" onChange={event => updateExpense(expense.id, 'amount_yuan', event.target.value)} /></label><label>用途<input value={expense.note} maxLength={100} placeholder="例如：餐费" onChange={event => updateExpense(expense.id, 'note', event.target.value)} /></label><button type="button" className="aa-icon-button" aria-label={`删除第 ${index + 1} 笔垫付`} disabled={busy || expenses.length === 1} onClick={() => { invalidate(); setExpenses(rows => rows.filter(row => row.id !== expense.id)) }}>×</button></div>)}</div>
    <button type="button" className="aa-calculate" disabled={busy || !canCalculate} onClick={() => void calculate()}>{busy ? '正在计算…' : '计算净额与建议转账'}</button>
    {error && <p className="error-banner" role="alert">{error}</p>}
    {preview && <div className="aa-settlement-result" role="status"><header><div><small>结算预览 · {preview.note}</small><strong>{aaMoney(preview.total_yuan)}</strong></div><span>{preview.expense_count} 笔垫付</span></header><div className="aa-settlement-table">{preview.participants.map(person => <div key={person.id}><strong>{person.name}</strong><span>已付 {aaMoney(person.paid_yuan)} · 应摊 {aaMoney(person.share_yuan)}</span><b className={person.direction === 'receive' ? 'aa-net-positive' : person.direction === 'pay' ? 'aa-net-negative' : ''}>{person.direction === 'receive' ? '应收 ' : person.direction === 'pay' ? '应付 ' : '已平 '} {aaAbsoluteMoney(person.net_yuan)}</b></div>)}</div><h3>建议结算路径</h3>{preview.transfers.length ? <ol>{preview.transfers.map((transfer, index) => <li key={`${transfer.from_id}-${transfer.to_id}-${index}`}><strong>{transfer.from_name}</strong><span>转给</span><strong>{transfer.to_name}</strong><b>{aaMoney(transfer.amount_yuan)}</b></li>)}</ol> : <p>各人垫付正好抵消，无需结算。</p>}<p className="aa-caption">{preview.notice} 建议路径为确定性净额计算；实际付款需另行核对并在转账页确认。</p></div>}
  </section>
}
