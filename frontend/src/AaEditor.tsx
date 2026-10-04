import { useState, type FormEvent } from 'react'
import { AaAllocation } from './AaAllocation'
import { aaCents, aaMoney, aaRequest, type AaAction, type AaContact, type AaDraft, type AaFormPayload, type AaPreview, type AaReply, type AaSeed } from './aaApi'

type ParticipantRow = { key: string; contactId: string; name: string }
type ItemizedRow = { key: string; description: string; amount_yuan: string; participant_ids: string[]; ocr_confidence?: number }
type ReceiptOcr = { items: { key: number; description: string; amount_yuan: string; confidence: number }[]; total_candidate_yuan: string | null; total_candidate_confidence: number | null; lines: { text: string; confidence: number }[]; warnings: string[] }
const rowsFromDraft = (draft?: AaDraft): ParticipantRow[] => draft?.participants.map((row) => ({ key: crypto.randomUUID(), contactId: row.contact_id || '', name: row.name })) || []
const yuanFromCents = (cents: number) => `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`
const MAX_OCR_SOURCE_BYTES = 20 * 1024 * 1024
const MAX_OCR_UPLOAD_BYTES = 900 * 1024

async function compressReceipt(file: File): Promise<File> {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('请选择 JPEG、PNG 或 WebP 小票图片。')
  if (file.size > MAX_OCR_SOURCE_BYTES) throw new Error('原图不能超过 20 MB。')
  const bitmap = await createImageBitmap(file)
  try {
    if (bitmap.width * bitmap.height > 20_000_000) throw new Error('图片不能超过 2000 万像素。')
    let scale = Math.min(1, 2200 / Math.max(bitmap.width, bitmap.height))
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const canvas = document.createElement('canvas')
      canvas.width = Math.max(1, Math.round(bitmap.width * scale))
      canvas.height = Math.max(1, Math.round(bitmap.height * scale))
      const context = canvas.getContext('2d')
      if (!context) throw new Error('浏览器无法处理这张图片。')
      context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height)
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
      const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', Math.max(0.58, 0.9 - attempt * 0.045)))
      if (blob && blob.size <= MAX_OCR_UPLOAD_BYTES) return new File([blob], 'receipt.jpg', { type: 'image/jpeg' })
      scale *= 0.82
    }
    throw new Error('图片压缩后仍超过 900 KB，请裁切到小票区域再试。')
  } finally { bitmap.close() }
}

export function AaEditor({ sessionId, contacts, seed, onCreated }: {
  sessionId: string; contacts: AaContact[]; seed?: AaSeed; onCreated: (id: string) => void
}) {
  const initial = seed?.draft
  const [source, setSource] = useState(seed?.source || initial?.source_transaction || null)
  const [input, setInput] = useState('')
  const [message, setMessage] = useState(seed?.message || '')
  const [mode, setMode] = useState(seed?.mode || 'offline')
  const [hasConversation, setHasConversation] = useState(Boolean(initial))
  const [total, setTotal] = useState(source?.amount_yuan || initial?.total_yuan || '')
  const [note, setNote] = useState(initial?.note || (source ? `${source.counterparty} AA分摊` : '聚餐AA'))
  const [rows, setRows] = useState<ParticipantRow[]>(rowsFromDraft(initial))
  const [includeSelf, setIncludeSelf] = useState(initial?.include_self === true && initial?.payer_is_self === true)
  const [requiresCustom, setRequiresCustom] = useState(initial?.requires_custom_shares || false)
  const [needsReview, setNeedsReview] = useState(initial?.needs_review || [])
  const [reviewAcknowledged, setReviewAcknowledged] = useState(false)
  const [preview, setPreview] = useState<AaPreview | null>(null)
  const [shares, setShares] = useState<Record<string, string>>({})
  const [ratioValues, setRatioValues] = useState<Record<string, string>>({})
  const [allocationMode, setAllocationMode] = useState<'equal' | 'proportional' | 'manual' | 'itemized'>('equal')
  const [itemizedRows, setItemizedRows] = useState<ItemizedRow[]>([])
  const [ocrResult, setOcrResult] = useState<ReceiptOcr | null>(null)
  const [ocrBusy, setOcrBusy] = useState(false)
  const [ocrError, setOcrError] = useState('')
  const [action, setAction] = useState<AaAction | null>(null)
  const [reviewed, setReviewed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  function invalidate() { setPreview(null); setAction(null); setReviewed(false); setReviewAcknowledged(false); setError(''); setNotice('') }
  function payload(custom = false, method = allocationMode): AaFormPayload {
    const base = { session_id: sessionId, total_yuan: total.trim(), contact_ids: rows.map((row) => row.contactId), include_self: includeSelf, note: note.trim(), source_transaction_id: source?.id || null }
    if (method === 'itemized') return { ...base, itemized_items: itemizedRows.map(({ description, amount_yuan, participant_ids }) => ({ description: description.trim(), amount_yuan: amount_yuan.trim(), participant_ids })) }
    if (method === 'proportional') return { ...base, shares_ratio: Object.fromEntries(['self', ...rows.map((row) => row.contactId)].map((id) => [id, Number(ratioValues[id] ?? '1')])) }
    if (custom && method === 'manual') return { ...base, shares_yuan: shares }
    return base
  }
  async function interpret(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy || !input.trim()) return
    setBusy(true); setError(''); setAction(null); setPreview(null); setReviewed(false); setNotice('')
    try {
      const reply = await aaRequest<AaReply>('/api/aa/interpret', { session_id: sessionId, message: input.trim(), source_transaction_id: source?.id || null, reset: !hasConversation })
      if (!reply.aa_draft) throw new Error('没有收到分账草稿，请补充金额和参与者。')
      const draft = reply.aa_draft
      setMessage(reply.message); setMode(reply.mode); setHasConversation(true); setInput('')
      setSource(draft.source_transaction); setTotal(draft.source_transaction?.amount_yuan || draft.total_yuan || '')
      setNote(draft.note); setRows(rowsFromDraft(draft)); setIncludeSelf(draft.include_self === true && draft.payer_is_self === true)
      setRequiresCustom(draft.requires_custom_shares)
      setNeedsReview(draft.needs_review); setReviewAcknowledged(false)
      setAllocationMode(draft.suggested_share_ratios ? 'proportional' : 'equal')
      setRatioValues(Object.fromEntries(Object.entries(draft.suggested_share_ratios || {}).map(([id, weight]) => [id, String(weight)])))
      setItemizedRows([])
    } catch (cause) { setError(cause instanceof Error ? cause.message : '理解分账需求失败') }
    finally { setBusy(false) }
  }
  async function calculate(method = allocationMode) {
    if (busy) return
    setAllocationMode(method)
    setBusy(true); setError(''); setAction(null); setReviewed(false); setNotice('')
    try {
      const next = await aaRequest<AaPreview>('/api/aa/preview', payload(false, method))
      setPreview(next); setShares(Object.fromEntries(next.participants.map((row) => [row.id, row.amount_yuan])))
    } catch (cause) { setPreview(null); setError(cause instanceof Error ? cause.message : '分摊计算失败') }
    finally { setBusy(false) }
  }
  async function prepare() {
    if (busy || !preview) return
    setBusy(true); setError(''); setReviewed(false); setAction(null)
    try {
      const reply = await aaRequest<AaReply>('/api/aa/prepare', payload(true, allocationMode))
      if (!reply.pending_action || reply.pending_action.type !== 'aa_collection') throw new Error(reply.message || '未能生成收款确认单')
      setAction(reply.pending_action)
      setPreview(reply.pending_action.details)
      setShares(Object.fromEntries(reply.pending_action.details.participants.map((row) => [row.id, row.amount_yuan])))
    } catch (cause) { setError(cause instanceof Error ? cause.message : '确认单生成失败') }
    finally { setBusy(false) }
  }
  async function confirm() {
    if (busy || !action || !reviewed) return
    setBusy(true); setError('')
    try {
      const result = await aaRequest<{ status: string; collection_id: string; message: string }>(`/api/actions/${action.id}/confirm`, { session_id: sessionId })
      if (result.status !== 'completed' || !result.collection_id) throw new Error(result.message || '建单尚未完成，请核对收款列表。')
      setAction(null); setPreview(null); setReviewed(false); setNotice(result.message)
      setTotal(''); setRows([]); setSource(null); setNeedsReview([]); setHasConversation(false); setMessage(''); setIncludeSelf(false); setRequiresCustom(false)
      setAllocationMode('equal'); setRatioValues({})
      setItemizedRows([])
      onCreated(result.collection_id)
    } catch (cause) { setError(cause instanceof Error ? cause.message : '建立收款单失败') }
    finally { setBusy(false) }
  }
  function moveRow(index: number, offset: number) {
    invalidate()
    setRows((current) => { const next = [...current]; [next[index], next[index + offset]] = [next[index + offset], next[index]]; return next })
  }
  function chooseAllocationMode(mode: 'equal' | 'proportional' | 'itemized') {
    invalidate()
    setAllocationMode(mode)
    if (mode === 'itemized' && !itemizedRows.length) {
      setItemizedRows([{ key: crypto.randomUUID(), description: '餐费', amount_yuan: total.trim(), participant_ids: ['self', ...rows.map((row) => row.contactId).filter(Boolean)] }])
    }
    if (mode === 'proportional') setRatioValues((current) => Object.fromEntries(['self', ...rows.map((row) => row.contactId)].map((id) => [id, current[id] ?? '1'])))
  }
  function addItemizedRow() {
    invalidate()
    setItemizedRows((current) => [...current, { key: crypto.randomUUID(), description: '', amount_yuan: '', participant_ids: ['self', ...rows.map((row) => row.contactId).filter(Boolean)] }])
  }
  async function scanReceipt(file?: File) {
    if (!file) return
    setOcrResult(null); setOcrError('')
    setOcrBusy(true)
    try {
      const compressed = await compressReceipt(file)
      const form = new FormData()
      form.append('file', compressed, 'receipt.jpg')
      const response = await fetch('/api/aa/ocr/receipt', { method: 'POST', body: form })
      const value = await response.json() as ReceiptOcr & { detail?: string }
      if (!response.ok) throw new Error(value.detail || '小票识别失败，请换图重试。')
      setOcrResult(value)
    } catch (cause) { setOcrError(cause instanceof Error ? cause.message : '小票识别失败，请重试。') }
    finally { setOcrBusy(false) }
  }
  function importOcrItems() {
    if (!ocrResult?.items.length) return
    invalidate()
    setAllocationMode('itemized')
    setItemizedRows(ocrResult.items.map((item) => ({ key: crypto.randomUUID(), description: item.description, amount_yuan: item.amount_yuan, participant_ids: [], ocr_confidence: item.confidence })))
    setOcrError('')
  }
  const itemTotalCents = itemizedRows.reduce<number | null>((sum, item) => {
    const cents = aaCents(item.amount_yuan)
    return sum === null || cents === null ? null : sum + cents
  }, 0)
  const allowedIds = new Set(['self', ...rows.map((row) => row.contactId).filter(Boolean)])
  const validItems = itemizedRows.length > 0 && itemizedRows.length <= 100 && itemizedRows.every((item) => item.description.trim().length > 0 && item.description.trim().length <= 80 && (aaCents(item.amount_yuan) || 0) > 0 && item.participant_ids.length > 0 && new Set(item.participant_ids).size === item.participant_ids.length && item.participant_ids.every((id) => allowedIds.has(id)))
  const ratioEntries = ['self', ...rows.map((row) => row.contactId)].map((id) => ratioValues[id] ?? '1')
  const validRatios = allocationMode !== 'proportional' || (ratioEntries.every((value) => /^(0|[1-9]\d{0,6})$/.test(value)) && ratioEntries.some((value) => Number(value) > 0))
  const canCalculate = includeSelf && rows.length > 0 && rows.every((row) => row.contactId) && new Set(rows.map((row) => row.contactId)).size === rows.length && (aaCents(total) || 0) > 0 && (!needsReview.length || reviewAcknowledged) && validRatios && (allocationMode !== 'itemized' || (validItems && itemTotalCents === aaCents(total)))
  return <section className="aa-editor" aria-labelledby="aa-editor-heading" aria-busy={busy}>
    <header className="aa-section-head"><div><h2 id="aa-editor-heading">一起消费，分得清楚</h2><p>描述垫付金额和参与者，核对后生成 AA 收款单。</p></div><span className="aa-step">01 / 分账</span></header>
    {source && <div className="aa-source"><div><strong>已引用原始支出 · {source.counterparty}</strong><span>{source.posted_on} · {aaMoney(source.amount_yuan)} · {source.id}</span></div><button type="button" className="aa-quiet" disabled={busy} onClick={() => { invalidate(); setSource(null); setHasConversation(false); setMessage(''); setNeedsReview([]) }}>移除引用</button></div>}
    <form className="smart-transfer-form" onSubmit={(event) => void interpret(event)}>
      <label htmlFor="aa-input">一句话描述或补充信息</label>
      <textarea id="aa-input" rows={2} maxLength={500} disabled={busy} value={input} placeholder={source ? '这笔支出我和林悦均分' : '聚餐我垫了368.50元，我、林悦、王明、陈晨四个人AA'} onChange={(event) => setInput(event.target.value)} />
      <div className="smart-transfer-form__footer"><span>可以继续回答追问，也可在下方手动填写。</span><button type="submit" disabled={busy || !input.trim()}>{busy ? '正在处理…' : hasConversation ? '补充并更新草稿 ↗' : '理解分账需求 ↗'}</button></div>
    </form>
    <section className="aa-receipt-ocr" aria-label="小票本地识别">
      <div><strong>小票识别（本机运行）</strong><p>浏览器先在本机压缩，再发到本机识别；图片不传云端、不落盘，候选结果只进入可编辑草稿。</p></div>
      <label className="aa-quiet">{ocrBusy ? '正在识别…' : '选择小票图片'}<input type="file" accept="image/jpeg,image/png,image/webp" disabled={ocrBusy || busy} onChange={(event) => { void scanReceipt(event.currentTarget.files?.[0]); event.currentTarget.value = '' }} /></label>
      {ocrError && <p className="aa-error" role="alert">{ocrError}</p>}
      {ocrResult && <div className="aa-receipt-ocr__result" role="status"><span>识别到 {ocrResult.items.length} 条菜品候选{ocrResult.total_candidate_yuan ? ` · 小票总额候选 ¥${ocrResult.total_candidate_yuan}（置信度 ${Math.round((ocrResult.total_candidate_confidence || 0) * 100)}%）` : ''}</span><small>识别行合计 {aaMoney(yuanFromCents(ocrResult.items.reduce((sum, row) => sum + (aaCents(row.amount_yuan) || 0), 0)))}</small><ul>{ocrResult.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul><button type="button" className="aa-quiet" disabled={!ocrResult.items.length || busy} onClick={importOcrItems}>导入按菜品草稿</button></div>}
    </section>
    {!hasConversation && !source && <div className="smart-examples"><span>试试</span><button type="button" disabled={busy} onClick={() => setInput('聚餐我垫了368.50元，我、林悦、王明、陈晨四个人AA')}>四人聚餐</button><button type="button" disabled={busy} onClick={() => setInput('我垫付了99元，我和林悦均分')}>两人均分</button></div>}
    {message && <div className="aa-understanding" role="status"><span className="provenance__badge">{mode === 'deepseek' ? 'AI' : '规则'}</span><p>{message}</p><small>{mode === 'deepseek' ? 'DeepSeek' : '本地规则'} 提取草稿 · 银行工具精确计算金额</small></div>}
    <fieldset className="aa-fields" disabled={busy}><legend>核对分账信息</legend>
      <div className="aa-field-pair"><label>垫付总额（元）<input aria-label="AA垫付总额" inputMode="decimal" maxLength={15} readOnly={Boolean(source)} value={total} placeholder="例如 368.50" onChange={(event) => { invalidate(); setTotal(event.target.value) }} />{source && <small>来自原账本，总额不可修改</small>}</label><label>用途 / 备注<input aria-label="AA用途备注" maxLength={100} value={note} placeholder="例如 聚餐AA" onChange={(event) => { invalidate(); setNote(event.target.value) }} /></label></div>
      <label className="review-check aa-self-check"><input type="checkbox" checked={includeSelf} onChange={(event) => { invalidate(); setIncludeSelf(event.target.checked) }} /><span>本次分摊包含我，支出由我垫付。<small>本人固定在第一位；如不承担费用，可在分摊表将本人金额改为 0。</small></span></label>
      <div className="aa-section-head aa-participant-heading"><h3>其他参与人 <span>{rows.length} 人</span></h3><button type="button" className="aa-quiet" disabled={rows.length >= contacts.length} onClick={() => { invalidate(); setRows((current) => [...current, { key: crypto.randomUUID(), contactId: '', name: '' }]) }}>＋ 添加参与人</button></div>
      {!rows.length && <p className="aa-caption">添加至少一位联系人；同名联系人需要核对手机号。</p>}
      <div className="aa-participant-list">{rows.map((row, index) => <div className="aa-participant" key={row.key}>
        <span className="aa-order">{index + 2}</span><label><span>{row.name && !row.contactId ? `请确认：${row.name}` : `参与人 ${index + 1}`}</span><select aria-label={`AA参与人${index + 1}`} value={row.contactId} onChange={(event) => { invalidate(); const selected = contacts.find((contact) => contact.id === event.target.value); const nextRows = rows.map((item) => item.key === row.key ? { ...item, contactId: event.target.value, name: selected?.name || '' } : item); setRows(nextRows); const nextIds = new Set(['self', ...nextRows.map((item) => item.contactId).filter(Boolean)]); setItemizedRows((current) => current.map((item) => ({ ...item, participant_ids: item.participant_ids.filter((id) => nextIds.has(id)) }))) }}><option value="">{row.name ? `选择 ${row.name} 对应的联系人` : '请选择联系人'}</option>{contacts.map((contact) => <option key={contact.id} value={contact.id} disabled={rows.some((other) => other.key !== row.key && other.contactId === contact.id)}>{contact.name} · {contact.phone_masked}</option>)}</select></label>
        <div className="aa-row-controls"><button type="button" className="aa-icon-button" disabled={index === 0} aria-label={`上移参与人${index + 1}`} onClick={() => moveRow(index, -1)}>↑</button><button type="button" className="aa-icon-button" disabled={index === rows.length - 1} aria-label={`下移参与人${index + 1}`} onClick={() => moveRow(index, 1)}>↓</button><button type="button" className="aa-icon-button" aria-label={`移除参与人${index + 1}`} onClick={() => { invalidate(); const nextRows = rows.filter((item) => item.key !== row.key); setRows(nextRows); const nextIds = new Set(['self', ...nextRows.map((item) => item.contactId).filter(Boolean)]); setItemizedRows((current) => current.map((item) => ({ ...item, participant_ids: item.participant_ids.filter((id) => nextIds.has(id)) }))) }}>×</button></div>
      </div>)}</div>
      <div className="aa-allocation-modes" role="group" aria-label="分摊方式">
        <button type="button" className="aa-quiet" aria-pressed={allocationMode === 'equal'} disabled={busy} onClick={() => chooseAllocationMode('equal')}>均分</button>
        <button type="button" className="aa-quiet" aria-pressed={allocationMode === 'proportional'} disabled={busy} onClick={() => chooseAllocationMode('proportional')}>按比例</button>
        <button type="button" className="aa-quiet" aria-pressed={allocationMode === 'itemized'} disabled={busy} onClick={() => chooseAllocationMode('itemized')}>按菜品</button>
      </div>
      {allocationMode === 'itemized' && <div className="aa-itemized" aria-label="按菜品分账明细">
        <div className="aa-section-head"><div><h3>菜品与参与人</h3><p>每道菜分别选择用餐人；余数按参与人顺序分到每一分。</p></div><button type="button" className="aa-quiet" disabled={busy || itemizedRows.length >= 100} onClick={addItemizedRow}>＋ 添加菜品</button></div>
        {itemizedRows.map((item, index) => <article className="aa-itemized-line" key={item.key}>
          <div className="aa-itemized-line__top"><div><strong>菜品 {index + 1}</strong>{item.ocr_confidence !== undefined && <small className="aa-itemized-line__confidence">OCR 置信度 {Math.round(item.ocr_confidence * 100)}% · 请核对</small>}</div><button type="button" className="aa-icon-button" aria-label={`删除菜品${index + 1}`} disabled={busy || itemizedRows.length <= 1} onClick={() => { invalidate(); setItemizedRows((current) => current.filter((row) => row.key !== item.key)) }}>×</button></div>
          <div className="aa-field-pair"><label>菜品名称<input aria-label={`菜品${index + 1}名称`} maxLength={80} value={item.description} disabled={busy} placeholder="例如：披萨" onChange={(event) => { invalidate(); setItemizedRows((current) => current.map((row) => row.key === item.key ? { ...row, description: event.target.value } : row)) }} /></label><label>金额（元）<input aria-label={`菜品${index + 1}金额`} inputMode="decimal" maxLength={15} value={item.amount_yuan} disabled={busy} placeholder="例如 68.00" onChange={(event) => { invalidate(); setItemizedRows((current) => current.map((row) => row.key === item.key ? { ...row, amount_yuan: event.target.value } : row)) }} /></label></div>
          <div className="aa-itemized-participants" role="group" aria-label={`菜品${index + 1}参与人`}>
            {[{ id: 'self', name: '我（本人）' }, ...rows.map((row, rowIndex) => ({ id: row.contactId, name: contacts.find((contact) => contact.id === row.contactId)?.name || `参与人 ${rowIndex + 1}` }))].map((person) => <label key={person.id || person.name} className="review-check"><input type="checkbox" checked={item.participant_ids.includes(person.id)} disabled={busy || !person.id} onChange={(event) => { invalidate(); setItemizedRows((current) => current.map((row) => row.key === item.key ? { ...row, participant_ids: event.target.checked ? [...row.participant_ids, person.id] : row.participant_ids.filter((id) => id !== person.id) } : row)) }} /><span>{person.name}</span></label>)}
          </div>
        </article>)}
        <p className={`aa-itemized-total${itemTotalCents !== aaCents(total) ? ' aa-warning-text' : ''}`} role="status">明细合计 {itemTotalCents === null ? '—' : aaMoney(yuanFromCents(itemTotalCents))} / 垫付总额 {aaMoney(total)}{itemTotalCents === aaCents(total) ? ' · 金额一致' : ' · 请调整至一致'}</p>
      </div>}
      {allocationMode === 'proportional' && <div className="aa-ratio-list"><p>输入每个人的权重，例如 1 : 2 : 3；系统按权重计算金额并展示到分结果。</p>
        {[{ id: 'self', name: '我（本人）' }, ...rows.map((row, index) => ({ id: row.contactId, name: contacts.find((contact) => contact.id === row.contactId)?.name || `参与人 ${index + 1}` }))].map((person) => <label key={person.id}>{person.name}<input type="number" min="0" max="1000000" step="1" inputMode="numeric" value={ratioValues[person.id] ?? '1'} disabled={busy} onChange={(event) => { invalidate(); setRatioValues((current) => ({ ...current, [person.id]: event.target.value })) }} /></label>)}
      </div>}
      {needsReview.length > 0 && <div className="aa-review-note"><ul>{needsReview.map((reason, index) => <li key={index}>{reason}</li>)}</ul><label className="review-check"><input type="checkbox" checked={reviewAcknowledged} onChange={(event) => { setPreview(null); setAction(null); setReviewed(false); setReviewAcknowledged(event.target.checked) }} /><span>我已在上方补齐并核对信息，确认这是本人垫付；选择分摊方式后再检查每个人的金额。</span></label></div>}
      {!preview && <button type="button" className="page-primary aa-calculate" disabled={!canCalculate} onClick={() => void calculate()}>{busy ? '正在计算…' : '计算分摊金额'}</button>}
    </fieldset>
    {preview && <AaAllocation preview={preview} shares={shares} action={action} busy={busy} reviewed={reviewed} requiresCustom={requiresCustom} onReviewed={setReviewed} onChange={(id, amount) => { setAllocationMode('manual'); setPreview((current) => current ? { ...current, allocation_method: 'manual', share_ratios: null, participants: current.participants.map((row) => ({ ...row, share_ratio: null })) } : current); setShares((current) => ({ ...current, [id]: amount })); setAction(null); setReviewed(false); setError('') }} onEqualize={() => void calculate('equal')} onPrepare={() => void prepare()} onConfirm={() => void confirm()} />}
    {error && <p className="error-banner" role="alert">{error}</p>}
    {notice && <p className="aa-notice" role="status">{notice}</p>}
  </section>
}
