"""Fictional card controls with real enforcement on the simulated card rail."""
import json
import os
import re
import sqlite3

from fastapi import HTTPException

from .db import ACCOUNT_ID, USER_ID, audit, db_session, utc_now
from .clock import business_date
from .execution_controls import debit, requires_red_tier
from .service import cents_from_yuan, create_action, money


PRODUCTS = [
    {"id": "debit", "name": "Vera 日常借记卡", "condition": "使用模拟账户余额，申请与审批分离", "fee_yuan": "0.00"},
    {"id": "credit", "name": "Vera 青年信用卡", "condition": "填写虚构月收入；模拟审批仅返回状态，不自动发卡", "fee_yuan": "0.00"},
]


def init_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY,user_id TEXT NOT NULL,name TEXT NOT NULL,last4 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','locked','lost')),online_enabled INTEGER NOT NULL,
        payment_limit_cents INTEGER NOT NULL,credit_limit_cents INTEGER NOT NULL,version INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS card_applications (
        id TEXT PRIMARY KEY,session_id TEXT NOT NULL,kind TEXT NOT NULL,card_id TEXT,
        payload_json TEXT NOT NULL,status TEXT NOT NULL,decision TEXT,created_at TEXT NOT NULL)""")
    conn.execute("INSERT OR IGNORE INTO cards VALUES ('card-main',?,'Vera 日常卡','6018','active',1,200000,0,1)", (USER_ID,))
    conn.execute("INSERT OR IGNORE INTO cards VALUES ('card-travel',?,'Vera 旅行卡','9026','active',1,100000,500000,1)", (USER_ID,))


def _card(conn, id):
    card = conn.execute("SELECT * FROM cards WHERE id=? AND user_id=?", (id, USER_ID)).fetchone()
    if card is None:
        raise HTTPException(404, "未找到该卡片")
    return card


def _public(row):
    value = dict(row)
    value.pop('user_id', None)
    value['online_enabled'] = bool(value['online_enabled'])
    value['payment_limit_yuan'] = money(row['payment_limit_cents'])
    value['credit_limit_yuan'] = money(row['credit_limit_cents'])
    return value


def snapshot(sid):
    with db_session() as conn:
        cards = [_public(c) for c in conn.execute('SELECT * FROM cards WHERE user_id=? ORDER BY id', (USER_ID,))]
        apps = []
        for row in conn.execute('SELECT * FROM card_applications WHERE session_id=? ORDER BY created_at DESC', (sid,)):
            item = dict(row)
            item['details'] = json.loads(item.pop('payload_json'))
            apps.append(item)
        return {"cards": cards, "applications": apps, "products": PRODUCTS,
                "notice": "锁卡、挂失与线上限制只作用于模拟刷卡通道；账户转账和账户代扣保持各自授权。授信额度不作为模拟刷卡现金。"}


def _plan(conn, sid, typ, tier, payload, message):
    return {"mode": "offline", "message": message, "pending_action": create_action(conn, sid, typ, tier, payload)}


def prepare_update(sid, id, operation, amount_yuan=None):
    with db_session() as conn:
        card = _card(conn, id)
        if operation not in ('lock','unlock','online_off','online_on','report_loss','payment_limit','credit_increase'):
            raise HTTPException(422, '请选择支持的卡片操作')
        if card['status'] == 'lost':
            raise HTTPException(409, '挂失卡不能通过普通解锁恢复，请申请新卡')
        if (operation == 'lock' and card['status'] != 'active') or (operation == 'unlock' and card['status'] != 'locked'):
            raise HTTPException(409, '卡片状态已变化，请刷新')
        payload = {"card_id": id, "name": card['name'], "last4": card['last4'], "version": card['version'], "operation": operation}
        if operation in ('payment_limit','credit_increase'):
            amount = cents_from_yuan(amount_yuan)
            if amount is None:
                raise HTTPException(422, '金额须大于零并精确到分')
            if operation == 'payment_limit' and amount > card['payment_limit_cents']:
                raise HTTPException(422, '当前可自助调低支付限额，调高需另行申请')
            if operation == 'credit_increase' and (card['credit_limit_cents'] == 0 or amount <= card['credit_limit_cents']):
                raise HTTPException(422, '仅信用卡可申请高于当前授信的额度')
            payload.update(amount_cents=amount, amount_yuan=money(amount))
        return _plan(conn, sid, 'card_update', 'red' if operation in ('report_loss','credit_increase') else 'yellow', payload,
                     '请核对卡号尾号和操作影响。挂失不能普通解锁；提额仅提交模拟申请。')


def execute_update(conn, aid, sid, payload):
    card = _card(conn, payload['card_id'])
    if card['version'] != payload['version'] or card['status'] == 'lost':
        raise HTTPException(409, '卡片状态或限制已变化，请重新生成计划')
    op = payload['operation']
    if op == 'credit_increase':
        conn.execute("INSERT INTO card_applications VALUES (?,?,'credit_increase',?,?,'submitted',NULL,?)",
                     (aid, sid, card['id'], json.dumps(payload, ensure_ascii=False), utc_now()))
        return {'status': 'completed', 'action_id': aid, 'application_id': aid, 'message': '已提交模拟提额申请，尚未审批或调整授信额度。'}
    field, value = {'lock': ('status','locked'), 'unlock': ('status','active'), 'report_loss': ('status','lost'),
                    'online_off': ('online_enabled',0), 'online_on': ('online_enabled',1),
                    'payment_limit': ('payment_limit_cents',payload.get('amount_cents'))}[op]
    # Field comes only from this fixed mapping, never model or request SQL.
    conn.execute(f'UPDATE cards SET {field}=?,version=version+1 WHERE id=?', (value, card['id']))
    return {'status': 'completed', 'action_id': aid, 'card': _public(_card(conn, card['id'])), 'message': '卡片控制已更新；仅作用于模拟刷卡通道。'}


def prepare_application(sid, product_id, applicant, income_yuan):
    income = cents_from_yuan(income_yuan)
    if product_id not in ('debit','credit') or income is None or not applicant.strip():
        raise HTTPException(422, '请选择产品并填写虚构申请人和有效月收入')
    with db_session() as conn:
        return _plan(conn,sid,'card_application','red', {'product_id':product_id,'applicant':applicant.strip(),
                     'income_cents':income,'income_yuan':money(income),'rules_version':'demo-1'}, '仅提交模拟申请，审批和发卡状态将单独展示。')


def execute_application(conn, aid, sid, payload):
    conn.execute("INSERT INTO card_applications VALUES (?,?,'new_card',NULL,?,'submitted',NULL,?)",
                 (aid,sid,json.dumps(payload,ensure_ascii=False),utc_now()))
    return {'status':'completed','action_id':aid,'application_id':aid,'message':'申请已提交，等待模拟审批，尚未发卡。'}


def review_application(sid, id):
    if os.getenv('VERALANE_DEMO_CONTROLS','1') != '1':
        raise HTTPException(403,'演示审批已关闭')
    with db_session() as conn:
        # No money is moved. BEGIN IMMEDIATE serializes this explicit demo event.
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM card_applications WHERE id=? AND session_id=?',(id,sid)).fetchone()
        if not row:
            raise HTTPException(404,'未找到该申请')
        if row['status'] != 'submitted':
            return {'status':row['status'],'message':row['decision']}
        p = json.loads(row['payload_json'])
        if row['kind'] == 'credit_increase':
            status, decision = 'manual_review', '提额转模拟人工审核；没有变更授信额度。'
        else:
            eligible = p['product_id']=='debit' or p['income_cents'] >= 300000
            status, decision = ('approved_pending_issue','模拟资格检查通过，待发卡；尚未获得可用新卡。') if eligible else ('declined','模拟规则要求信用卡月收入不少于 3000 元；此结果不代表真实银行审批。')
        conn.execute('UPDATE card_applications SET status=?,decision=? WHERE id=?',(status,decision,id))
        audit(conn,sid,'card_application_reviewed',{'application_id':id,'status':status})
        return {'status':status,'message':decision}


def prepare_payment(sid,id,amount_yuan,channel,merchant):
    amount=cents_from_yuan(amount_yuan)
    if amount is None or channel not in ('online','pos') or not merchant.strip():
        raise HTTPException(422,'请填写有效金额、通道和虚构商户')
    with db_session() as conn:
        card=_card(conn,id)
        _check_payment(conn,card,amount,channel)
        return _plan(conn,sid,'card_payment','red' if requires_red_tier(amount) else 'yellow',
                     {'card_id':id,'name':card['name'],'last4':card['last4'],'version':card['version'],
                      'amount_cents':amount,'amount_yuan':money(amount),'channel':channel,'merchant':merchant},'确认后在模拟刷卡通道扣款。')


def issue_card(sid,id):
    if os.getenv('VERALANE_DEMO_CONTROLS','1')!='1':
        raise HTTPException(403,'模拟发卡已关闭')
    with db_session() as conn:
        conn.rollback()
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM card_applications WHERE id=? AND session_id=?',(id,sid)).fetchone()
        if row is None:
            raise HTTPException(404,'未找到该申请')
        if row['status']=='issued':
            return {'status':'issued','card_id':row['card_id'],'message':'模拟卡片已发放，未重复创建。'}
        if row['status']!='approved_pending_issue' or row['kind']!='new_card':
            raise HTTPException(409,'该申请尚未通过审批，不能发卡')
        p=json.loads(row['payload_json'])
        used={r[0] for r in conn.execute('SELECT last4 FROM cards')}
        last4=next((f'{n:04d}' for n in range(1000,10000) if f'{n:04d}' not in used),None)
        if last4 is None:
            raise HTTPException(409,'模拟卡号资源不足')
        cid='issued-'+id
        name=next(x['name'] for x in PRODUCTS if x['id']==p['product_id'])
        conn.execute("INSERT INTO cards VALUES (?,?,?,?,'active',1,100000,?,1)",(cid,USER_ID,name,last4,100000 if p['product_id']=='credit' else 0))
        conn.execute("UPDATE card_applications SET status='issued',card_id=?,decision='已发放虚构卡片；模拟刷卡仍使用账户现金，不提供真实授信。' WHERE id=?",(cid,id))
        audit(conn,sid,'demo_card_issued',{'application_id':id,'card_id':cid})
        return {'status':'issued','card_id':cid,'message':'模拟卡片已发放，可在卡片列表查看；不代表真实银行开户或授信。'}


def _check_payment(conn,card,amount,channel):
    if card['status'] != 'active' or (channel=='online' and not card['online_enabled']):
        raise HTTPException(409,'卡片状态或线上支付限制阻止了这次刷卡')
    spent = conn.execute("SELECT COALESCE(SUM(t.amount_cents),0) FROM transactions t JOIN actions a ON t.action_id=a.id WHERE t.account_id=? AND t.posted_on=? AND a.type='card_payment' AND json_extract(a.payload_json,'$.card_id')=?",(ACCOUNT_ID,business_date(conn),card['id'])).fetchone()[0]
    if spent+amount > card['payment_limit_cents']:
        raise HTTPException(409,'超过该卡当日支付限额')


def execute_payment(conn,aid,sid,payload):
    card=_card(conn,payload['card_id'])
    if card['version']!=payload['version']:
        raise HTTPException(409,'卡片控制已变化，请重新核对')
    _check_payment(conn,card,payload['amount_cents'],payload['channel'])
    debit(conn,payload['amount_cents'])
    txid='card-'+aid
    conn.execute("INSERT INTO transactions (id,account_id,posted_on,direction,amount_cents,counterparty,category,note,action_id) VALUES (?,?,?,'out',?,?,'刷卡',?,?)",(txid,ACCOUNT_ID,business_date(conn),payload['amount_cents'],payload['merchant'],'模拟刷卡 · '+card['last4'],aid))
    return {'status':'completed','action_id':aid,'transaction_id':txid,'message':'模拟刷卡成功，已扣减可用现金并写入交易。'}


def interpret(sid,message):
    text = re.split(r'备注|用途',message,maxsplit=1)[0]
    with db_session() as conn:
        cards = list(conn.execute('SELECT * FROM cards WHERE user_id=?',(USER_ID,)))
    matches=[c for c in cards if c['last4'] in text or c['name'] in text]
    if len(matches)!=1:
        return {'status':'needs_clarification','mode':'offline','message':'请选择卡号尾号，并明确临时锁卡或正式挂失；两种操作影响不同。','choices':[_public(c) for c in cards]}
    # A missing card is not enough authority to choose a reversible lock or
    # irreversible loss report. Require an explicit choice before creating an action.
    if re.search(r'找不到|丢了|遗失|不见了', text) and not re.search(r'锁卡|冻结|挂失', text):
        operations = ['report_loss'] if matches[0]['status'] == 'locked' else ['lock','report_loss']
        next_step = '当前卡已临时锁定；可维持锁定，或选择正式挂失。' if matches[0]['status'] == 'locked' else '请选择临时锁卡或正式挂失；当前没有创建操作。'
        return {'status':'needs_clarification','mode':'offline',
                'message':f"{matches[0]['name']}（尾号 {matches[0]['last4']}）找不到。{next_step}",
                'decision_card':_public(matches[0]),'operation_choices':operations}
    op=next((op for words,op in [(['挂失'],'report_loss'),(['解锁','找到了'],'unlock'),(['关闭线上','禁止线上'],'online_off'),(['开启线上','恢复线上'],'online_on'),(['锁卡','冻结','找不到'],'lock')] if any(w in text for w in words)),None)
    if not op:
        return {'status':'needs_clarification','mode':'offline','message':'请说明锁卡、解锁、挂失或线上支付限制；额度调整请填写金额。'}
    return {'status':'ok',**prepare_update(sid,matches[0]['id'],op)}
