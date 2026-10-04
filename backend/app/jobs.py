"""Shared demo event queue; never skip another session's earlier due event."""
import os
from fastapi import HTTPException
from .db import db_session, connect, audit
from .clock import business_now, clock_info


def pending_events(conn):
    events=[]
    for r in conn.execute("SELECT id,session_id,execute_at FROM scheduled_transfers WHERE status='pending'"):
        events.append({'id':r['id'],'session_id':r['session_id'],'at':r['execute_at'],'label':'预约转账'})
    for r in conn.execute("SELECT id,session_id,settles_on FROM investment_orders WHERE status='pending_settlement'"):
        events.append({'id':r['id'],'session_id':r['session_id'],'at':r['settles_on']+'T09:00:00+08:00','label':'赎回到账'})
    for r in conn.execute("SELECT id,session_id,order_at FROM life_tasks WHERE status='scheduled'"):
        events.append({'id':r['id'],'session_id':r['session_id'],'at':r['order_at'],'label':'生日订单创建'})
    for r in conn.execute("SELECT o.id,t.session_id,t.delivery_at FROM life_orders o JOIN life_tasks t ON t.id=o.task_id WHERE o.status='placed'"):
        events.append({'id':r['id'],'session_id':r['session_id'],'at':r['delivery_at'],'label':'模拟生日配送'})
    for r in conn.execute("SELECT o.id,p.session_id,o.execute_at FROM recurring_occurrences o JOIN recurring_plans p ON p.id=o.plan_id WHERE o.status='pending' AND p.status='active'"):
        events.append({'id':r['id'],'session_id':r['session_id'],'at':r['execute_at'],'label':'周期转账'})
    return sorted(events,key=lambda x:(x['at'],x['id']))


def run_due(conn):
    from .schedules import _execute_due
    from .investments import run_due_settlements
    from .life_tasks import run_due_tasks
    from .recurring import run_due_recurring
    return _execute_due(conn)+run_due_settlements(conn)+run_due_tasks(conn)+run_due_recurring(conn)


def events_snapshot(sid):
    with db_session() as conn:
        events=pending_events(conn)
        return {'clock':clock_info(conn),'controls_enabled':os.getenv('VERALANE_DEMO_CONTROLS','1')=='1',
                'events':[{k:v for k,v in e.items() if k!='session_id'} for e in events if e['session_id']==sid],
                'next_at':events[0]['at'] if events else None,
                'notice':'所有演示任务共享业务时钟，推进会处理全局最早事件；确认与验证码有效期仍按实际时间计算。'}


def advance_events(sid):
    if os.getenv('VERALANE_DEMO_CONTROLS','1')!='1':
        raise HTTPException(403,'演示时间控制已关闭')
    conn=connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        events=pending_events(conn)
        if not any(e['session_id']==sid for e in events):
            raise HTTPException(409,'当前会话没有待执行预约或结算事件')
        old=business_now(conn).isoformat(timespec='seconds')
        new=max(old,events[0]['at'])
        conn.execute('UPDATE demo_clock SET now=? WHERE id=1',(new,))
        count=run_due(conn)
        audit(conn,sid,'demo_clock_advanced',{'from':old,'to':new,'handled':count})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
