import {useEffect,useState} from 'react'
import {requestJson,type Operation} from './bankingApi'
import {OperationConfirm} from './OperationConfirm'
import {formatBankTime} from './bankTime'
import './FundsPanel.css'

type Reserve={id:string;purpose:string;status:string;amount_yuan:string;remaining_yuan:string;consumed_yuan:string;released_yuan:string}
type Funds={balance_yuan:string;available_yuan:string;reserved_yuan:string;reservations:Reserve[]}
type Events={clock:{now:string};controls_enabled:boolean;events:{id:string;at:string;label:string}[];next_at:string|null;notice:string}
export function FundsPanel({sessionId,onChanged}:{sessionId:string;onChanged:()=>void}) {
  const [data,setData]=useState<Funds>()
  const [events,setEvents]=useState<Events>()
  const [revision,setRevision]=useState(0)
  const [amount,setAmount]=useState('1000')
  const [purpose,setPurpose]=useState('')
  const [action,setAction]=useState<Operation>()
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const [busy,setBusy]=useState(false)
  const [advanceConsent,setAdvanceConsent]=useState(false)
  useEffect(()=>{
    const c=new AbortController()
    Promise.all([requestJson<Funds>(`/api/funds?session_id=${encodeURIComponent(sessionId)}`,undefined,c.signal),requestJson<Events>(`/api/demo/events?session_id=${encodeURIComponent(sessionId)}`,undefined,c.signal)])
      .then(([f,e])=>{setData(f);setEvents(e)})
      .catch(e=>{if(!c.signal.aborted)setError(e instanceof Error?e.message:'读取失败')})
    return()=>c.abort()
  },[sessionId,revision])
  async function prepare(path:string,body:object={}) {
    setBusy(true);setError('');setAction(undefined)
    try {const r=await requestJson<{pending_action:Operation;message:string}>(path,{session_id:sessionId,...body});setAction(r.pending_action);setNotice(r.message)}
    catch(e){setError(e instanceof Error?e.message:'生成计划失败')}
    finally{setBusy(false)}
  }
  function changed(){setRevision(r=>r+1);onChanged()}
  async function advance(){
    if(!advanceConsent||busy)return
    setBusy(true);setError('')
    try {await requestJson('/api/demo/clock/advance-next',{session_id:sessionId});setNotice('演示时钟已推进，相关任务已扫描；请查看每项实际结果。');setAdvanceConsent(false);changed()}
    catch(e){setError(e instanceof Error?e.message:'推进失败')}
    finally{setBusy(false)}
  }
  return <section className="funds-panel" aria-label="资金预留与演示时钟">
    <header><h2>资金预留</h2><button className="page-secondary" onClick={()=>changed()}>刷新</button></header>
    <div className="funds-totals"><span>账面余额<strong>¥{data?.balance_yuan??'—'}</strong></span><span>可用余额<strong>¥{data?.available_yuan??'—'}</strong></span><span>有效预留<strong>¥{data?.reserved_yuan??'—'}</strong></span></div>
    <p>预留只减少可用余额；释放不增加账面余额。账户所有转出工具共用此规则。</p>
    <form autoComplete="off" onSubmit={e=>{e.preventDefault();void prepare('/api/funds/reservations/prepare',{amount_yuan:amount,purpose})}} className="funds-form">
      <label>预留用途<input required value={purpose} maxLength={100} placeholder="如生日预算" onChange={e=>setPurpose(e.target.value)}/></label>
      <label>金额（元）<input required inputMode="decimal" value={amount} onChange={e=>setAmount(e.target.value)}/></label>
      <button className="page-secondary" disabled={busy}>生成预留计划</button>
    </form>
    {notice&&<p role="status">{notice}</p>}{error&&<p className="error-banner" role="alert">{error}</p>}
    {action&&<OperationConfirm key={action.id} action={action} sessionId={sessionId} title={action.type==='fund_reserve'?'确认预留':'确认释放未使用资金'} onDone={r=>{setNotice(String(r.message));setAction(undefined);changed()}}><dl><dt>用途</dt><dd>{String(action.details.purpose)}</dd><dt>金额</dt><dd>¥{String(action.details.amount_yuan??(Number(action.details.remaining_cents)/100).toFixed(2))}</dd></dl></OperationConfirm>}
    <div className="funds-list">{data?.reservations.map(r=><article key={r.id}><div><strong>{r.purpose}</strong><small>{r.status==='active'?'有效':r.status==='consumed'?'已使用':'已释放'} · 原预留 ¥{r.amount_yuan}</small></div><span>剩余 ¥{r.remaining_yuan} · 已用 ¥{r.consumed_yuan}</span>{r.status==='active'&&<button disabled={busy} className="page-secondary" onClick={()=>void prepare(`/api/funds/reservations/${r.id}/release/prepare`)}>释放剩余</button>}</article>)}</div>
    <details className="funds-clock"><summary>演示时钟与待执行事件</summary><p>北京时间 {events?formatBankTime(events.clock.now):'—'}</p><p>{events?.notice}</p>{events?.events.map(e=><div className="funds-event" key={e.id}><strong>{e.label}</strong><span>{formatBankTime(e.at)}</span></div>)}{!events?.events.length&&<p>当前会话没有待执行事件。</p>}
      {events?.controls_enabled&&events.events.length>0&&<><label className="review-check"><input type="checkbox" checked={advanceConsent} onChange={e=>setAdvanceConsent(e.target.checked)}/>推进到全局下一事件 {events.next_at?formatBankTime(events.next_at):''}，可能执行已授权的模拟扣款。</label><button className="page-secondary" disabled={busy||!advanceConsent} onClick={()=>void advance()}>推进并执行到期事件</button></>}
    </details>
  </section>
}
