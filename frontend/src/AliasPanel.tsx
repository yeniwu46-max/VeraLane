import { useEffect, useState, type FormEvent } from 'react'
import { requestJson, type Operation } from './bankingApi'
import { OperationConfirm } from './OperationConfirm'
import './AliasPanel.css'

type Contact = { id: string; name: string; phone_masked: string }
type Alias = { id: string; alias: string; version: number; contact_id: string; recipient: string; phone_masked: string; available: boolean }
type Props = { sessionId: string; onChanged?: () => void }

function AliasPanelContent({ sessionId, onChanged }: Props) {
  const [aliases, setAliases] = useState<Alias[]>([])
  const [contacts, setContacts] = useState<Contact[]>([])
  const [name, setName] = useState('')
  const [contactId, setContactId] = useState('')
  const [editingId, setEditingId] = useState<string>()
  const [action, setAction] = useState<Operation>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    Promise.all([
      requestJson<{ aliases: Alias[] }>(`/api/aliases?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal),
      requestJson<Contact[]>('/api/contacts', undefined, controller.signal),
    ]).then(([data, people]) => { setAliases(data.aliases); setContacts(people) }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '读取别名失败')
    })
    return () => controller.abort()
  }, [sessionId])

  async function refresh() {
    const data = await requestJson<{ aliases: Alias[] }>(`/api/aliases?session_id=${encodeURIComponent(sessionId)}`)
    setAliases(data.aliases)
  }

  async function prepare(event: FormEvent) {
    event.preventDefault()
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await requestJson<{ pending_action: Operation }>('/api/aliases/prepare', { session_id: sessionId, alias: name, contact_id: contactId, ...(editingId ? { alias_id: editingId } : {}) })
      setAction(result.pending_action)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成别名确认失败') }
    finally { setBusy(false) }
  }

  async function remove(alias: Alias) {
    setBusy(true); setError(''); setNotice('')
    try {
      const result = await requestJson<{ pending_action: Operation }>(`/api/aliases/${alias.id}/delete/prepare`, { session_id: sessionId })
      setAction(result.pending_action)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '生成删除确认失败') }
    finally { setBusy(false) }
  }

  async function discard() {
    if (!action) return
    setBusy(true); setError('')
    try {
      await requestJson(`/api/aliases/actions/${action.id}/discard`, { session_id: sessionId })
      setAction(undefined)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '放弃编辑失败') }
    finally { setBusy(false) }
  }

  async function done(result: Record<string, unknown>) {
    setAction(undefined); setName(''); setContactId(''); setEditingId(undefined)
    setNotice(String(result.message || '别名已更新'))
    try { await refresh(); onChanged?.() }
    catch (reason) { setError(reason instanceof Error ? reason.message : '刷新别名失败') }
  }

  return <details className="alias-panel">
    <summary>联系人别名 <span>{aliases.length} 个已确认称呼</span></summary>
    <p className="alias-panel__hint">明确保存“房东 → 林悦”后，可以说“转给房东 100 元”。别名仅用于当前会话；同名联系人需核对手机号。</p>
    <form autoComplete="off" className="alias-panel__form" onSubmit={(event) => void prepare(event)}>
      <label>称呼<input aria-label="联系人别名" maxLength={20} placeholder="例如：房东" value={name} disabled={busy || !!action} onChange={(event) => setName(event.target.value)} /></label>
      <label>已验证联系人<select aria-label="别名对应联系人" value={contactId} disabled={busy || !!action} onChange={(event) => setContactId(event.target.value)}><option value="">选择姓名与手机号</option>{contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.name} · {contact.phone_masked}</option>)}</select></label>
      <button className="page-primary" disabled={busy || !!action || !name.trim() || !contactId}>{busy ? '处理中…' : editingId ? '核对修改' : '核对并保存'}</button>
      {editingId && !action && <button className="page-secondary" type="button" disabled={busy} onClick={() => { setEditingId(undefined); setName(''); setContactId('') }}>取消编辑</button>}
    </form>
    {aliases.length > 0 && <ul className="alias-panel__list">{aliases.map((alias) => <li key={alias.id}><span><strong>{alias.alias}</strong><small>{alias.recipient} · {alias.phone_masked}{!alias.available && ' · 信息已变化，请重新保存'}</small></span><button type="button" className="page-secondary" disabled={busy || !!action} onClick={() => { setEditingId(alias.id); setName(alias.alias); setContactId(alias.contact_id); setNotice('') }}>编辑<span className="sr-only">{alias.alias}</span></button><button type="button" className="page-secondary" disabled={busy || !!action} onClick={() => void remove(alias)}>删除<span className="sr-only">{alias.alias}</span></button></li>)}</ul>}
    {action && <><OperationConfirm key={action.id} action={action} sessionId={sessionId} title={action.type === 'alias_delete' ? '确认删除别名' : '确认别名关系'} onDone={(result) => void done(result)}><p><strong>{String(action.details.alias)}</strong>{action.type === 'alias_upsert' && <> → {String(action.details.recipient)}（{String(action.details.phone_masked)}）</>}</p></OperationConfirm><button type="button" className="page-secondary" disabled={busy} onClick={() => void discard()}>放弃这次编辑</button></>}
    {notice && <p className="page-notice" role="status">{notice}</p>}
    {error && <p className="error-banner" role="alert">{error}</p>}
  </details>
}

export function AliasPanel(props: Props) {
  return <AliasPanelContent key={props.sessionId} {...props} />
}

export default AliasPanel
