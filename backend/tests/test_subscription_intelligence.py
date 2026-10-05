import pytest
from fastapi.testclient import TestClient
from app import db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path/'subs.sqlite3')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def prepare(client, ids=('sub-cloud', 'sub-music')):
    response = client.post('/api/subscriptions/prepare-batch', json={'session_id':'s','subscription_ids':list(ids)})
    assert response.status_code == 200
    return response.json()['pending_action']


def test_diagnostics_returns_evidence_not_usage_assumptions(client):
    body=client.get('/api/subscriptions/diagnostics').json()
    assert body['as_of']=='2026-09-30'
    music=next(i for i in body['items'] if i['merchant']=='青柠音乐')
    assert music['status']=='active' and music['price_change_yuan']=='21.00'
    assert len(music['evidence'])==2 and music['renewal_on']=='2026-10-16'


def test_batch_confirm_receipts_and_no_cash_refund(client):
    action=prepare(client)
    assert action['details']['expected_savings_yuan']=='296.00'
    assert all(s['status']=='active' for s in client.get('/api/overview').json()['subscriptions'])
    url=f"/api/actions/{action['id']}/confirm"
    assert client.post(url,json={'session_id':'other'}).status_code==404
    result=client.post(url,json={'session_id':'s'}).json()
    assert result['status']=='completed'
    assert client.post(url,json={'session_id':'s'}).json()==result
    assert client.get('/api/overview').json()['account']['balance_yuan']=='8888.30'


def test_same_day_charge_after_cancellation_is_flagged_as_order_unknown(client):
    prepared = client.post('/api/subscriptions/sub-music/prepare-cancel', json={'session_id': 's'})
    action = prepared.json()['pending_action']
    assert client.post(f"/api/actions/{action['id']}/confirm", json={'session_id': 's'}).status_code == 200
    with db.db_session() as conn:
        cancelled_on = conn.execute("SELECT cancelled_at FROM subscription_closures WHERE subscription_id='sub-music'").fetchone()[0][:10]
        conn.execute("""INSERT INTO transactions
            (id,account_id,posted_on,direction,amount_cents,counterparty,category,note)
            VALUES('same-day-after-cancel',? ,?,'out',2100,'青柠音乐','数字服务','同日模拟扣费')""",
            (db.ACCOUNT_ID, cancelled_on))
    item = next(i for i in client.get('/api/subscriptions/diagnostics').json()['items'] if i['subscription_id'] == 'sub-music')
    assert 'same-day-after-cancel' in item['same_day_cancel_ids']
    assert 'same-day-after-cancel' not in item['after_cancel_ids']


def test_batch_partial_preserves_state_change(client):
    action=prepare(client)
    with db.db_session() as conn:
        conn.execute("UPDATE subscriptions SET amount_cents=999 WHERE id='sub-cloud'")
    result=client.post(f"/api/actions/{action['id']}/confirm",json={'session_id':'s'}).json()
    assert result['status']=='partially_completed'
    assert result['expected_savings_yuan']=='128.00'
    assert [i['status'] for i in result['items']]==['failed','completed']


def test_reminders_deduplicated_and_read_persisted(client):
    one=client.get('/api/reminders',params={'session_id':'s'}).json()['items']
    two=client.get('/api/reminders',params={'session_id':'s'}).json()['items']
    assert one==two and len(one)==2
    assert client.post(f"/api/reminders/{one[0]['id']}/read",json={'session_id':'s'}).status_code==200
    assert client.get('/api/reminders',params={'session_id':'s'}).json()['items'][0]['read_at']


def test_unknown_candidate_cannot_be_cancelled(client):
    with db.db_session() as conn:
        conn.executemany("INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note) VALUES(?,? ,?,'out',1000,'未知数字服务','数字服务','')", [('candidate1',db.ACCOUNT_ID,'2026-08-25'),('candidate2',db.ACCOUNT_ID,'2026-09-25')])
    item=next(i for i in client.get('/api/subscriptions/diagnostics').json()['items'] if i['merchant']=='未知数字服务')
    assert item['status']=='candidate' and item['renewal_on'] is None and item['estimated_renewal']
    assert client.post('/api/subscriptions/prepare-batch',json={'session_id':'s','subscription_ids':['未知数字服务']}).status_code==409


def test_atomic_database_failure_rolls_back_entire_batch(client):
    action=prepare(client)
    with db.db_session() as conn:
        conn.execute("CREATE TRIGGER fail_subscription BEFORE UPDATE ON subscriptions WHEN NEW.id='sub-music' BEGIN SELECT RAISE(ABORT,'fail'); END")
    with pytest.raises(Exception):
        client.post(f"/api/actions/{action['id']}/confirm",json={'session_id':'s'})
    assert all(s['status']=='active' for s in client.get('/api/overview').json()['subscriptions'])


def test_extra_fields_and_duplicate_ids_rejected(client):
    assert client.post('/api/subscriptions/prepare-batch',json={'session_id':'s','subscription_ids':['sub-cloud'],'amount':'1'}).status_code==422
    assert client.post('/api/subscriptions/prepare-batch',json={'session_id':'s','subscription_ids':['sub-cloud','sub-cloud']}).status_code==422
