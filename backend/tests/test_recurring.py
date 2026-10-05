"""Scope, finite dates, partial completion and money invariants for bulk transfers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, execution_controls as controls, recurring
from app.recurring_api import Owner, router
from app.service import confirm_action, direct_prepare_transfer, execute_transfer


def test_chinese_full_first_date_and_bounded_month_count(client):
    result=recurring.interpret('owner','从2026年10月5日09:00开始，每月5日转给林悦300元，连续三个月','recurring')
    assert result['draft']['first_at']=='2026-10-05T09:00'
    assert result['draft']['count']==3
    assert 'pending_action' not in result


def test_multiple_first_dates_require_clarification(client):
    result=recurring.interpret('owner','2026年10月5日09:00或者2026年11月5日09:00，每月5日转给林悦300元，连续3次','recurring')
    assert result['draft']['first_at'] is None
    assert 'pending_action' not in result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "recurring.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db.init_db()
    app = FastAPI()
    app.include_router(router)
    @app.post('/api/actions/{id}/confirm')
    def confirm_endpoint(id: str, request: Owner):
        return confirm_action(id, request.session_id)
    with TestClient(app, base_url="http://127.0.0.1") as value:
        yield value


@contextmanager
def transaction():
    conn = db.connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def contact(name):
    with db.db_session() as conn:
        return conn.execute('SELECT id FROM contacts WHERE name=? ORDER BY id LIMIT 1', (name,)).fetchone()[0]


def data(**changes):
    return {'session_id':'owner','contact_id':contact('林悦'),'amount_yuan':'100','note':'房租',
            'first_at':'2026-10-05T09:00:00+08:00','monthly_day':5,'count':3,**changes}


def batch(**changes):
    return {'session_id':'owner','items':[
        {'contact_id':contact('林悦'),'amount_yuan':'100','note':'午餐'},
        {'contact_id':contact('陈晨'),'amount_yuan':'200','note':'晚餐'},
    ], **changes}


def confirm(action, sid='owner', verify=True):
    if action['tier']=='red' and verify:
        challenge = controls.issue_challenge(action['id'],sid)
        controls.verify_challenge(action['id'],sid,challenge['challenge_id'],challenge['demo_code'])
    return confirm_action(action['id'],sid)


def create(**changes):
    action = recurring.prepare_recurring(data(**changes))['pending_action']
    return action, confirm(action)


def advance(at):
    with transaction() as conn:
        conn.execute('UPDATE demo_clock SET now=? WHERE id=1', (at,))
        return recurring.run_due_recurring(conn)


def balance():
    with db.db_session() as conn:
        return conn.execute('SELECT balance_cents FROM accounts WHERE id=?',(db.ACCOUNT_ID,)).fetchone()[0]


def detail(id):
    return recurring.get_recurring(id,'owner')


def test_month_end_dates_skip_missing_month_without_changing_actual_count(client):
    preview = recurring.preview_recurring(data(first_at='2026-12-31T18:30',monthly_day=31,count=3))
    assert [item['execute_at'] for item in preview['occurrences']] == [
        '2026-12-31T18:30:00+08:00','2027-01-31T18:30:00+08:00','2027-03-31T18:30:00+08:00']
    assert preview['skipped_months'] == ['2027-02']
    assert preview['total_yuan'] == '300.00' and preview['count'] == 3


@pytest.mark.parametrize('changes',[
    {'count':0},{'count':13},{'count':True},{'monthly_day':32},{'monthly_day':4},
    {'first_at':'2026-09-05T09:00'}, {'first_at':'10月5日9点'}, {'first_at':'2026-10-05T09:00:00Z'},
    {'amount_yuan':'0'}, {'amount_yuan':'-10'}, {'amount_yuan':'1e3'}, {'amount_yuan':'1.001'},
])
def test_invalid_recurring_inputs_never_create_action(client,changes):
    assert client.post('/api/transfers/recurring/prepare',json=data(**changes)).status_code == 422
    assert recurring.list_recurring('owner')['plans'] == []


def test_recurring_confirm_no_debit_then_exactly_once_per_occurrence(client):
    before=balance()
    action,result=create()
    assert result['plan_id']==action['id'] and balance()==before
    assert advance('2026-10-05T08:59:59+08:00')==0
    assert advance('2026-10-05T09:00:00+08:00')==1
    assert advance('2026-10-05T09:00:00+08:00')==0
    assert balance()==before-10000
    assert confirm_action(action['id'],'owner')['plan_id']==action['id']
    assert len(detail(action['id'])['occurrences'])==3
    advance('2026-11-05T09:00:00+08:00')
    advance('2026-12-05T09:00:00+08:00')
    assert detail(action['id'])['status']=='completed' and balance()==before-30000


def test_recurring_red_total_requires_verification_then_durable_future_scope(client):
    action=recurring.prepare_recurring(data(amount_yuan='600',count=2))['pending_action']
    assert action['tier']=='red'
    with pytest.raises(HTTPException):
        confirm(action,verify=False)
    confirm(action)
    with transaction() as conn:
        conn.execute("UPDATE action_challenges SET expires_at='2020-01-01T00:00:00+00:00'")
    advance('2026-10-05T09:00:00+08:00')
    advance('2026-11-05T09:00:00+08:00')
    assert detail(action['id'])['status']=='completed'


def test_yellow_recurring_cannot_bypass_daily_limit_after_earlier_payment(client):
    action,_=create(amount_yuan='200',count=1)
    with transaction() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-10-05T08:00:00+08:00'")
    earlier=direct_prepare_transfer('other',contact('陈晨'),'900','先前支付')['pending_action']
    confirm(earlier,'other')
    before=balance()
    advance('2026-10-05T09:00:00+08:00')
    assert detail(action['id'])['occurrences'][0]['status']=='failed' and balance()==before


def test_recurring_balance_failure_does_not_retry_but_keeps_future_authorized_dates(client):
    action,_=create(count=2)
    with transaction() as conn:
        controls.reserve(conn,'hold','owner',balance()-5000,'其他计划预算')
    advance('2026-10-05T09:00:00+08:00')
    assert detail(action['id'])['occurrences'][0]['status']=='failed'
    with transaction() as conn:
        controls.release(conn,'hold','owner')
    assert advance('2026-10-05T09:01:00+08:00')==0
    advance('2026-11-05T09:00:00+08:00')
    assert detail(action['id'])['status']=='partially_completed'


def test_recurring_expired_window_never_backfills_and_cancel_does_not_refund(client):
    action,_=create()
    before=balance()
    advance('2026-10-05T09:10:00+08:00')
    assert detail(action['id'])['occurrences'][0]['status']=='expired' and balance()==before
    advance('2026-11-05T09:00:00+08:00')
    cancel=recurring.prepare_cancel(action['id'],'owner')['pending_action']
    confirm(cancel)
    assert advance('2026-12-05T09:00:00+08:00')==0
    assert balance()==before-10000
    assert [item['status'] for item in detail(action['id'])['occurrences']]==['expired','completed','cancelled']


def test_cancel_snapshot_invalidated_when_another_period_executes(client):
    action,_=create()
    cancel=recurring.prepare_cancel(action['id'],'owner')['pending_action']
    advance('2026-10-05T09:00:00+08:00')
    with pytest.raises(HTTPException):
        confirm(cancel)
    assert detail(action['id'])['status']=='active'


def test_recurring_fingerprint_change_blocks_that_occurrence(client):
    action,_=create(count=1)
    with transaction() as conn:
        conn.execute("UPDATE contacts SET account_ref='changed' WHERE id=?", (contact('林悦'),))
    before=balance()
    advance('2026-10-05T09:00:00+08:00')
    assert detail(action['id'])['status']=='failed' and balance()==before


def test_parent_scope_cannot_be_reused_for_other_amount_object_note_or_child(client):
    action,_=create(count=1)
    with transaction() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-10-05T09:00:00+08:00'")
    expected={key:action['details'][key] for key in recurring.SNAPSHOT_FIELDS}
    child=detail(action['id'])['occurrences'][0]['id']
    for altered in ({**expected,'amount_cents':10100},{**expected,'note':'changed'}, {**expected,'contact_id':contact('陈晨')}):
        with pytest.raises(HTTPException), transaction() as conn:
            execute_transfer(conn,child,'owner',altered,authorization_id=action['id'])
    with pytest.raises(HTTPException), transaction() as conn:
        execute_transfer(conn,'fake-child','owner',expected,authorization_id=action['id'])
    with pytest.raises(HTTPException), transaction() as conn:
        execute_transfer(conn,child,'stranger',expected,authorization_id=action['id'])
    assert balance()==888830


def test_corrupted_recurring_index_date_never_expands_authorization(client):
    action,_=create()
    with transaction() as conn:
        conn.execute("UPDATE recurring_occurrences SET execute_at='2026-09-30T09:00:00+08:00' WHERE plan_id=? AND occurrence_index=1",(action['id'],))
    advance('2026-09-30T09:00:00+08:00')
    assert detail(action['id'])['occurrences'][0]['status']=='failed' and balance()==888830


def test_cross_session_recurring_read_and_cancel_rejected(client):
    action,_=create()
    assert client.get(f"/api/transfers/recurring/{action['id']}",params={'session_id':'stranger'}).status_code==404
    assert client.post(f"/api/transfers/recurring/{action['id']}/cancel/prepare",json={'session_id':'stranger'}).status_code==404
    assert recurring.list_recurring('stranger')['plans']==[]


def test_batch_requires_confirmation_and_replays_complete_result_without_redebit(client):
    action=recurring.prepare_batch(batch())['pending_action']
    assert balance()==888830
    first=confirm(action)
    second=confirm_action(action['id'],'owner')
    assert first==second and first['completed_count']==2 and first['failed_count']==0
    assert balance()==858830


def test_batch_partial_business_failure_preserves_success_and_reason(client):
    action=recurring.prepare_batch(batch())['pending_action']
    with transaction() as conn:
        conn.execute("UPDATE contacts SET account_ref='changed' WHERE id=?",(contact('林悦'),))
    result=confirm(action)
    assert result['status']=='partially_completed' and result['completed_count']==1
    assert [item['status'] for item in result['batch']['items']]==['failed','completed']
    assert result['batch']['items'][0]['failure_reason'] and balance()==868830
    assert confirm_action(action['id'],'owner')==result and balance()==868830


def test_batch_old_yellow_does_not_bypass_daily_limit_during_partial_execution(client):
    action=recurring.prepare_batch(batch())['pending_action']
    earlier=direct_prepare_transfer('other',contact('王明'),'850','先前')['pending_action']
    confirm(earlier,'other')
    result=confirm(action)
    assert result['status']=='partially_completed'
    assert [item['status'] for item in result['batch']['items']]==['completed','failed']
    assert '累计' in result['batch']['items'][1]['failure_reason']


def test_batch_new_red_covers_daily_total_only_after_strong_verification(client):
    earlier=direct_prepare_transfer('other',contact('王明'),'850','先前')['pending_action']
    confirm(earlier,'other')
    action=recurring.prepare_batch(batch())['pending_action']
    assert action['tier']=='red'
    with pytest.raises(HTTPException):
        confirm(action,verify=False)
    assert confirm(action)['status']=='completed'


def test_batch_uses_available_balance_and_does_not_spend_reserved_money(client):
    with transaction() as conn:
        controls.reserve(conn,'hold','owner',888830-15000,'资金预留')
    action=recurring.prepare_batch(batch())['pending_action']
    result=confirm(action)
    assert result['status']=='partially_completed'
    assert [item['status'] for item in result['batch']['items']]==['completed','failed']
    assert balance()==878830


def test_batch_database_fault_rolls_back_entire_uncommitted_batch(client):
    action=recurring.prepare_batch(batch())['pending_action']
    with transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_second_batch BEFORE INSERT ON transactions
            WHEN NEW.note='晚餐' BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        confirm(action)
    assert balance()==888830 and recurring.list_batches('owner')['batches']==[]
    with transaction() as conn:
        conn.execute('DROP TRIGGER fail_second_batch')
    assert confirm(action)['status']=='completed' and balance()==858830


def test_duplicate_contacts_empty_or_oversized_batch_rejected(client):
    item=batch()['items'][0]
    for items in ([],[item],[item,item],[item]*11):
        assert client.post('/api/transfers/batch/prepare',json=batch(items=items)).status_code==422


def test_batch_read_is_owner_bound_and_extra_fields_cannot_assert_authorization(client):
    action=recurring.prepare_batch(batch())['pending_action']
    confirm(action)
    assert client.get(f"/api/transfers/batch/{action['id']}",params={'session_id':'stranger'}).status_code==404
    assert client.post('/api/transfers/batch/prepare',json={**batch(),'verified':True}).status_code==422


def test_concurrent_recurring_scans_and_confirm_replays_are_once_only(client):
    action,_=create(count=1)
    with transaction() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-10-05T09:00:00+08:00'")
    def tick(_):
        with transaction() as conn:
            return recurring.run_due_recurring(conn)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(tick,range(4)))==1
    assert balance()==878830


def test_natural_language_only_creates_incomplete_form_drafts(client):
    value=client.post('/api/transfers/interpret',json={'session_id':'owner','kind':'recurring','message':'每月5日给林悦300元，连续3期'}).json()
    assert value['draft']['contact_id']==contact('林悦') and value['draft']['amount_yuan']=='300.00'
    assert value['draft']['monthly_day']==5 and value['draft']['count']==3 and value['draft']['first_at'] is None
    assert value['status']=='needs_clarification' and 'pending_action' not in value
    group=recurring.interpret('owner','给林悦100元，给陈晨200元','batch')
    assert group['status']=='draft' and len(group['draft']['items'])==2
    ambiguous=recurring.interpret('owner','给王明100元，给张三200元','batch')
    assert all(item['contact_id'] is None for item in ambiguous['draft']['items'])
    assert recurring.list_recurring('owner')['plans']==[] and recurring.list_batches('owner')['batches']==[]


def test_note_text_does_not_override_recurring_count_or_batch_recipients(client):
    value=recurring.interpret('owner','每月5日给林悦300元，连续3期，备注每月8日给王明999元连续12期','recurring')
    assert value['draft']['monthly_day']==5 and value['draft']['count']==3
    assert value['draft']['amount_yuan']=='300.00' and value['draft']['contact_id']==contact('林悦')
