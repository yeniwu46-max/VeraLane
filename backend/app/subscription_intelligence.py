"""Evidence-based subscription diagnosis and individually receipted cancellation."""
from __future__ import annotations

import calendar
import json
import statistics
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .clock import business_date, business_now
from .db import ACCOUNT_ID, USER_ID, audit, db_session
from .service import create_action, money, reply

router = APIRouter(prefix="/api")


def init_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS subscription_closures (subscription_id TEXT PRIMARY KEY, cancelled_at TEXT NOT NULL)")
    conn.execute("""CREATE TABLE IF NOT EXISTS reminders (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, source_id TEXT NOT NULL,
        due_on TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT)""")


def diagnosis(conn):
    today = date.fromisoformat(business_date(conn))
    rows = conn.execute("SELECT * FROM transactions WHERE account_id=? AND direction='out' AND posted_on<=? ORDER BY posted_on,id",
                        (ACCOUNT_ID, today.isoformat())).fetchall()
    subs = {r['merchant']: dict(r) for r in conn.execute("SELECT * FROM subscriptions WHERE user_id=?", (USER_ID,))}
    groups = {}
    for row in rows:
        if row['category'] == '数字服务' or row['counterparty'] in subs:
            groups.setdefault(row['counterparty'], []).append(row)
    items = []
    for merchant in sorted(set(groups) | set(subs)):
        history = groups.get(merchant, [])
        sub = subs.get(merchant)
        days = sorted({date.fromisoformat(r['posted_on']) for r in history})
        intervals = [(b-a).days for a,b in zip(days, days[1:])]
        median = statistics.median(intervals) if intervals else None
        cycle = ('每周' if 5 <= median <= 9 else '每月' if 25 <= median <= 35 else '每年' if 350 <= median <= 380 else None) if median else None
        tolerance = 5 if cycle == '每月' else 2 if cycle == '每周' else 15
        stable = bool(cycle and all(abs(i-median) <= tolerance for i in intervals))
        if not sub and not stable:
            continue
        estimated = None
        if not sub and stable:
            predicted = days[-1] + timedelta(days=round(median))
            estimated = {'from': (predicted-timedelta(days=tolerance)).isoformat(), 'to': (predicted+timedelta(days=tolerance)).isoformat(), 'overdue': predicted < today}
        last = history[-1] if history else None
        previous = history[-2] if len(history) > 1 else None
        rise = last['amount_cents']-previous['amount_cents'] if previous else 0
        closure = conn.execute("SELECT cancelled_at FROM subscription_closures WHERE subscription_id=?", (sub['id'],)).fetchone() if sub else None
        after_cancel = [r['id'] for r in history if closure and r['posted_on'] > closure['cancelled_at'][:10]]
        items.append({
            'merchant': merchant, 'subscription_id': sub['id'] if sub else None,
            'status': sub['status'] if sub else 'candidate', 'cycle': cycle if stable else '待核对',
            'confidence': '协议已保存' if sub else '周期线索较稳定' if len(days)>=3 else '仅两次扣费线索',
            'renewal_on': sub['renewal_on'] if sub and sub['status']=='active' else None,
            'estimated_renewal': estimated, 'amount_yuan': money(sub['amount_cents'] if sub else last['amount_cents']),
            'price_change_yuan': money(rise), 'price_increased': rise > 0,
            'after_cancel_ids': after_cancel,
            'limitations': ['账单不能说明服务使用频率。', '取消银行代扣不等于退订商户会员。'] + ([] if len(days)>=3 else ['历史样本少，周期不能视为已确定。']),
            'evidence': [{'id': r['id'], 'posted_on': r['posted_on'], 'counterparty': r['counterparty'], 'category': r['category'], 'note': r['note'], 'amount_yuan': money(r['amount_cents'])} for r in reversed(history)],
        })
    return {'as_of': today.isoformat(), 'mode': 'offline', 'items': items}


def refresh_reminders(conn):
    today = date.fromisoformat(business_date(conn))
    for sub in conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND status='active'", (USER_ID,)):
        due = date.fromisoformat(sub['renewal_on'])
        if 0 <= (due-today).days <= 30:
            conn.execute("INSERT OR IGNORE INTO reminders(id,account_id,kind,title,body,source_id,due_on,created_at) VALUES(?,?,'renewal',?,?,?,?,?)",
                         (f"renewal:{sub['id']}:{due}", ACCOUNT_ID, f"{sub['merchant']}即将续费", f"已保存协议：{due} 扣费 ¥{money(sub['amount_cents'])}。是否保留请由你决定。", sub['id'], str(due), business_now(conn).isoformat(timespec='seconds')))


def prepare_batch(session_id, ids):
    if not ids or len(set(ids)) != len(ids):
        raise HTTPException(422, '请选择不重复的具体代扣协议')
    with db_session() as conn:
        items = []
        today = date.fromisoformat(business_date(conn))
        first = date(today.year+(today.month==12), today.month%12+1, 1)
        end = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        for sid in ids:
            row = conn.execute("SELECT * FROM subscriptions WHERE id=? AND user_id=? AND status='active'", (sid, USER_ID)).fetchone()
            if row is None:
                raise HTTPException(409, '所选协议不再生效，请刷新后核对')
            items.append({'subscription_id': row['id'], 'merchant': row['merchant'], 'amount_cents': row['amount_cents'], 'amount_yuan': money(row['amount_cents']), 'renewal_on': row['renewal_on']})
        expected = sum(i['amount_cents'] for i in items if first.isoformat() <= i['renewal_on'] <= end.isoformat())
        payload = {'items': items, 'period_start': str(first), 'period_end': str(end), 'expected_savings_yuan': money(expected)}
        action = create_action(conn, session_id, 'subscription_batch', 'yellow', payload)
        return reply('请逐项核对取消银行代扣的影响；不代表商户退订或退款。', session_id, 'offline', pending_action=action)


def execute_batch(conn, action_id, session_id, payload):
    results = []
    saved = 0
    for item in payload['items']:
        current = conn.execute('SELECT * FROM subscriptions WHERE id=? AND user_id=?', (item['subscription_id'], USER_ID)).fetchone()
        valid = current and current['status']=='active' and all(current[k]==item[k] for k in ('merchant','amount_cents','renewal_on'))
        if not valid:
            results.append({**item, 'status': 'failed', 'message': '协议状态或金额已变化，未执行；请重新核对。'})
            continue
        conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (current['id'],))
        conn.execute('INSERT OR REPLACE INTO subscription_closures VALUES(?,?)', (current['id'], business_now(conn).isoformat(timespec='seconds')))
        if max(payload['period_start'], business_date(conn)) <= current['renewal_on'] <= payload['period_end']:
            saved += current['amount_cents']
        results.append({**item, 'status': 'completed', 'message': '已取消此模拟银行代扣，商户会员状态未知。'})
        audit(conn, session_id, 'subscription_cancelled', {'action_id': action_id, 'subscription_id': current['id']})
    count = sum(i['status']=='completed' for i in results)
    return {'status': 'completed' if count==len(results) else 'partially_completed' if count else 'failed',
            'message': f"已取消 {count}/{len(results)} 项模拟代扣，逐项结果如下。", 'items': results,
            'expected_savings_yuan': money(saved), 'period_start': payload['period_start'], 'period_end': payload['period_end'], 'action_id': action_id}


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=1, max_length=100)
    subscription_ids: list[str] = Field(min_length=1, max_length=10)


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(min_length=1, max_length=100)


@router.get('/subscriptions/diagnostics')
def get_diagnostics():
    with db_session() as conn:
        return diagnosis(conn)


@router.post('/subscriptions/prepare-batch')
def post_batch(request: BatchRequest):
    return prepare_batch(request.session_id, request.subscription_ids)


@router.get('/reminders')
def get_reminders(session_id: str = Query(min_length=1, max_length=100)):
    with db_session() as conn:
        refresh_reminders(conn)
        rows = conn.execute("SELECT r.*,s.status AS source_status FROM reminders r LEFT JOIN subscriptions s ON r.source_id=s.id WHERE r.account_id=? ORDER BY r.due_on,r.id", (ACCOUNT_ID,)).fetchall()
        return {'items': [dict(r) for r in rows], 'as_of': business_date(conn)}


@router.post('/reminders/{reminder_id}/read')
def read_reminder(reminder_id: str, request: ReadRequest):
    with db_session() as conn:
        if not conn.execute('SELECT 1 FROM reminders WHERE id=? AND account_id=?', (reminder_id, ACCOUNT_ID)).fetchone():
            raise HTTPException(404, '未找到此提醒')
        conn.execute('UPDATE reminders SET read_at=COALESCE(read_at,?) WHERE id=?', (business_now(conn).isoformat(timespec='seconds'), reminder_id))
        return {'status': 'read'}
