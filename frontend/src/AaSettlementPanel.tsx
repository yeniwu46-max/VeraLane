import { useCallback, useEffect, useState } from 'react'
import { aaCents, aaMoney, aaRequest, type AaContact } from './aaApi'
import { OperationConfirm } from './OperationConfirm'
import type { Operation } from './bankingApi'

type ExpenseDraft = { id: string; payer_id: string; amount_yuan: string; note: string }
type SettlementTransfer = {
  id: string; from_id: string; from_name: string; to_id: string; to_name: string; amount_yuan: string
  status?: 'pending' | 'completed' | 'external'; in_account?: boolean; transaction_id?: string | null; completed_at?: string | null
}
type SettlementPreview = {
  note: string; total_yuan: string; expense_count: number; notice: string
  participants: { id: string; name: string; paid_yuan: string; share_yuan: string; net_yuan: string; direction: 'receive' | 'pay' | 'settled' }[]
  expenses: { payer_id: string; payer_name: string; amount_yuan: string; note: string }[]
  transfers: SettlementTransfer[]
}
type SavedSettlement = SettlementPreview & { id: string; status: 'pending' | 'owner_actions_complete'; created_at: string }
const aaAbsoluteMoney = (value: string) => aaMoney(String(Math.abs(Number(value))))

export function AaSettlementPanel({ sessionId, contacts }: { sessionId: string; contacts: AaContact[] }) {
  const [participantIds, setParticipantIds] = useState<string[]>([])
  const [expenses, setExpenses] = useState<ExpenseDraft[]>([{ id: crypto.randomUUID(), payer_id: 'self', amount_yuan: '', note: '' }])
  const [note, setNote] = useState('多人垫付结算')
  const [preview, setPreview] = useState<SettlementPreview | null>(null)
  const [settlements, setSettlements] = useState<SavedSettlement[]>([])
  const [planAction, setPlanAction] = useState<Operation | null>(null)
  const [legAction, setLegAction] = useState<{ settlementId: string; action: Operation } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const refreshSettlements = useCallback(async () => {
    const result = await aaRequest<{ settlements: SavedSettlement[] }>(`/api/aa/settlements?session_id=${encodeURIComponent(sessionId)}`)
    setSettlements(result.settlements)
  }, [sessionId])
  useEffect(() => {
    let active = true
    void (async () => {
      try {
        const result = await aaRequest<{ settlements: SavedSettlement[] }>(`/api/aa/settlements?session_id=${encodeURIComponent(sessionId)}`)
        if (active) setSettlements(result.settlements)
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : '结算计划读取失败')
      }
    })()
    return () => { active = false }
  }, [sessionId])

  const payerOptions = [{ id: 'self', name: '我（本人）' }, ...contacts.filter(person => participantIds.includes(person.id)).map(person => ({ id: person.id, name: person.name }))]
  function invalidate() { setPreview(null); setError(''); setNotice('') }
  function inputPayload() {
    return {
      session_id: sessionId, contact_ids: participantIds, note: note.trim(),
      expenses: expenses.map(({ payer_id, amount_yuan, note: expenseNote }) => ({ payer_id, amount_yuan: amount_yuan.trim(), note: expenseNote.trim() })),
    }
  }
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
    setBusy(true); setError(''); setNotice(''); setPreview(null)
    try { setPreview(await aaRequest<SettlementPreview>('/api/aa/settlements/preview', inputPayload())) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '结算计算失败') }
    finally { setBusy(false) }
  }
  async function prepareSettlement() {
    if (busy || !preview) return
    setBusy(true); setError('')
    try {
      const result = await aaRequest<{ pending_action: Operation }>('/api/aa/settlements/prepare', inputPayload())
      if (result.pending_action.type !== 'aa_settlement') throw new Error('没有收到结算计划确认，请重新计算。')
      setPlanAction(result.pending_action)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '结算计划准备失败') }
    finally { setBusy(false) }
  }
  async function prepareLeg(settlementId: string, legId: string) {
    if (busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await aaRequest<{ pending_action: Operation }>(`/api/aa/settlements/${settlementId}/legs/${legId}/prepare`, { session_id: sessionId })
      if (result.pending_action.type !== 'aa_settlement_leg') throw new Error('没有收到逐笔转账确认，请刷新计划。')
      setLegAction({ settlementId, action: result.pending_action })
    } catch (cause) { setError(cause instanceof Error ? cause.message : '逐笔转账准备失败') }
    finally { setBusy(false) }
  }
  async function planCompleted(result: Record<string, unknown>) {
    setPlanAction(null)
    setNotice(typeof result.settlement_id === 'string' ? '结算计划已保存。每笔本人相关转账仍需单独核对与授权。' : '结算计划已处理。')
    try { await refreshSettlements() } catch (cause) { setError(cause instanceof Error ? cause.message : '计划已保存，但刷新列表失败') }
  }
  async function legCompleted(result: Record<string, unknown>) {
    setLegAction(null)
    setNotice(typeof result.message === 'string' ? result.message : '模拟结算已处理，请核对交易流水。')
    try { await refreshSettlements() } catch (cause) { setError(cause instanceof Error ? cause.message : '结算已处理，但刷新明细失败') }
  }
  const canCalculate = participantIds.length >= 1 && participantIds.length <= 7 && expenses.length > 0 && expenses.length <= 100 && expenses.every(row => (aaCents(row.amount_yuan) || 0) > 0 && payerOptions.some(person => person.id === row.payer_id))

  return <section className="aa-editor aa-settlement" aria-labelledby="aa-settlement-heading" aria-busy={busy}>
    <header className="aa-section-head"><div><h2 id="aa-settlement-heading">多人垫付抵消</h2><p>录入本次共同消费和每笔实际付款人，按人头均分后抵消应收应付。</p></div><span className="aa-step">逐笔授权 · 本地模拟</span></header>
    <label className="aa-settlement-title">结算名称<input value={note} maxLength={100} onChange={event => { setNote(event.target.value); invalidate() }} /></label>
    <fieldset className="aa-settlement-members"><legend>参与人（包含本人，共 2–8 人）</legend><div>{contacts.map(person => <label key={person.id}><input type="checkbox" checked={participantIds.includes(person.id)} onChange={() => toggleParticipant(person.id)} />{person.name} · {person.phone_masked}</label>)}</div></fieldset>
    <div className="aa-section-head aa-expenses-heading"><div><h3>垫付明细</h3><p>金额精确到分；最多 100 笔，总额不超过 ¥100,000。</p></div><button type="button" className="aa-quiet" disabled={busy || expenses.length >= 100} onClick={() => { invalidate(); setExpenses(rows => [...rows, { id: crypto.randomUUID(), payer_id: 'self', amount_yuan: '', note: '' }]) }}>添加一笔</button></div>
    <div className="aa-expense-list">{expenses.map((expense, index) => <div className="aa-expense-row" key={expense.id}><span className="aa-order">{String(index + 1).padStart(2, '0')}</span><label>付款人<select value={expense.payer_id} onChange={event => updateExpense(expense.id, 'payer_id', event.target.value)}>{payerOptions.map(person => <option key={person.id} value={person.id}>{person.name}</option>)}</select></label><label>金额（元）<input inputMode="decimal" value={expense.amount_yuan} placeholder="0.00" onChange={event => updateExpense(expense.id, 'amount_yuan', event.target.value)} /></label><label>用途<input value={expense.note} maxLength={100} placeholder="例如：餐费" onChange={event => updateExpense(expense.id, 'note', event.target.value)} /></label><button type="button" className="aa-icon-button" aria-label={`删除第 ${index + 1} 笔垫付`} disabled={busy || expenses.length === 1} onClick={() => { invalidate(); setExpenses(rows => rows.filter(row => row.id !== expense.id)) }}>×</button></div>)}</div>
    <button type="button" className="aa-calculate" disabled={busy || !canCalculate} onClick={() => void calculate()}>{busy ? '正在计算…' : '计算净额与建议转账'}</button>
    {error && <p className="error-banner" role="alert">{error}</p>}{notice && <p className="aa-notice" role="status">{notice}</p>}
    {preview && <div className="aa-settlement-result" role="status"><header><div><small>结算预览 · {preview.note}</small><strong>{aaMoney(preview.total_yuan)}</strong></div><span>{preview.expense_count} 笔垫付</span></header><div className="aa-settlement-table">{preview.participants.map(person => <div key={person.id}><strong>{person.name}</strong><span>已付 {aaMoney(person.paid_yuan)} · 应摊 {aaMoney(person.share_yuan)}</span><b className={person.direction === 'receive' ? 'aa-net-positive' : person.direction === 'pay' ? 'aa-net-negative' : ''}>{person.direction === 'receive' ? '应收 ' : person.direction === 'pay' ? '应付 ' : '已平 '} {aaAbsoluteMoney(person.net_yuan)}</b></div>)}</div><h3>建议结算路径</h3>{preview.transfers.length ? <ol>{preview.transfers.map((transfer, index) => <li key={`${transfer.from_id}-${transfer.to_id}-${index}`}><strong>{transfer.from_name}</strong><span>转给</span><strong>{transfer.to_name}</strong><b>{aaMoney(transfer.amount_yuan)}</b></li>)}</ol> : <p>各人垫付正好抵消，无需结算。</p>}<p className="aa-caption">{preview.notice} 确认保存后，每笔经过本人账户的转账还须单独确认；其他参与人之间的路径不会代为执行。</p><button type="button" className="aa-calculate" disabled={busy} onClick={() => void prepareSettlement()}>核对并保存结算计划</button></div>}
    {planAction && <OperationConfirm key={planAction.id} action={planAction} sessionId={sessionId} title="确认保存多人垫付计划" onDone={planCompleted}><dl><div><dt>结算名称</dt><dd>{String((planAction.details.preview as Record<string, unknown> | undefined)?.note ?? '多人垫付结算')}</dd></div><div><dt>垫付总额</dt><dd>{aaMoney(String((planAction.details.preview as Record<string, unknown> | undefined)?.total_yuan ?? '0'))}</dd></div></dl><h4>核对建议结算路径</h4><ol>{(((planAction.details.preview as Record<string, unknown> | undefined)?.transfers as SettlementTransfer[] | undefined) ?? []).map((transfer, index) => <li key={`${transfer.from_id}-${transfer.to_id}-${index}`}><strong>{transfer.from_name}</strong><span>转给</span><strong>{transfer.to_name}</strong><b>{aaMoney(transfer.amount_yuan)}</b></li>)}</ol><p className="aa-caption">保存计划不会移动资金；每笔本人相关路径需再次单独授权。其他人之间的建议不会被标记为已付款。</p></OperationConfirm>}
    <section className="aa-saved-settlements" aria-label="已保存的多人垫付计划"><div className="aa-section-head"><div><h3>已保存的结算计划</h3><p>本人账户相关转账各自确认；其他参与人之间的建议仅作线下参考。</p></div><button type="button" className="aa-quiet" disabled={busy} onClick={() => void refreshSettlements().catch(cause => setError(cause instanceof Error ? cause.message : '结算计划刷新失败'))}>刷新</button></div>
      {!settlements.length && <p className="aa-caption">还没有已保存的多人垫付计划。</p>}
      {settlements.map(settlement => <article className="aa-saved-settlement" key={settlement.id}><header><div><strong>{settlement.note}</strong><small>{settlement.expense_count} 笔垫付 · 合计 {aaMoney(settlement.total_yuan)} · {settlement.id}</small></div><span className={`aa-status aa-status--${settlement.status === 'pending' ? 'pending' : 'paid'}`}>{settlement.status === 'pending' ? '本人路径待处理' : '本人路径已处理'}</span></header>
        <ol>{settlement.transfers.map(leg => <li key={leg.id}><div><strong>{leg.from_name}</strong><span>转给</span><strong>{leg.to_name}</strong><b>{aaMoney(leg.amount_yuan)}</b></div>{leg.status === 'completed' ? <small>已模拟入账 · 交易 {leg.transaction_id}</small> : leg.status === 'external' ? <small>其他参与人之间的建议 · 不经过本人账户 · 尚未执行</small> : <button type="button" className="aa-quiet" disabled={busy} onClick={() => void prepareLeg(settlement.id, leg.id)}>{leg.from_id === 'self' ? `核对并模拟转给 ${leg.to_name}` : `核对 ${leg.from_name} 向本人模拟结算`}</button>}</li>)}</ol>
      </article>)}
    </section>
    {legAction && <OperationConfirm key={legAction.action.id} action={legAction.action} sessionId={sessionId} title="核对多人垫付中的单笔结算" onDone={legCompleted}><dl><div><dt>付款方向</dt><dd>{String(legAction.action.details.direction) === 'out' ? '本人转出' : '模拟对方向本人入账'}</dd></div><div><dt>交易对象</dt><dd>{String(legAction.action.details.counterparty_name)}（{String(legAction.action.details.counterparty_phone_masked)}）</dd></div><div><dt>金额</dt><dd>{aaMoney(String(legAction.action.details.amount_yuan))}</dd></div><div><dt>结算计划</dt><dd>{legAction.settlementId}</dd></div></dl><p className="aa-caption">所有参与者之间的计划不会整体代付。确认后只更新本人账户及本笔回执；模拟收款不证明对方真实付款。</p></OperationConfirm>}
  </section>
}
