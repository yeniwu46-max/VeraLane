import { useEffect, useRef, useState } from 'react'
import { AaEditor } from './AaEditor'
import { AaCollectionDialog } from './AaCollectionDialog'
import { AaSettlementPanel } from './AaSettlementPanel'
import { aaMoney, aaRequest, aaStatusLabels, type AaCollection, type AaCollections, type AaContact, type AaSeed } from './aaApi'
import './AaPanel.css'

export function AaPanel({ sessionId, contacts, seed, onChanged, onCreated }: {
  sessionId: string; contacts: AaContact[]; seed?: AaSeed; onChanged: () => void; onCreated: () => void
}) {
  const [data, setData] = useState<AaCollections | null>(null)
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const generation = useRef(0)
  const changed = useRef(onChanged)
  useEffect(() => { changed.current = onChanged }, [onChanged])
  useEffect(() => {
    const controller = new AbortController()
    let loading = false
    async function load() {
      if (loading) return
      loading = true
      const version = generation.current
      try {
        const next = await aaRequest<AaCollections>(`/api/aa/collections?session_id=${encodeURIComponent(sessionId)}`, undefined, controller.signal)
        if (!controller.signal.aborted && version === generation.current) { setData(next); setError('') }
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '收款单加载失败')
      } finally { loading = false }
    }
    void load()
    const timer = window.setInterval(() => { if (!document.hidden) void load() }, 4000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [sessionId, revision])
  function updated(collection: AaCollection) {
    generation.current += 1
    setData((current) => current ? { ...current, collections: current.collections.map((row) => row.id === collection.id ? collection : row) } : current)
    changed.current()
  }
  const selected = data?.collections.find((collection) => collection.id === selectedId) || null
  const activeCount = data?.collections.filter((collection) => collection.status === 'pending' || collection.status === 'partial').length || 0
  return <div className="aa-workspace">
    <AaEditor key={seed?.id || 'new-aa'} sessionId={sessionId} contacts={contacts} seed={seed} onCreated={(id) => { generation.current += 1; setSelectedId(id); setRevision((value) => value + 1); changed.current(); onCreated() }} />
    <AaSettlementPanel sessionId={sessionId} contacts={contacts} />
    <section className="aa-collections" aria-labelledby="aa-collections-heading"><header className="aa-section-head"><div><h2 id="aa-collections-heading">我的 AA 收款单 <span>{activeCount} 笔进行中</span></h2><p>查看每个人的到账情况与回执。</p></div><button type="button" className="aa-quiet" onClick={() => setRevision((value) => value + 1)}>刷新</button></header>
      {error && <p className="error-banner" role="alert">{error}</p>}
      {!data && !error && <p className="aa-empty" role="status">正在读取收款单…</p>}
      {data && (!data.collections.length ? <p className="aa-empty">还没有收款单。完成上方分摊并确认后，会在这里持续跟踪。</p> : <div className="aa-collection-list">{data.collections.map((collection) => <button type="button" className="aa-collection-row" key={collection.id} onClick={() => setSelectedId(collection.id)} aria-label={`查看${collection.note} ${aaStatusLabels[collection.status]}的AA收款单`}><span className="aa-collection-title"><strong>{collection.note || 'AA收款'}</strong><small>{collection.participants.length} 人分摊 · 总额 {aaMoney(collection.total_yuan)}</small></span><span className="aa-collection-progress"><strong>{aaMoney(collection.received_yuan)} <small>/ {aaMoney(collection.receivable_yuan)}</small></strong><small>已收 / 应收</small></span><span className={`aa-status aa-status--${collection.status}`}>{aaStatusLabels[collection.status]}</span><span className="aa-arrow" aria-hidden="true">↗</span></button>)}</div>)}
    </section>
    <AaCollectionDialog key={selectedId || 'closed'} collection={selected} sessionId={sessionId} demoEnabled={Boolean(data?.demo_controls_enabled)} onClose={() => setSelectedId(null)} onUpdated={updated} />
  </div>
}
