"""Final shared-boundary regression; feature algorithms have their own tests."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app import agent, db, execution_controls, main


SESSION = "integration-owner"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "integration.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.setenv("VERALANE_MODEL_MODE", "offline")
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(main, "run_due_transfers", lambda: 0)
    with TestClient(main.app) as test_client:
        yield test_client


def post(client, path, **payload):
    return client.post(path, json={"session_id": SESSION, **payload})


def prepared(client, path, **payload):
    response = post(client, path, **payload)
    assert response.status_code == 200, response.status_code
    return response.json()["pending_action"]


def confirm(client, action):
    if action["tier"] == "red":
        challenge = post(client, f"/api/actions/{action['id']}/challenge")
        assert challenge.status_code == 200
        data = challenge.json()
        verified = post(client, f"/api/actions/{action['id']}/verify", challenge_id=data["challenge_id"], code=data["demo_code"])
        assert verified.status_code == 200
    return post(client, f"/api/actions/{action['id']}/confirm")


def test_chat_workflow_plan_is_persisted_readable_and_session_owned(client):
    response = post(client, "/api/chat", message="看看上个月的消费，下个月少花300元")
    assert response.status_code == 200
    workflow = response.json()["workflow"]
    assert workflow["view"] == "tasks"
    assert workflow["section"] == "plans"
    assert workflow["plan_id"]
    detail = client.get(f"/api/plans/{workflow['plan_id']}", params={"session_id": SESSION})
    assert detail.status_code == 200
    assert detail.json()["status"] == "draft"
    assert detail.json()["target_yuan"] == "300.00"
    assert client.get(f"/api/plans/{workflow['plan_id']}", params={"session_id": "other"}).status_code == 404
    assert client.get("/api/plans", params={"session_id": "other"}).json()["plans"] == []
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='cancelled'").fetchone()[0] == 0


@pytest.mark.parametrize("message,view,section", [
    ("帮我安排爱人生日", "tasks", "life"),
    ("我的卡找不到了，需要挂失", "cards", None),
    ("有笔钱暂时不用，比较理财产品", "investments", None),
])
def test_new_workflow_routes_preserve_input_without_claiming_execution(client, message, view, section):
    response = post(client, "/api/chat", message=message).json()
    assert response["workflow"] == {"view": view, "section": section, "message": message}
    assert response["mode"] == "offline"
    assert "pending_action" not in response
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE event='workflow_routed'").fetchone()[0] == 1


@pytest.mark.parametrize("memo", ["爱人生日礼物", "银行卡挂失后归还垫款"])
def test_memo_workflow_keywords_do_not_reroute_a_transfer(client, memo):
    response = post(client, "/api/chat", message=f"转给林悦100元，备注{memo}").json()
    assert "workflow" not in response
    assert response["pending_action"]["type"] == "transfer"
    assert response["pending_action"]["details"]["contact_id"] == "contact-linyue"
    assert response["pending_action"]["details"]["amount_yuan"] == "100.00"
    assert memo in response["pending_action"]["details"]["note"]


def test_manual_offline_with_fake_key_never_uses_network_or_billing(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-key-never-send")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline mode must stop before network and cost reservation")

    monkeypatch.setattr(agent.httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(agent, "reserve_call", forbidden)
    response = post(client, "/api/chat", message="查询余额")
    assert response.status_code == 200
    assert response.json()["mode"] == "offline"
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_usage").fetchone()[0] == 0
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_attempts'").fetchone():
            assert conn.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
        entry = conn.execute("SELECT details_json FROM audit WHERE event='intent_parsed' ORDER BY id DESC LIMIT 1").fetchone()
        assert json.loads(entry[0])["model_call"]["fallback_reason"] == "manual_offline"


def test_shared_available_balance_enforced_across_transfer_card_investment_recurring_and_birthday(client):
    # A spending budget is a reporting preference, not a second fund reservation.
    budget = prepared(client, "/api/bill-preferences/budgets/prepare", month="2026-09", category=None, amount_yuan="0")
    assert confirm(client, budget).status_code == 200
    original = client.get("/api/overview").json()["account"]
    assert original["balance_yuan"] == original["available_yuan"] == "8888.30"

    transfer = prepared(client, "/api/transfers/prepare", contact_id="contact-linyue", amount_yuan="100", note="集成验证")
    card = prepared(client, "/api/cards/card-main/payment/prepare", amount_yuan="100", channel="online", merchant="模拟验收商户")
    assert post(client, "/api/investments/assessment", answers=[3, 3, 3, 3, 3]).status_code == 200
    purchase = prepared(client, "/api/investments/buy/prepare", product_id="product-stable", amount_yuan="100")
    birthday = prepared(client, "/api/life/tasks/prepare", goal="模拟生日计划", birthday="2026-10-05", budget_yuan="200", recipient_label="虚构收件人", delivery_note="虚构地址", product_ids=["flowers-classic"])
    monthly = prepared(client, "/api/transfers/recurring/prepare", contact_id="contact-linyue", amount_yuan="100", first_at="2026-10-01T09:00:00+08:00", monthly_day=1, count=1, note="集成验证")
    with db.db_session() as conn:
        execution_controls.reserve(conn, "integration-reserve", SESSION, 883830, "已确认的模拟资金预留")
        count_before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    assert client.get("/api/overview").json()["account"]["available_yuan"] == "50.00"
    forecast = client.get("/api/bill-preferences", params={"session_id": SESSION}).json()["forecast"]
    assert forecast["available_yuan"] == "50.00"
    for action in (transfer, card, purchase, birthday):
        response = confirm(client, action)
        assert response.status_code == 409, (action["type"], response.status_code)

    # A future transfer may be authorized before funds arrive, but cannot consume
    # another purpose's reserved funds when its actual execution time comes.
    assert confirm(client, monthly).status_code == 200
    advanced = post(client, "/api/demo/clock/advance-next")
    assert advanced.status_code == 200
    with db.db_session() as conn:
        assert conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()[0] == 888830
        assert execution_controls.available_cents(conn) == 5000
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == count_before
        assert conn.execute("SELECT status FROM recurring_occurrences WHERE plan_id=?", (monthly["id"],)).fetchone()[0] == "failed"
        assert conn.execute("SELECT COUNT(*) FROM life_tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM investment_holdings").fetchone()[0] == 0


def test_production_static_mount_is_only_public_dist_and_keeps_api_priority(client):
    mounts = [route for route in main.app.routes if isinstance(route, Mount) and isinstance(route.app, StaticFiles)]
    if not mounts:
        assert not (main.ROOT / "frontend" / "dist" / "index.html").is_file()
        pytest.skip("Production frontend has not been built")
    assert len(mounts) == 1
    mount = mounts[0]
    assert Path(mount.app.directory).resolve() == (main.ROOT / "frontend" / "dist").resolve()
    assert mount.app.follow_symlink is False
    api_response = client.get("/api/health")
    page_response = client.get("/")
    assert api_response.json() == {"status": "ok"}
    assert page_response.status_code == 200
    for response in (api_response, page_response):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "script-src 'self'" in csp
        assert "object-src 'none'" in csp
        assert "strict-transport-security" not in response.headers
    # Inspect response statuses only: never print or retain a potential secret body.
    for path in ("/.env", "/backend/.env", "/data/veralane.sqlite3", "/backend/data/veralane.sqlite3", "/%2e%2e/.env", "/%2e%2e/%2e%2e/backend/data/veralane.sqlite3"):
        assert client.get(path).status_code == 404, path
