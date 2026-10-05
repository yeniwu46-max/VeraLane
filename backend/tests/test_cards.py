import pytest
from fastapi import HTTPException
from app import db, cards, execution_controls as funds
from app.service import confirm_action, direct_prepare_transfer


@pytest.fixture(autouse=True)
def state(tmp_path,monkeypatch):
    monkeypatch.setattr(db,'DB_PATH',tmp_path/'cards.sqlite3')
    monkeypatch.setenv('VERALANE_DEMO_CONTROLS','1')
    db.init_db()


def run(reply,sid='owner'):
    a=reply['pending_action']
    if a['tier']=='red':
        c=funds.issue_challenge(a['id'],sid)
        funds.verify_challenge(a['id'],sid,c['challenge_id'],c['demo_code'])
    return confirm_action(a['id'],sid)


def balance():
    with db.db_session() as c:
        return c.execute('SELECT balance_cents FROM accounts').fetchone()[0]


def test_lock_enforces_rail_but_does_not_freeze_account():
    run(cards.prepare_update('owner','card-main','lock'))
    with pytest.raises(HTTPException) as e:
        cards.prepare_payment('owner','card-main','10','pos','虚构商店')
    assert e.value.status_code==409
    run(direct_prepare_transfer('owner','contact-linyue','10','转账'))
    run(cards.prepare_update('owner','card-main','unlock'))
    run(cards.prepare_payment('owner','card-main','10','pos','虚构商店'))
    assert balance()==886830


def test_online_only_and_cumulative_limit():
    run(cards.prepare_update('owner','card-main','online_off'))
    with pytest.raises(HTTPException):
        cards.prepare_payment('owner','card-main','20','online','商店')
    run(cards.prepare_update('owner','card-main','payment_limit','30'))
    run(cards.prepare_payment('owner','card-main','20','pos','商店'))
    with pytest.raises(HTTPException):
        cards.prepare_payment('owner','card-main','11','pos','商店')


def test_changed_card_snapshot_prevents_old_payment():
    p=cards.prepare_payment('owner','card-main','20','pos','商店')
    run(cards.prepare_update('owner','card-main','lock'))
    with pytest.raises(HTTPException):
        run(p)
    assert balance()==888830


def test_lost_requires_verification_and_cannot_unlock():
    p=cards.prepare_update('owner','card-main','report_loss')
    with pytest.raises(HTTPException) as e:
        confirm_action(p['pending_action']['id'],'owner')
    assert e.value.status_code==403
    run(p)
    with pytest.raises(HTTPException):
        cards.prepare_update('owner','card-main','unlock')


def test_reservation_prevents_card_and_transfer_spending():
    run(funds.prepare_reserve('owner','8800','用途'))
    p=cards.prepare_payment('owner','card-main','100','pos','商店')
    with pytest.raises(HTTPException):
        run(p)
    assert 'pending_action' not in direct_prepare_transfer('owner','contact-linyue','100','测试')
    assert balance()==888830


def test_card_payment_idempotent_and_rollback():
    p=cards.prepare_payment('owner','card-main','10','pos','商店')
    a=run(p)
    assert run(p)==a
    assert balance()==887830
    p=cards.prepare_payment('owner','card-main','10','pos','商店')
    with db.db_session() as c:
        c.execute("CREATE TRIGGER block_card BEFORE INSERT ON transactions WHEN NEW.category='刷卡' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(Exception):
        run(p)
    assert balance()==887830


def test_applicant_state_no_automatic_card_or_credit():
    before=cards.snapshot('owner')['cards']
    a=run(cards.prepare_application('owner','credit','虚构用户','5000'))
    row=cards.snapshot('owner')['applications'][0]
    assert row['status']=='submitted'
    with pytest.raises(HTTPException):
        cards.review_application('other',a['application_id'])
    result=cards.review_application('owner',a['application_id'])
    assert result['status']=='approved_pending_issue'
    assert cards.review_application('owner',a['application_id'])==result
    assert cards.snapshot('owner')['cards']==before
    a=run(cards.prepare_update('owner','card-travel','credit_increase','6000'))
    assert cards.review_application('owner',a['application_id'])['status']=='manual_review'
    assert cards.snapshot('owner')['cards']==before


def test_card_issuance_requires_approval_and_is_idempotent():
    a=run(cards.prepare_application('owner','debit','虚构用户','5000'))['application_id']
    with pytest.raises(HTTPException):
        cards.issue_card('owner',a)
    cards.review_application('owner',a)
    result=cards.issue_card('owner',a)
    assert cards.issue_card('owner',a)['card_id']==result['card_id']
    assert len(cards.snapshot('owner')['cards'])==3
    assert balance()==888830


def test_no_invented_card_or_relationship():
    result=cards.interpret('owner','卡找不到了')
    assert result['status']=='needs_clarification' and 'pending_action' not in result
    p=cards.interpret('owner','锁卡6018，备注解锁9026')
    assert p['pending_action']['details']['card_id']=='card-main'


def test_missing_card_requires_explicit_lock_or_loss_choice():
    result=cards.interpret('owner','6018卡找不到了')
    assert result['status']=='needs_clarification'
    assert result['decision_card']['id']=='card-main'
    assert result['operation_choices']==['lock','report_loss']
    assert 'pending_action' not in result

    temporary=cards.interpret('owner','6018卡找不到了，锁卡')
    assert temporary['pending_action']['details']['operation']=='lock'
    formal=cards.interpret('owner','6018卡不见了，正式挂失')
    assert formal['pending_action']['details']['operation']=='report_loss'

    run(cards.prepare_update('owner','card-main','lock'))
    locked=cards.interpret('owner','6018卡找不到了')
    assert locked['operation_choices']==['report_loss']
    assert '已临时锁定' in locked['message']


@pytest.mark.parametrize('message', [
    '6018卡不要挂失',
    '不要给6018卡挂失',
    '6018卡怎么挂失',
    '如何正式挂失6018卡',
])
def test_card_help_and_negative_requests_do_not_prepare_operations(message):
    result = cards.interpret('owner', message)
    assert result['status'] == 'needs_clarification'
    assert 'pending_action' not in result
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='card_update'").fetchone()[0] == 0


def test_negative_card_request_preserves_an_existing_pending_operation():
    pending = cards.prepare_update('owner', 'card-travel', 'online_off')['pending_action']
    with db.db_session() as conn:
        before_rows = [tuple(row) for row in conn.execute('SELECT id,type,status,payload_json FROM actions ORDER BY id')]

    result = cards.interpret('owner', '9026不要关闭线上支付')
    assert result['status'] == 'needs_clarification'
    assert 'pending_action' not in result

    with db.db_session() as conn:
        after_rows = [tuple(row) for row in conn.execute('SELECT id,type,status,payload_json FROM actions ORDER BY id')]
    assert after_rows == before_rows
    assert any(row[0] == pending['id'] and row[2] == 'pending' for row in after_rows)


def test_high_transfer_confirmation_bound_and_replay():
    p=direct_prepare_transfer('owner','contact-linyue','1200','验证')
    aid=p['pending_action']['id']
    with pytest.raises(HTTPException):
        confirm_action(aid,'owner')
    run(p)
    assert balance()==768830
    confirm_action(aid,'owner')
    assert balance()==768830


def test_subscription_snapshot_changes_block_cancel():
    from app.service import direct_prepare_cancel
    p=direct_prepare_cancel('owner','sub-music')
    with db.db_session() as c:
        c.execute("UPDATE subscriptions SET amount_cents=15000 WHERE id='sub-music'")
    with pytest.raises(HTTPException):
        run(p)
    with db.db_session() as c:
        assert c.execute("SELECT status FROM subscriptions WHERE id='sub-music'").fetchone()[0]=='active'
