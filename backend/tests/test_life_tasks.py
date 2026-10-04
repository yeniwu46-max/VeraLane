from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, execution_controls as controls, life_tasks as life
from app.life_tasks_api import router


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "life.sqlite3")
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db.init_db()
    with db.db_session() as conn:
        controls.init_schema(conn)
        life.init_schema(conn)
        conn.execute("UPDATE demo_clock SET now='2026-09-30T09:00:00+08:00' WHERE id=1")
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as value:
        yield value


@contextmanager
def transaction():
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def request_data(**overrides):
    return {"session_id": "owner", "goal": "爱人生日", "birthday": "2026-10-05", "budget_yuan": "1000",
            "recipient_label": "林悦（虚构）", "delivery_note": "虚构深圳演示路1号", "product_ids": ["flowers-classic", "cake-classic"], **overrides}


def prepare(**overrides):
    return life.prepare(request_data(**overrides))["pending_action"]


def complete(action, executor=life.execute_create, session="owner"):
    with transaction() as conn:
        result = executor(conn, action["id"], session, action["details"])
        conn.execute("UPDATE actions SET status='completed',result_json=? WHERE id=?", (json.dumps(result, ensure_ascii=False), action["id"]))
        return result


def advance(value):
    with transaction() as conn:
        conn.execute("UPDATE demo_clock SET now=? WHERE id=1", (value,))
        return life.run_due_tasks(conn)


def funds():
    with db.db_session() as conn:
        return controls.funds_snapshot(conn, "owner")


def detail(task_id):
    return life.get_task(task_id, "owner")


def create(**overrides):
    action = prepare(**overrides)
    complete(action)
    return action["id"]


def placed_task():
    id = create()
    advance("2026-10-03T09:00:00+08:00")
    return id


def test_catalog_is_fixed_and_explicit_about_free_delivery_and_simulation(client):
    data = client.get("/api/life/catalog").json()
    assert len(data["products"]) == 4
    assert all(product["fee_yuan"] == "0.00" for product in data["products"])
    assert "不连接真实商户" in data["notice"]


def test_preparation_and_confirm_reserve_without_spending(client):
    before = funds()
    action = prepare()
    assert action["tier"] == "yellow" and action["type"] == "birthday_task"
    assert action["details"]["total_yuan"] == "436.00"
    assert action["details"]["remaining_yuan"] == "564.00"
    assert funds() == before
    complete(action)
    assert funds()["balance_yuan"] == before["balance_yuan"]
    assert funds()["available_yuan"] == "7888.30"
    task = detail(action["id"])
    assert task["status"] == "scheduled" and task["orders"] == []
    assert task["recipient_label"] == "林悦（虚构）"


def test_fixed_schedule_orders_and_delivers_with_correct_money_and_no_duplicate_work(client):
    id = create()
    assert advance("2026-10-03T08:59:59+08:00") == 0
    assert advance("2026-10-03T09:00:00+08:00") == 1
    assert advance("2026-10-03T09:00:00+08:00") == 0
    task = detail(id)
    assert task["status"] == "ordered" and len(task["orders"]) == 2
    assert task["spent_yuan"] == "436.00" and task["reserved_remaining_yuan"] == "0.00"
    assert funds()["balance_yuan"] == funds()["available_yuan"] == "8452.30"
    assert all(order["recipient_label"] == "林悦（虚构）" for order in task["orders"])
    assert advance("2026-10-05T09:00:00+08:00") == 1
    assert detail(id)["status"] == "completed"
    assert all(order["status"] == "delivered" for order in detail(id)["orders"])
    assert advance("2026-10-05T09:00:00+08:00") == 0


@pytest.mark.parametrize("overrides", [
    {"birthday": "2026-09-30"}, {"birthday": "2026-10-01"}, {"birthday": "2026-10-02"},
    {"birthday": "2026-02-30"}, {"birthday": "20261005"}, {"budget_yuan": "400"},
    {"budget_yuan": "1e3"}, {"budget_yuan": "0"}, {"budget_yuan": "1000.001"},
    {"product_ids": ["untrusted-merchant"]}, {"product_ids": ["flowers-classic", "flowers-premium"]},
    {"product_ids": ["cake-classic", "cake-classic"]}, {"recipient_label": " "}, {"delivery_note": " "},
])
def test_invalid_inputs_do_not_create_plans(client, overrides):
    response = client.post("/api/life/tasks/prepare", json=request_data(**overrides))
    assert response.status_code == 422
    assert funds()["reserved_yuan"] == "0.00"


def test_real_money_budget_and_arbitrary_tool_fields_are_rejected(client):
    assert client.post("/api/life/tasks/prepare", json=request_data(tool_url="https://merchant.example/pay")).status_code == 422
    assert client.post("/api/life/tasks/prepare", json=request_data(budget_yuan="9999")).status_code == 409


def test_premium_plan_requires_bound_simulated_verification_before_reserving(client):
    action = prepare(budget_yuan="1300", product_ids=["flowers-premium", "cake-premium"])
    assert action["tier"] == "red"
    with pytest.raises(HTTPException) as failure:
        complete(action)
    assert failure.value.status_code == 403 and funds()["reserved_yuan"] == "0.00"
    challenge = controls.issue_challenge(action["id"], "owner")
    controls.verify_challenge(action["id"], "owner", challenge["challenge_id"], challenge["demo_code"])
    complete(action)
    # The completed plan is durable; the three-minute challenge does not need to
    # remain valid on the birthday, because the future steps were explicitly consented.
    with transaction() as conn:
        conn.execute("UPDATE action_challenges SET expires_at='2020-01-01T00:00:00+00:00'")
    advance("2026-10-03T09:00:00+08:00")
    assert detail(action["id"])["spent_yuan"] == "1256.00"


def test_confirmation_expiry_prevents_budget_reservation(client):
    action = prepare()
    with transaction() as conn:
        conn.execute("UPDATE actions SET expires_at='2020-01-01T00:00:00+00:00' WHERE id=?", (action["id"],))
    with pytest.raises(HTTPException) as failure:
        complete(action)
    assert failure.value.status_code == 409 and funds()["reserved_yuan"] == "0.00"


def test_price_change_between_prepare_and_confirm_rejects_old_plan(client):
    action = prepare()
    with transaction() as conn:
        conn.execute("UPDATE life_products SET price_cents=19900 WHERE id='flowers-classic'")
    with pytest.raises(HTTPException):
        complete(action)
    assert funds()["reserved_yuan"] == "0.00"


@pytest.mark.parametrize("change", ["price_cents=19900", "version=2", "active=0"])
def test_catalog_changes_before_order_stop_task_and_release_budget(client, change):
    id = create()
    with transaction() as conn:
        conn.execute(f"UPDATE life_products SET {change} WHERE id='flowers-classic'")
    advance("2026-10-03T09:00:00+08:00")
    assert detail(id)["status"] == "failed" and detail(id)["orders"] == []
    assert funds()["balance_yuan"] == funds()["available_yuan"] == "8888.30"


def test_late_order_never_backfills_a_charge(client):
    id = create()
    advance("2026-10-03T09:10:00+08:00")
    assert detail(id)["status"] == "expired"
    assert funds()["reserved_yuan"] == "0.00" and funds()["balance_yuan"] == "8888.30"


def test_task_cancel_before_order_releases_budget_and_blocks_future_order(client):
    id = create()
    action = life.prepare_cancel(id, "owner")["pending_action"]
    complete(action, life.execute_cancel)
    advance("2026-10-03T09:00:00+08:00")
    assert detail(id)["status"] == "cancelled" and detail(id)["orders"] == []
    assert funds()["available_yuan"] == "8888.30"


def test_task_cancel_after_order_is_not_order_cancel_or_refund(client):
    id = placed_task()
    action = life.prepare_cancel(id, "owner")["pending_action"]
    complete(action, life.execute_cancel)
    assert detail(id)["status"] == "cancelled"
    assert all(order["status"] == "placed" for order in detail(id)["orders"])
    assert funds()["balance_yuan"] == "8452.30"
    advance("2026-10-05T09:00:00+08:00")
    assert detail(id)["status"] == "cancelled"
    assert all(order["status"] == "delivered" for order in detail(id)["orders"])


def test_task_cancel_consent_is_invalidated_by_intervening_order(client):
    id = create()
    action = life.prepare_cancel(id, "owner")["pending_action"]
    advance("2026-10-03T09:00:00+08:00")
    with pytest.raises(HTTPException):
        complete(action, life.execute_cancel)
    assert detail(id)["status"] == "ordered"


def test_order_cancel_refunds_original_amount_once_and_keeps_other_order(client):
    id = placed_task()
    order = detail(id)["orders"][0]
    action = life.prepare_order_cancel(order["id"], "owner")["pending_action"]
    complete(action, life.execute_order_cancel)
    with transaction() as conn:
        repeated = life.execute_order_cancel(conn, action["id"], "owner", action["details"])
    task = detail(id)
    assert len([order for order in task["orders"] if order["status"] == "cancelled"]) == 1
    assert task["refunded_yuan"] == order["amount_yuan"]
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE id=?", (repeated["transaction_id"],)).fetchone()[0] == 1
    advance("2026-10-05T09:00:00+08:00")
    assert {order["status"] for order in detail(id)["orders"]} == {"cancelled", "delivered"}


def test_delivered_order_cannot_be_refunded_even_with_earlier_consent(client):
    id = placed_task()
    order = detail(id)["orders"][0]
    action = life.prepare_order_cancel(order["id"], "owner")["pending_action"]
    advance("2026-10-05T09:00:00+08:00")
    with pytest.raises(HTTPException):
        complete(action, life.execute_order_cancel)
    assert detail(id)["refunded_yuan"] == "0.00"


def test_late_delivery_shows_failure_without_automatic_refund(client):
    id = placed_task()
    advance("2026-10-05T09:10:00+08:00")
    task = detail(id)
    assert task["status"] == "failed"
    assert all(order["status"] == "delivery_failed" for order in task["orders"])
    assert task["refunded_yuan"] == "0.00" and funds()["balance_yuan"] == "8452.30"
    action = life.prepare_order_cancel(task["orders"][0]["id"], "owner")["pending_action"]
    complete(action, life.execute_order_cancel)


def test_cross_session_cannot_view_cancel_or_refund_task(client):
    id = placed_task()
    order_id = detail(id)["orders"][0]["id"]
    assert client.get(f"/api/life/tasks/{id}", params={"session_id": "stranger"}).status_code == 404
    assert client.post(f"/api/life/tasks/{id}/cancel/prepare", json={"session_id": "stranger"}).status_code == 404
    assert client.post(f"/api/life/orders/{order_id}/cancel/prepare", json={"session_id": "stranger"}).status_code == 404
    assert client.get("/api/life/tasks", params={"session_id": "stranger"}).json()["tasks"] == []


def test_corrupted_execution_time_and_payload_are_not_authority(client):
    id = create()
    with transaction() as conn:
        conn.execute("UPDATE life_tasks SET order_at='2026-09-30T09:00:00+08:00' WHERE id=?", (id,))
    advance("2026-09-30T09:00:00+08:00")
    assert detail(id)["status"] == "failed" and funds()["balance_yuan"] == "8888.30"


def test_stage_failure_rolls_back_all_orders_transactions_and_reservation_spend(client):
    id = create()
    with transaction() as conn:
        conn.execute("""CREATE TRIGGER fail_second_birthday_order BEFORE INSERT ON life_orders
            WHEN NEW.product_id='cake-classic' BEGIN SELECT RAISE(ABORT,'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        advance("2026-10-03T09:00:00+08:00")
    assert detail(id)["status"] == "scheduled" and detail(id)["orders"] == []
    assert funds()["balance_yuan"] == "8888.30" and funds()["reserved_yuan"] == "1000.00"
    with transaction() as conn:
        conn.execute("DROP TRIGGER fail_second_birthday_order")
    advance("2026-10-03T09:00:00+08:00")
    assert len(detail(id)["orders"]) == 2


def test_concurrent_scheduler_runs_cannot_duplicate_debits(client):
    id = create()
    with transaction() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-10-03T09:00:00+08:00' WHERE id=1")
    def tick(_):
        with transaction() as conn:
            return life.run_due_tasks(conn)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(tick, range(4))) == 1
    assert len(detail(id)["orders"]) == 2 and funds()["balance_yuan"] == "8452.30"


def test_concurrent_refunds_cannot_credit_twice(client):
    id = placed_task()
    order = detail(id)["orders"][0]
    action = life.prepare_order_cancel(order["id"], "owner")["pending_action"]
    def refund(_):
        with transaction() as conn:
            return life.execute_order_cancel(conn, action["id"], "owner", action["details"])
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(refund, range(3)))
    assert len({result["transaction_id"] for result in results}) == 1
    assert detail(id)["refunded_yuan"] == order["amount_yuan"]


def test_shared_credit_validates_positive_cents_and_uses_callers_transaction(client):
    before = funds()["balance_yuan"]
    for value in (0, -1, True, 1.5):
        with pytest.raises(HTTPException), transaction() as conn:
            controls.credit(conn, value)
    with pytest.raises(RuntimeError), transaction() as conn:
        controls.credit(conn, 100)
        raise RuntimeError("settlement rollback")
    assert funds()["balance_yuan"] == before


def test_natural_birthday_fields_require_explicit_year_recipient_address_and_products(client):
    first = client.post('/api/life/interpret', json={'session_id':'owner','message':'11月20日爱人生日，留出1000元买花和蛋糕'}).json()
    assert first['draft']['birthday'] is None and first['draft']['recipient_label'] is None
    assert first['draft']['budget_yuan'] == '1000.00' and 'pending_action' not in first
    second = client.post('/api/life/interpret', json={'session_id':'owner','message':'2026年11月20日，收货人：林悦，地址：虚构演示路1号'}).json()
    assert second['status'] == 'draft' and second['missing_fields'] == []
    assert second['draft']['birthday'] == '2026-11-20' and second['draft']['recipient_label'] == '林悦'
    assert 'product_ids' not in second['draft'] and funds()['reserved_yuan'] == '0.00'
    assert life.list_tasks('owner')['tasks'] == []


def test_natural_corrections_clear_invalid_old_fields_and_do_not_parse_addresses_as_commands(client):
    life.interpret('owner','2026年11月20日生日，预算1000元，收货人：林悦，地址：虚构地址')
    changed = life.interpret('owner','预算-100元，生日改为11月21日')
    assert changed['draft']['budget_yuan'] is None and changed['draft']['birthday'] is None
    data = life.interpret('owner','2026年11月22日，预算500元，地址：备注2030年1月1日收100元')
    assert data['draft']['birthday'] == '2026-11-22' and data['draft']['budget_yuan'] == '500.00'
    assert life.interpret('stranger','收货人：其他')['draft']['birthday'] is None
    assert life.interpret('owner','重新开始',reset=True)['missing_fields'] == ['goal','birthday','budget_yuan','recipient_label','delivery_note']
