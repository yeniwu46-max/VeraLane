"""Original ledger preservation, effective reports and nonduplicated cash scenarios."""

import asyncio
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import aa, bill_preferences, db, execution_controls, investments, recurring
from app.bill_preferences_api import Owner, router
from app.insights import query_insights
from app.service import confirm_action, direct_bill_report, process_message


SESSION = "budget-owner"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "bill_preferences.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    db.init_db()
    with db.db_session() as conn:
        bill_preferences.init_schema(conn)
    app = FastAPI()
    app.include_router(router)

    @app.post("/api/actions/{action_id}/confirm")
    def confirm(action_id: str, body: Owner):
        return confirm_action(action_id, body.session_id)

    with TestClient(app) as test_client:
        yield test_client


def read(client, sid=SESSION, period="本月"):
    response = client.get("/api/bill-preferences", params={"session_id": sid, "period": period})
    assert response.status_code == 200, response.text
    return response.json()


def classification(client, **changes):
    return client.post("/api/bill-preferences/classifications/prepare", json={"session_id": SESSION, "transaction_id": "tx-1", "category": "差旅", "reason": "出差早餐", **changes})


def budget(client, **changes):
    return client.post("/api/bill-preferences/budgets/prepare", json={"session_id": SESSION, "month": "2026-09", "category": "餐饮", "amount_yuan": "100", **changes})


def confirm(client, action, sid=SESSION):
    return client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": sid})


def save_budget(client, **changes):
    prepared = budget(client, **changes)
    assert prepared.status_code == 200, prepared.text
    action = prepared.json()["pending_action"]
    response = confirm(client, action, changes.get("session_id", SESSION))
    assert response.status_code == 200, response.text
    return response.json()["budget_id"]


def test_classification_only_changes_effective_report_after_confirmation(client):
    before = direct_bill_report("本月")
    with db.db_session() as conn:
        raw_before = dict(conn.execute("SELECT * FROM transactions WHERE id='tx-1'").fetchone())
    action = classification(client).json()["pending_action"]
    assert next(row for row in direct_bill_report("本月")["transactions"] if row["id"] == "tx-1")["category"] == "餐饮"
    assert confirm(client, action).status_code == 200
    after = direct_bill_report("本月")
    changed = next(row for row in after["transactions"] if row["id"] == "tx-1")
    assert changed["category"] == "差旅"
    assert changed["original_category"] == "餐饮"
    assert changed["classification_reason"] == "出差早餐"
    assert changed["classification_version"] == 1
    assert before["total_yuan"] == after["total_yuan"]
    assert next(row for row in after["categories"] if row["name"] == "差旅")["amount_yuan"] == "45.90"
    with db.db_session() as conn:
        assert dict(conn.execute("SELECT * FROM transactions WHERE id='tx-1'").fetchone()) == raw_before


def test_insights_query_custom_category_uses_overlay(client):
    action = classification(client, category="商务出行").json()["pending_action"]
    assert confirm(client, action).status_code == 200
    result = query_insights(SESSION, "本月商务出行花了多少")
    assert result["status"] == "ok"
    assert result["report"]["total_yuan"] == "45.90"
    assert result["report"]["transactions"][0]["original_category"] == "餐饮"


def test_classification_history_version_and_stale_write(client):
    first = classification(client).json()["pending_action"]
    stale = classification(client, category="日用").json()["pending_action"]
    assert confirm(client, first).status_code == 200
    assert confirm(client, stale).status_code == 409
    restore = classification(client, category="餐饮", reason="恢复原分类").json()["pending_action"]
    assert confirm(client, restore).status_code == 200
    assert confirm(client, restore).status_code == 200
    history = read(client)["classification_history"]
    assert len(history) == 2
    assert history[0]["version"] == 2
    assert history[0]["original_category"] == "餐饮"
    assert history[0]["previous_category"] == "差旅"


def test_classifier_owner_account_scope_and_no_income_edit(client):
    action = classification(client).json()["pending_action"]
    assert confirm(client, action, "other").status_code == 404
    assert confirm(client, action).status_code == 200
    assert classification(client, session_id="other").status_code == 404
    # Read-only effective reports still use the current account's categorization.
    assert next(row for row in read(client, sid="other")["transactions"] if row["id"] == "tx-1")["category"] == "差旅"
    assert classification(client, transaction_id="tx-7").status_code == 404
    with db.db_session() as conn:
        conn.execute("INSERT INTO accounts VALUES('other-account','other-user','other',0)")
        conn.execute("INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category) VALUES('other-tx','other-account','2026-09-30','out',100,'商户','餐饮')")
    assert classification(client, transaction_id="other-tx").status_code == 404


def test_changed_raw_transaction_refuses_old_classification(client):
    action = classification(client).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE transactions SET amount_cents=4600 WHERE id='tx-1'")
    assert confirm(client, action).status_code == 409


def test_classification_history_failure_rolls_back_override(client):
    action = classification(client).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON bill_category_history BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        confirm(client, action)
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bill_category_overrides").fetchone()[0] == 0
        assert conn.execute("SELECT category FROM transactions WHERE id='tx-1'").fetchone()[0] == "餐饮"


def test_budget_no_cash_movement_overspend_and_effective_category(client):
    before = read(client)["forecast"]["balance_yuan"]
    action = budget(client).json()["pending_action"]
    assert read(client)["budgets"] == []
    assert confirm(client, action).status_code == 200
    saved = read(client)["budgets"][0]
    assert saved["spent_yuan"] == "103.90"
    assert saved["remaining_yuan"] == "-3.90"
    assert saved["over_yuan"] == "3.90"
    classify = classification(client).json()["pending_action"]
    assert confirm(client, classify).status_code == 200
    updated = read(client)["budgets"][0]
    assert updated["spent_yuan"] == "58.00"
    assert updated["remaining_yuan"] == "42.00"
    assert read(client)["forecast"]["balance_yuan"] == before


def test_zero_budget_and_total_budget(client):
    save_budget(client, amount_yuan="0")
    save_budget(client, category=None, amount_yuan="2000")
    data = read(client)
    restaurant = next(row for row in data["budgets"] if row["category"] == "餐饮")
    total = next(row for row in data["budgets"] if row["category"] is None)
    assert restaurant["remaining_yuan"] == "-103.90"
    assert total["spent_yuan"] == data["current_month_spent_yuan"]


@pytest.mark.parametrize("changes", [{"amount_yuan": "-1"}, {"amount_yuan": "NaN"}, {"amount_yuan": "1.001"}, {"month": "2026-10"}, {"month": "2026-13"}])
def test_invalid_budget_requires_valid_current_month_and_cents(client, changes):
    assert budget(client, **changes).status_code == 422


def test_budget_owner_update_delete_and_stale_confirmation(client):
    budget_id = save_budget(client)
    assert read(client, sid="other")["budgets"] == []
    assert budget(client, budget_id=budget_id, session_id="other").status_code == 404
    old = client.post(f"/api/bill-preferences/budgets/{budget_id}/delete/prepare", json={"session_id": SESSION}).json()["pending_action"]
    update = budget(client, budget_id=budget_id, amount_yuan="200").json()["pending_action"]
    assert confirm(client, update).status_code == 200
    assert confirm(client, old).status_code == 409
    new = client.post(f"/api/bill-preferences/budgets/{budget_id}/delete/prepare", json={"session_id": SESSION}).json()["pending_action"]
    assert confirm(client, new).status_code == 200
    assert read(client)["budgets"] == []


def test_month_change_before_confirm_rejects_budget(client):
    action = budget(client).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-10-01T09:00:00+08:00'")
    assert confirm(client, action).status_code == 409


def test_discarded_edit_invalidates_confirmation(client):
    action = classification(client).json()["pending_action"]
    path = f"/api/bill-preferences/actions/{action['id']}/discard"
    assert client.post(path, json={"session_id": "other"}).status_code == 404
    assert client.post(path, json={"session_id": SESSION}).status_code == 200
    assert confirm(client, action).status_code == 409


def test_forecast_separates_authorized_estimated_and_reserved_without_double_count(client):
    with db.db_session() as conn:
        execution_controls.reserve(conn, "birthday-reserve", SESSION, 100000, "生日订单预算")
    prepared = asyncio.run(process_message(SESSION, "明天上午9点转给林悦100元"))["pending_action"]
    before = read(client)["forecast"]
    assert before["authorized_transfer_yuan"] == "0.00"
    assert before["known_debit_yuan"] == "296.00"
    assert before["reserved_yuan"] == "1000.00"
    assert before["available_yuan"] == "7888.30"
    assert confirm(client, prepared).status_code == 200
    forecast = read(client)["forecast"]
    assert forecast["authorized_transfer_yuan"] == "100.00"
    assert forecast["after_authorized_yuan"] == "7788.30"
    assert forecast["after_known_debits_yuan"] == "7492.30"
    assert {row["status"] for row in forecast["authorized_transfers"]} == {"authorized"}
    assert {row["status"] for row in forecast["known_debits"]} == {"estimated"}


def test_future_recurring_occurrences_count_only_confirmed_valid_in_window(client):
    with db.db_session() as conn:
        execution_controls.reserve(conn, "reserved-for-orders", SESSION, 100000, "生日订单")
    reply = recurring.prepare_recurring({"session_id": SESSION, "contact_id": "contact-linyue", "amount_yuan": "50", "note": "月度支持", "first_at": "2026-10-05T09:00:00+08:00", "monthly_day": 5, "count": 3})
    assert read(client)["forecast"]["authorized_transfer_yuan"] == "0.00"
    assert confirm(client, reply["pending_action"]).status_code == 200
    forecast = read(client)["forecast"]
    assert forecast["authorized_transfer_yuan"] == "50.00"  # November and December are outside 30 days.
    assert forecast["available_yuan"] == "7888.30"
    assert forecast["after_authorized_yuan"] == "7838.30"
    assert forecast["authorized_transfers"][0]["kind"] == "recurring_transfer"
    occurrence_id = forecast["authorized_transfers"][0]["id"]
    with db.db_session() as conn:
        conn.execute("UPDATE recurring_occurrences SET execute_at='2026-10-06T09:00:00+08:00' WHERE id=?", (occurrence_id,))
    assert read(client)["forecast"]["authorized_transfer_yuan"] == "0.00"  # Tampered child is not authorization evidence.


def test_expired_recurring_window_not_counted_as_future_cash(client):
    reply = recurring.prepare_recurring({"session_id": SESSION, "contact_id": "contact-linyue", "amount_yuan": "50", "note": "月度支持", "first_at": "2026-10-05T09:00:00+08:00", "monthly_day": 5, "count": 2})
    assert confirm(client, reply["pending_action"]).status_code == 200
    with db.db_session() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-10-05T09:11:00+08:00'")
    assert read(client)["forecast"]["authorized_transfer_yuan"] == "0.00"  # Missed October is not an automatic retry; November 5 is beyond 30 days.


def test_aa_receivable_not_counted_as_spendable_cash(client):
    before = read(client)["forecast"]
    reply = aa.prepare({"session_id": SESSION, "total_yuan": "300.00", "contact_ids": ["contact-linyue"], "include_self": True, "note": "聚餐", "source_transaction_id": None, "shares_yuan": None})
    assert confirm(client, reply["pending_action"]).status_code == 200
    after = read(client)["forecast"]
    assert after["available_yuan"] == before["available_yuan"]
    assert after["after_known_debits_yuan"] == before["after_known_debits_yuan"]


def test_pending_redemption_is_disclosed_but_not_added_to_cash(client):
    investments.assess(SESSION, [3, 3, 3, 3, 3])
    action = investments.prepare_buy(SESSION, "product-stable", "100")["pending_action"]
    challenge = execution_controls.issue_challenge(action["id"], SESSION)
    execution_controls.verify_challenge(action["id"], SESSION, challenge["challenge_id"], challenge["demo_code"])
    receipt = confirm(client, action).json()
    before = read(client)["forecast"]
    redemption = investments.prepare_redeem(SESSION, receipt["holding_id"], "50")["pending_action"]
    challenge = execution_controls.issue_challenge(redemption["id"], SESSION)
    execution_controls.verify_challenge(redemption["id"], SESSION, challenge["challenge_id"], challenge["demo_code"])
    assert confirm(client, redemption).status_code == 200
    after = read(client)["forecast"]
    assert after["pending_settlements"][0]["amount_yuan"] == "50.00"
    assert after["available_yuan"] == before["available_yuan"]
    assert after["after_known_debits_yuan"] == before["after_known_debits_yuan"]


def test_missing_history_period_is_clear_and_extra_input_rejected(client):
    assert read(client, period="去年")["transactions"] == []
    assert client.get("/api/bill-preferences", params={"session_id": SESSION, "period": "2030-01"}).status_code == 422
    assert client.post("/api/bill-preferences/classifications/prepare", json={"session_id": SESSION, "transaction_id": "tx-1", "category": "差旅", "reason": "出差", "amount_yuan": "1"}).status_code == 422
