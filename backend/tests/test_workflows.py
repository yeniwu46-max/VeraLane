from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "demo.sqlite3")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def send(client, message, session_id="test-session"):
    response = client.post("/api/chat", json={"session_id": session_id, "message": message})
    assert response.status_code == 200
    return response.json()


def test_transfer_requires_confirmation_and_is_idempotent(client):
    original = client.get("/api/overview").json()["account"]["balance_yuan"]
    prepared = send(client, "转给林悦300元，备注房租")
    action = prepared["pending_action"]
    assert action["tier"] == "yellow"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == original

    url = f"/api/actions/{action['id']}/confirm"
    first = client.post(url, json={"session_id": "test-session"})
    second = client.post(url, json={"session_id": "test-session"})
    assert first.status_code == second.status_code == 200
    assert first.json()["transaction_id"] == second.json()["transaction_id"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8588.30"


def test_transfer_history_question_is_read_only_and_returns_ledger_evidence(client):
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note) "
            "VALUES (?, ?, ?, 'out', ?, ?, '转账', ?)",
            ("tx-history-lin", db.ACCOUNT_ID, "2026-09-20", 30000, "林悦", "房租"),
        )
    original_balance = client.get("/api/overview").json()["account"]["balance_yuan"]
    result = send(client, "查一下转给林悦300元的记录")
    assert "pending_action" not in result
    assert "流水 tx-history-lin" in result["message"]
    assert "不能证明外部银行" in result["message"]
    assert result["transaction_query"]["transactions"][0]["amount_yuan"] == "300.00"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == original_balance
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='transfer'").fetchone()[0] == 0
        audit = conn.execute(
            "SELECT details_json FROM audit WHERE session_id=? AND event='transfer_history_queried'",
            ("test-session",),
        ).fetchone()
        assert "tx-history-lin" in audit["details_json"]


def test_transfer_failure_question_does_not_prepare_a_new_debit(client):
    original_balance = client.get("/api/overview").json()["account"]["balance_yuan"]
    result = send(client, "为什么我转给林悦300元失败了")
    assert "pending_action" not in result
    assert "没有找到" in result["message"]
    assert "失败状态和原因不在这份流水中" in result["message"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == original_balance
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='transfer'").fetchone()[0] == 0


def test_transfer_success_after_instruction_is_not_misread_as_history_query(client):
    result = send(client, "转给林悦300元，成功后记账", session_id="success-condition")
    assert result["pending_action"]["type"] == "transfer"
    assert result["pending_action"]["details"]["recipient"] == "林悦"
    assert result["pending_action"]["details"]["amount_yuan"] == "300.00"


def test_prior_transfer_question_is_read_only(client):
    result = send(client, "我之前给林悦转过300元吗")
    assert "pending_action" not in result
    assert "没有找到" in result["message"]
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='transfer'").fetchone()[0] == 0


def test_transfer_history_query_preserves_an_unfinished_transfer_draft(client):
    partial = send(client, "转给林悦")
    assert "pending_action" not in partial
    queried = send(client, "查一下转给林悦300元的记录")
    assert "pending_action" not in queried
    completed = send(client, "300元")
    assert completed["pending_action"]["details"]["recipient"] == "林悦"
    assert completed["pending_action"]["details"]["amount_yuan"] == "300.00"


def test_transfer_history_query_applies_supported_relative_period(client):
    with db.db_session() as conn:
        conn.executemany(
            "INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note) "
            "VALUES (?, ?, ?, 'out', ?, '林悦', '转账', '')",
            [
                ("tx-history-aug", db.ACCOUNT_ID, "2026-08-20", 30000),
                ("tx-history-sep", db.ACCOUNT_ID, "2026-09-20", 30000),
                ("tx-history-2025", db.ACCOUNT_ID, "2025-08-20", 30000),
            ],
        )
    result = send(client, "查一下上个月转给林悦300元的记录")
    ids = [row["id"] for row in result["transaction_query"]["transactions"]]
    assert ids == ["tx-history-aug"]
    assert "上个月" in result["message"]
    year_result = send(client, "查一下2025年转给林悦300元的记录")
    assert [row["id"] for row in year_result["transaction_query"]["transactions"]] == ["tx-history-2025"]


def test_transfer_history_query_clarifies_unsupported_relative_period(client):
    for question in ("查一下上周转给林悦300元的记录", "查一下近三个月转给林悦300元的记录"):
        result = send(client, question)
        assert "pending_action" not in result
        assert result["transaction_query"]["needs_clarification"] is True
        assert "暂时不能可靠地解析" in result["message"]
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE event='transfer_history_queried'").fetchone()[0] == 0


def test_transfer_history_query_uses_confirmed_contact_identity_for_duplicate_names(client):
    transaction_ids = {}
    for contact_id in ("contact-wang1", "contact-wang2"):
        session_id = f"wang-session-{contact_id}"
        draft = send(client, "转给王明100元", session_id=session_id)
        selected = next(item for item in draft["choices"] if item["id"] == contact_id)
        resolved = client.post("/api/transfers/resolve", json={
            "session_id": session_id, "contact_id": selected["id"],
        })
        assert resolved.status_code == 200
        action = resolved.json()["pending_action"]
        confirmed = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session_id})
        assert confirmed.status_code == 200
        transaction_ids[contact_id] = confirmed.json()["transaction_id"]

    exact = send(client, "查一下转给13900001111的记录", session_id="phone-query")
    assert [row["id"] for row in exact["transaction_query"]["transactions"]] == [transaction_ids["contact-wang1"]]
    assert "139****1111" in exact["message"]

    name_query = send(client, "查一下王明转账记录", session_id="name-query")
    rows = name_query["transaction_query"]["transactions"]
    assert {row["id"] for row in rows} == set(transaction_ids.values())
    assert {row["recipient_phone_masked"] for row in rows} == {"139****1111", "139****2222"}
    with db.db_session() as conn:
        audit = conn.execute(
            "SELECT details_json FROM audit WHERE session_id='phone-query' AND event='transfer_history_queried'"
        ).fetchone()["details_json"]
        assert "13900001111" not in audit

    unknown_phone = send(client, "查一下转给13999990000的记录", session_id="unknown-phone-query")
    assert unknown_phone["transaction_query"]["needs_clarification"] is True
    assert unknown_phone["transaction_query"]["transactions"] == []
    mismatched = send(client, "查一下林悦13900001111的转账记录", session_id="mismatched-query")
    assert mismatched["transaction_query"]["needs_clarification"] is True


def test_explicitly_negated_transfer_never_creates_a_pending_action(client):
    balance = client.get("/api/overview").json()["account"]["balance_yuan"]
    result = send(client, "不要转给林悦100元")
    assert "pending_action" not in result
    assert "未发生扣款" in result["message"]
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='transfer'").fetchone()[0] == 0
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == balance


def test_negation_withdraws_pending_transfer_action_and_does_not_match_reminders(client):
    action = send(client, "转给林悦100元")["pending_action"]
    result = send(client, "现在不要再转给林悦了")
    assert "pending_action" not in result
    rejected = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "test-session"})
    assert rejected.status_code == 409
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"
    reminder = send(client, "不要忘记转给林悦100元", session_id="positive-reminder")
    assert reminder["pending_action"]["type"] == "transfer"


def test_transfer_reminder_preferences_do_not_withdraw_transfer_actions(client):
    action = send(client, "转给林悦100元")["pending_action"]
    result = send(client, "我不想收到转账提醒")
    assert "未发生扣款" not in result["message"]
    with db.db_session() as conn:
        status = conn.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"]
    assert status == "pending"

    cancelled = send(client, "取消转账提醒")
    assert "未发生扣款" not in cancelled["message"]
    with db.db_session() as conn:
        status = conn.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"]
    assert status == "pending"


def test_negation_clears_partial_transfer_slots_without_cancelling_approved_schedule(client):
    partial = send(client, "转给林悦")
    assert "pending_action" not in partial
    declined = send(client, "暂时不转账了")
    assert "未确认的转账草稿" in declined["message"]
    assert "pending_action" not in send(client, "100元")

    scheduled = send(client, "明天晚上8点给林悦转100元")["pending_action"]
    completed = client.post(f"/api/actions/{scheduled['id']}/confirm", json={"session_id": "test-session"})
    assert completed.status_code == 200
    send(client, "不要再转给林悦了")
    with db.db_session() as conn:
        schedule = conn.execute("SELECT status FROM scheduled_transfers WHERE id=?", (scheduled["id"],)).fetchone()
        assert schedule["status"] == "pending"


def test_red_transfer_cannot_bypass_strong_verification(client):
    prepared = send(client, "转给林悦1200元")
    action = prepared["pending_action"]
    assert action["tier"] == "red"
    response = client.post(
        f"/api/actions/{action['id']}/confirm", json={"session_id": "test-session"}
    )
    assert response.status_code == 403
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"


def test_daily_limit_rechecked_after_prior_transfer(client):
    first = send(client, "转给林悦900元")["pending_action"]
    confirmed = client.post(
        f"/api/actions/{first['id']}/confirm", json={"session_id": "test-session"}
    )
    assert confirmed.status_code == 200
    second = send(client, "转给林悦200元")["pending_action"]
    assert second["tier"] == "red"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "7988.30"


def test_confirmation_is_bound_to_session(client):
    action = send(client, "转给林悦100元")["pending_action"]
    response = client.post(
        f"/api/actions/{action['id']}/confirm", json={"session_id": "another-session"}
    )
    assert response.status_code == 404
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"


def test_ambiguous_recipient_requires_clarification(client):
    prepared = send(client, "转给王明200元")
    assert "pending_action" not in prepared
    assert len(prepared["choices"]) == 2
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"


def test_recipient_choice_resolves_only_current_ambiguous_transfer(client):
    prepared = send(client, "转给王明200元，备注聚餐")
    choice = prepared["choices"][0]
    assert choice["phone"].startswith("139****")
    assert choice["phone"] != "13900001111"
    invalid = client.post("/api/transfers/resolve", json={
        "session_id": "another-session", "contact_id": choice["id"],
    })
    assert invalid.status_code == 409
    resolved = client.post("/api/transfers/resolve", json={
        "session_id": "test-session", "contact_id": choice["id"],
    })
    assert resolved.status_code == 200
    action = resolved.json()["pending_action"]
    assert action["details"]["amount_yuan"] == "200.00"
    assert action["details"]["note"] == "聚餐"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"


def test_bill_summary_is_backed_by_transaction_ids(client):
    report = send(client, "本月账单")["report"]
    assert report["period"] == "2026-09"
    assert report["total_yuan"] == "1860.30"
    assert len(report["transaction_ids"]) == 7
    assert report["alerts"][0]["counterparty"] == "安居物业"
    assert next(row for row in report["transactions"] if row["id"] == "tx-1")["note"] == "早餐"


def test_natural_spending_comparison_routes_to_explained_bill_evidence(client):
    original_balance = client.get("/api/overview").json()["account"]["balance_yuan"]
    question = "本月餐饮比上月多花多少"
    result = send(client, question)
    assert "pending_action" not in result
    assert result["insight_query"] == question
    report = result["insight_report"]
    assert report["period"] == "2026-09"
    assert report["filters"]["category"] == "餐饮"
    assert report["comparison"]["current_total_yuan"] == "103.90"
    assert report["comparison"]["previous_total_yuan"] == "137.00"
    assert report["comparison"]["delta_yuan"] == "-33.10"
    assert {row["id"] for row in report["transactions"]} == {"tx-1", "tx-6"}
    assert "减少 ¥33.10" in result["message"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == original_balance
    with db.db_session() as conn:
        audit = conn.execute(
            "SELECT details_json FROM audit WHERE session_id=? AND event='insights_queried'",
            ("test-session",),
        ).fetchone()
        assert '"category": "餐饮"' in audit["details_json"]


def test_read_only_bill_comparison_preserves_pending_transfer_context(client):
    partial = send(client, "转给林悦", session_id="mixed-read-session")
    assert "pending_action" not in partial
    insight = send(client, "本月餐饮比上月多花多少", session_id="mixed-read-session")
    assert "insight_report" in insight
    completed = send(client, "300元", session_id="mixed-read-session")
    assert completed["pending_action"]["details"]["recipient"] == "林悦"
    assert completed["pending_action"]["details"]["amount_yuan"] == "300.00"


def test_bill_exports_include_summary_and_transaction_details(client):
    csv_response = client.get("/api/bills/export", params={"period": "本月", "format": "csv"})
    assert csv_response.status_code == 200
    assert "attachment" in csv_response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(csv_response.text.lstrip("\ufeff"))))
    assert ["总支出（元）", "1860.30"] in rows
    assert ["2026-09-28", "tx-1", "城市咖啡", "餐饮", "早餐", "45.90", "餐饮", "", ""] in rows

    json_response = client.get("/api/bills/export", params={"period": "本月", "format": "json"})
    assert json_response.status_code == 200
    assert json_response.json()["transactions"][0]["note"] == "早餐"


def test_annual_bill_and_transfer_follow_up(client):
    annual = send(client, "看看今年的年度账单")["report"]
    assert annual["period"] == "2026"
    assert len(annual["transaction_ids"]) == 11

    missing = send(client, "转给林悦")
    assert "pending_action" not in missing
    completed = send(client, "300元")
    assert completed["pending_action"]["details"]["amount_yuan"] == "300.00"


def test_transfer_amount_before_recipient(client):
    result = send(client, "转300元给林悦")
    assert result["pending_action"]["details"]["recipient"] == "林悦"


def test_subscription_cancel_requires_confirmation(client):
    prepared = send(client, "取消云影会员自动续费")
    action = prepared["pending_action"]
    assert action["details"]["merchant"] == "云影会员"
    assert client.get("/api/overview").json()["subscriptions"][0]["status"] == "active"
    response = client.post(
        f"/api/actions/{action['id']}/confirm", json={"session_id": "test-session"}
    )
    assert response.status_code == 200
    subscriptions = client.get("/api/overview").json()["subscriptions"]
    assert next(row for row in subscriptions if row["id"] == "sub-cloud")["status"] == "cancelled"


def test_subscription_cancel_refusal_prevents_and_withdraws_pending_action(client):
    refused = send(client, "先别取消云影会员，只查扣款记录")
    assert "pending_action" not in refused
    assert "不会取消" in refused["message"]
    action = send(client, "取消云影会员自动续费")["pending_action"]
    refused_again = send(client, "先别取消云影会员")
    assert "pending_action" not in refused_again
    rejected = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "test-session"})
    assert rejected.status_code == 409
    subscriptions = client.get("/api/overview").json()["subscriptions"]
    assert next(row for row in subscriptions if row["id"] == "sub-cloud")["status"] == "active"
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='subscription_cancel' AND status='pending'").fetchone()[0] == 0


def test_future_subscription_cancel_is_not_executed_immediately(client):
    action = send(client, "取消云影会员自动续费")["pending_action"]
    delayed = send(client, "云影会员下次扣款后再取消")
    assert "pending_action" not in delayed
    assert "不支持延后自动取消" in delayed["message"]
    rejected = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "test-session"})
    assert rejected.status_code == 409
    subscriptions = client.get("/api/overview").json()["subscriptions"]
    assert next(row for row in subscriptions if row["id"] == "sub-cloud")["status"] == "active"


def test_positive_subscription_cancellation_after_renewal_negation_is_preserved(client):
    result = send(client, "我不想继续订阅云影会员了，请现在取消它")
    assert result["pending_action"]["type"] == "subscription_cancel"


def test_declining_one_subscription_preserves_other_pending_cancellation(client):
    action = send(client, "取消青柠音乐", session_id="music-session")["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id='sub-cloud'")
    result = send(client, "先别取消云影会员", session_id="music-session")
    assert "其他协议的待确认操作不受影响" in result["message"]
    confirmed = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "music-session"})
    assert confirmed.status_code == 200


def test_recurring_subscription_detection_has_transaction_evidence(client):
    signals = client.get("/api/overview").json()["subscription_signals"]
    cloud = next(row for row in signals if row["merchant"] == "云影会员")
    assert set(cloud["evidence_ids"]) == {"tx-2", "tx-9"}
    assert cloud["days_until_renewal"] == 25
    assert cloud["reminder"] is True
    listed = send(client, "查看我正在扣费的订阅")
    assert "近两个月账单均有扣费记录" in listed["message"]


def test_direct_page_endpoints_share_confirmation_boundary(client):
    contacts = client.get("/api/contacts").json()
    lin = next(row for row in contacts if row["name"] == "林悦")
    assert lin["phone_masked"] == "138****1234"
    assert "phone" not in lin

    report = client.get("/api/bills", params={"period": "本月"}).json()
    assert report["total_yuan"] == "1860.30"
    assert len(report["transactions"]) == 7

    prepared = client.post("/api/transfers/prepare", json={
        "session_id": "page-session", "contact_id": lin["id"],
        "amount_yuan": "50.00", "note": "测试",
    })
    assert prepared.status_code == 200
    action = prepared.json()["pending_action"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"
    result = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "page-session"})
    assert result.status_code == 200
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8838.30"


def test_direct_cancel_and_invalid_contact(client):
    unknown = client.post("/api/transfers/prepare", json={
        "session_id": "page-session", "contact_id": "unknown", "amount_yuan": "20", "note": "",
    })
    assert unknown.status_code == 404
    prepared = client.post(
        "/api/subscriptions/sub-cloud/prepare-cancel", json={"session_id": "page-session"}
    )
    assert prepared.status_code == 200
    action = prepared.json()["pending_action"]
    assert action["type"] == "subscription_cancel"
    assert client.get("/api/overview").json()["subscriptions"][0]["status"] == "active"
