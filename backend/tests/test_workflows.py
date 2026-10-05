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
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
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


@pytest.mark.parametrize(("message", "expected_note"), [
    ("帮我给林悦付款300元，备注房租", "房租"),
    ("向林悦支付300元，用途午餐", "午餐"),
    ("把300元付给林悦，备注车费", "车费"),
    ("给林悦打款300元", "转账"),
])
def test_common_directed_payment_phrasings_prepare_transfer(client, message, expected_note):
    result = send(client, message, session_id=f"payment-{expected_note}")
    assert result["pending_action"]["type"] == "transfer"
    details = result["pending_action"]["details"]
    assert details["recipient"] == "林悦"
    assert details["amount_yuan"] == "300.00"
    assert details["note"] == expected_note
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"


def test_directed_payment_by_phone_and_ambiguous_name_keep_contact_checks(client):
    by_phone = send(client, "给13800001234付款300元，备注房租", session_id="payment-phone")
    assert by_phone["pending_action"]["details"]["recipient"] == "林悦"
    assert by_phone["pending_action"]["details"]["phone_masked"].endswith("1234")

    ambiguous = send(client, "给王明付款200元", session_id="payment-ambiguous")
    assert "pending_action" not in ambiguous
    assert len(ambiguous["choices"]) == 2


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


def test_past_directed_payment_question_is_read_only(client):
    with db.db_session() as conn:
        conn.execute(
            "INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note) "
            "VALUES (?, ?, ?, 'out', ?, ?, '转账', ?)",
            ("tx-paid-lin", db.ACCOUNT_ID, "2026-09-20", 30000, "林悦", "房租"),
        )
    result = send(client, "本月给林悦付了多少钱")
    assert "pending_action" not in result
    assert result["transaction_query"]["transactions"][0]["id"] == "tx-paid-lin"
    assert result["transaction_query"]["transactions"][0]["amount_yuan"] == "300.00"
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='transfer'").fetchone()[0] == 0


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
    for question in ("查一下最近转给林悦300元的记录", "查一下近三个月转给林悦300元的记录",
                     "查一下最近91天转给林悦300元的记录", "查一下未来5天转账记录",
                     "查一下下周转账记录", "查一下上上周转账记录", "查一下上周末转账记录",
                     "查一下上周一转账记录", "查一下上上个月转账记录", "查一下上上月转账记录",
                     "查一下前两周转账记录",
                     "查一下前两个月转账记录"):
        result = send(client, question)
        assert "pending_action" not in result
        assert result["transaction_query"]["needs_clarification"] is True
        assert "暂时不能可靠地解析" in result["message"]
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE event='transfer_history_queried'").fetchone()[0] == 0


def test_transfer_history_query_supports_previous_calendar_week(client):
    with db.db_session() as conn:
        conn.executemany(
            "INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note) "
            "VALUES (?, ?, ?, 'out', 30000, '林悦', '转账', '')",
            [(f"tx-week-{day}", db.ACCOUNT_ID, day) for day in
             ("2026-09-20", "2026-09-21", "2026-09-27", "2026-09-28")],
        )

    result = send(client, "查一下上周转给林悦300元的记录")

    assert [row["id"] for row in result["transaction_query"]["transactions"]] == [
        "tx-week-2026-09-27", "tx-week-2026-09-21",
    ]
    assert result["transaction_query"]["period"] == {
        "label": "上周", "start": "2026-09-21", "end": "2026-09-27",
    }


@pytest.mark.parametrize(("phrase", "start"), [("最近7天", "2026-09-24"), ("过去三天", "2026-09-28")])
def test_transfer_history_query_supports_bounded_recent_days(client, phrase, start):
    with db.db_session() as conn:
        conn.executemany(
            "INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note) "
            "VALUES (?, ?, ?, 'out', 30000, '林悦', '转账', '')",
            [(f"tx-recent-{day}", db.ACCOUNT_ID, day) for day in
             ("2026-09-23", "2026-09-24", "2026-09-28", "2026-09-30")],
        )

    result = send(client, f"查一下{phrase}转给林悦300元的记录")

    assert result["transaction_query"]["period"]["start"] == start
    assert result["transaction_query"]["period"]["end"] == "2026-09-30"
    assert all(row["posted_on"] >= start for row in result["transaction_query"]["transactions"])


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


def test_how_to_cancel_question_does_not_revoke_a_pending_transfer(client):
    action = send(client, "转给林悦100元", session_id="cancel-help") ["pending_action"]

    question = send(client, "怎么取消转账？", session_id="cancel-help")

    assert "pending_action" not in question
    with db.db_session() as conn:
        status = conn.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"]
    assert status == "pending"


def test_how_to_transfer_question_does_not_create_a_transfer(client):
    question = send(client, "如何给林悦转账300元？", session_id="transfer-help")

    assert "pending_action" not in question
    with db.db_session() as conn:
        count = conn.execute("SELECT COUNT(*) FROM actions WHERE session_id='transfer-help' AND type='transfer'").fetchone()[0]
    assert count == 0
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"


def test_negation_withdraws_pending_transfer_action_and_reminders_never_execute(client):
    action = send(client, "转给林悦100元")["pending_action"]
    result = send(client, "现在不要再转给林悦了")
    assert "pending_action" not in result
    rejected = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "test-session"})
    assert rejected.status_code == 409
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"
    reminder = send(client, "不要忘记转给林悦100元", session_id="positive-reminder")
    assert "pending_action" not in reminder
    assert "转账提醒" in reminder["message"]
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE session_id='positive-reminder' AND type='transfer'").fetchone()[0] == 0


@pytest.mark.parametrize(
    "reminder_text",
    ("提醒我明天给王明转100元", "提醒我\n明天给王明转100元"),
)
def test_transfer_reminder_does_not_consume_or_replace_an_existing_draft(client, reminder_text):
    partial = send(client, "转给林悦", session_id="transfer-reminder-draft")
    assert "pending_action" not in partial

    reminder = send(client, reminder_text, session_id="transfer-reminder-draft")

    assert "pending_action" not in reminder
    assert "转账提醒" in reminder["message"]
    completed_draft = send(client, "300元", session_id="transfer-reminder-draft")
    assert completed_draft["pending_action"]["details"]["recipient"] == "林悦"
    assert completed_draft["pending_action"]["details"]["amount_yuan"] == "300.00"


def test_transfer_reminder_does_not_cancel_an_existing_action(client):
    action = send(client, "转给林悦100元", session_id="transfer-reminder-action")["pending_action"]

    reminder = send(client, "提醒我别忘给王明转账", session_id="transfer-reminder-action")

    assert "pending_action" not in reminder
    assert "转账提醒" in reminder["message"]
    with db.db_session() as conn:
        status = conn.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"]
    assert status == "pending"


def test_reminder_after_a_transfer_request_is_rejected_without_creating_an_action(client):
    result = send(client, "转给林悦300元，之后提醒我核对回执", session_id="transfer-then-reminder")

    assert "pending_action" not in result
    assert "转账提醒" in result["message"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"


def test_transfer_reminder_preferences_do_not_withdraw_transfer_actions(client):
    action = send(client, "转给林悦100元")["pending_action"]
    result = send(client, "我不想收到转账提醒")
    assert "未发生扣款" not in result["message"]
    assert result["workflow"]["section"] == "reminders"
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


def test_payment_wording_negation_clears_unconfirmed_transfer_draft(client):
    partial = send(client, "给林悦付款")
    assert "pending_action" not in partial
    for reminder_request in ("我不想收到转账提醒", "不要给林悦付款提醒"):
        reminder = send(client, reminder_request)
        assert "未确认的转账草稿" not in reminder["message"]
    declined = send(client, "不要给林悦付款了")
    assert "未确认的转账草稿" in declined["message"]
    assert "pending_action" not in send(client, "300元")


def test_natural_transfer_reminder_saves_an_in_app_note_without_creating_money_action(client):
    before_balance = client.get("/api/overview").json()["account"]["balance_yuan"]
    result = send(client, "提醒我明天给林悦转300元", session_id="transfer-reminder-owner")

    assert "不会执行转账" in result["message"]
    assert result["workflow"]["view"] == "tasks"
    assert result["workflow"]["section"] == "reminders"
    reminders = client.get("/api/transfer-reminders", params={"session_id": "transfer-reminder-owner"}).json()["items"]
    assert len(reminders) == 1
    assert reminders[0]["due_on"] == "2026-10-01"
    assert reminders[0]["status"] == "pending"
    assert "给林悦转300元" in reminders[0]["body"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == before_balance
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE session_id='transfer-reminder-owner' AND type LIKE '%transfer%'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transfer_reminders").fetchone()[0] == 1


def test_transfer_reminder_requires_one_future_calendar_date_and_preserves_existing_draft(client):
    draft = send(client, "转给林悦", session_id="reminder-date-clarification")
    assert "pending_action" not in draft

    missing = send(client, "提醒我给王明转100元", session_id="reminder-date-clarification")
    assert "明确的未来日期" in missing["message"]
    assert "pending_action" not in missing
    assert send(client, "300元", session_id="reminder-date-clarification")["pending_action"]["details"]["recipient"] == "林悦"

    for text in ("每月5日提醒我给林悦转100元", "提醒我10月6日或10月7日给林悦转账", "提醒我昨天给林悦转100元"):
        refused = send(client, text, session_id=f"invalid-reminder-{text[:5]}")
        assert "pending_action" not in refused
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transfer_reminders").fetchone()[0] == 0


def test_transfer_reminder_becomes_due_on_demo_clock_without_executing_transfer(client):
    before_balance = client.get("/api/overview").json()["account"]["balance_yuan"]
    created = send(client, "提醒我10月1日给林悦转100元", session_id="reminder-due-owner")
    reminder_id = client.get("/api/transfer-reminders", params={"session_id": "reminder-due-owner"}).json()["items"][0]["id"]

    advanced = client.post("/api/demo/clock/advance-next", json={"session_id": "reminder-due-owner"})
    assert advanced.status_code == 200
    items = client.get("/api/transfer-reminders", params={"session_id": "reminder-due-owner"}).json()["items"]
    assert items[0]["id"] == reminder_id
    assert items[0]["status"] == "due"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == before_balance
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE action_id IN (SELECT id FROM actions WHERE type LIKE '%transfer%')").fetchone()[0] == 0


def test_transfer_reminders_are_session_scoped_and_can_be_completed_or_cancelled(client):
    send(client, "提醒我10月1日给林悦转100元", session_id="reminder-session-a")
    send(client, "提醒我10月2日给王明转200元", session_id="reminder-session-b")
    a = client.get("/api/transfer-reminders", params={"session_id": "reminder-session-a"}).json()["items"]
    b = client.get("/api/transfer-reminders", params={"session_id": "reminder-session-b"}).json()["items"]
    assert len(a) == len(b) == 1
    assert a[0]["id"] != b[0]["id"]
    assert client.post(f"/api/transfer-reminders/{a[0]['id']}/complete", json={"session_id": "reminder-session-b"}).status_code == 404
    assert client.post(f"/api/transfer-reminders/{a[0]['id']}/complete", json={"session_id": "reminder-session-a"}).status_code == 200
    assert client.post(f"/api/transfer-reminders/{b[0]['id']}/cancel", json={"session_id": "reminder-session-b"}).status_code == 200
    final_a = client.get("/api/transfer-reminders", params={"session_id": "reminder-session-a"}).json()["items"]
    final_b = client.get("/api/transfer-reminders", params={"session_id": "reminder-session-b"}).json()["items"]
    assert final_a[0]["status"] == "completed"
    assert final_b[0]["status"] == "cancelled"


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


def test_plain_bill_report_preserves_pending_transfer_context(client):
    session_id = "plain-report-keeps-transfer-session"
    partial = send(client, "转给林悦", session_id=session_id)
    assert "pending_action" not in partial

    report = send(client, "本月账单", session_id=session_id)
    assert "report" in report

    completed = send(client, "300元", session_id=session_id)
    assert completed["pending_action"]["type"] == "transfer"
    assert completed["pending_action"]["details"]["recipient"] == "林悦"


def test_natural_language_category_budget_creates_confirmable_plan_and_updates_existing_budget(client):
    session_id = "budget-chat-session"
    original_balance = client.get("/api/overview").json()["account"]["balance_yuan"]

    prepared = send(client, "本月餐饮预算控制在1500元", session_id=session_id)
    action = prepared["pending_action"]
    assert action["type"] == "bill_budget_upsert"
    assert action["tier"] == "yellow"
    assert action["details"]["month"] == "2026-09"
    assert action["details"]["category"] == "餐饮"
    assert action["details"]["amount_yuan"] == "1500.00"
    assert client.get("/api/bill-preferences", params={"session_id": session_id}).json()["budgets"] == []

    first = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session_id})
    assert first.status_code == 200
    budget = client.get("/api/bill-preferences", params={"session_id": session_id}).json()["budgets"][0]
    assert budget["category"] == "餐饮"
    assert budget["amount_yuan"] == "1500.00"
    assert budget["spent_yuan"] == "103.90"

    revised = send(client, "本月餐饮预算调整为1600元", session_id=session_id)["pending_action"]
    assert revised["type"] == "bill_budget_upsert"
    assert revised["details"]["budget_id"] == budget["id"]
    assert revised["details"]["amount_yuan"] == "1600.00"
    second = client.post(f"/api/actions/{revised['id']}/confirm", json={"session_id": session_id})
    assert second.status_code == 200
    updated = client.get("/api/bill-preferences", params={"session_id": session_id}).json()["budgets"][0]
    assert updated["version"] == 2
    assert updated["amount_yuan"] == "1600.00"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == original_balance


def test_budget_how_to_question_does_not_prepare_a_budget_change(client):
    question = send(client, "怎么设置本月餐饮预算1500元？", session_id="budget-how-to")

    assert "pending_action" not in question
    with db.db_session() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM actions WHERE session_id='budget-how-to' AND type='bill_budget_upsert'"
        ).fetchone()[0]
    assert count == 0
    assert client.get("/api/bill-preferences", params={"session_id": "budget-how-to"}).json()["budgets"] == []


def test_budget_how_to_question_does_not_replace_a_pending_budget_change(client):
    session_id = "budget-how-to-pending"
    action = send(client, "本月餐饮预算控制在1500元", session_id=session_id)["pending_action"]

    question = send(client, "如何调整预算？", session_id=session_id)

    assert "pending_action" not in question
    assert "预算" in question["message"]
    with db.db_session() as conn:
        status = conn.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"]
    assert status == "pending"


def test_natural_language_total_budget_is_supported_but_ambiguous_category_is_not_guessed(client):
    total = send(client, "本月总预算控制在3000元")
    assert total["pending_action"]["type"] == "bill_budget_upsert"
    assert total["pending_action"]["details"]["category"] is None
    assert total["pending_action"]["details"]["amount_yuan"] == "3000.00"

    ambiguous = send(client, "本月餐饮和交通预算分别设为1000元")
    assert "pending_action" not in ambiguous

    historical = send(client, "上月餐饮预算设为1200元")
    assert "pending_action" not in historical


def test_natural_language_budget_does_not_erase_incomplete_transfer(client):
    session_id = "budget-preserves-transfer-session"
    partial = send(client, "转给林悦", session_id=session_id)
    assert "pending_action" not in partial

    budget = send(client, "本月餐饮预算控制在1500元", session_id=session_id)
    assert budget["pending_action"]["type"] == "bill_budget_upsert"

    completed = send(client, "300元", session_id=session_id)
    assert completed["pending_action"]["type"] == "transfer"
    assert completed["pending_action"]["details"]["recipient"] == "林悦"


def test_chat_reports_confirmed_budget_remaining_with_ledger_evidence(client):
    session_id = "budget-query-session"
    prepared = send(client, "本月餐饮预算控制在1500元", session_id=session_id)
    action_id = prepared["pending_action"]["id"]
    assert client.post(f"/api/actions/{action_id}/confirm", json={"session_id": session_id}).status_code == 200

    result = send(client, "本月餐饮预算还剩多少？", session_id=session_id)

    assert result["mode"] == "offline"
    assert "¥1396.10" in result["message"]
    assert result["budget_status"]["month"] == "2026-09"
    assert result["budget_status"]["budgets"] == [{
        "category": "餐饮", "amount_yuan": "1500.00", "spent_yuan": "103.90",
        "remaining_yuan": "1396.10", "over_yuan": "0.00", "transaction_ids": ["tx-1", "tx-6"],
    }]


def test_chat_budget_query_without_saved_budget_is_read_only_and_keeps_transfer_draft(client):
    session_id = "empty-budget-query-session"
    send(client, "转给林悦", session_id=session_id)

    result = send(client, "本月预算还剩多少？", session_id=session_id)

    assert "尚未设置本月预算" in result["message"]
    assert result["budget_status"]["budgets"] == []
    completed = send(client, "300元", session_id=session_id)
    assert completed["pending_action"]["type"] == "transfer"
    assert completed["pending_action"]["details"]["recipient"] == "林悦"


def test_category_budget_query_does_not_substitute_the_total_budget(client):
    session_id = "category-budget-query-session"
    prepared = send(client, "本月总预算控制在3000元", session_id=session_id)
    action_id = prepared["pending_action"]["id"]
    assert client.post(f"/api/actions/{action_id}/confirm", json={"session_id": session_id}).status_code == 200

    result = send(client, "本月餐饮预算还剩多少？", session_id=session_id)

    assert "餐饮”尚未设置本月预算" in result["message"]
    assert result["budget_status"]["budgets"] == []


def test_chat_cash_forecast_lists_known_debits_and_authorized_transfers(client):
    result = send(client, "未来30天预计有哪些自动扣款和已授权转账？")

    assert result["mode"] == "offline"
    assert "未来30天" in result["message"]
    assert "云影会员" in result["message"]
    assert "已知代扣估算" in result["message"]
    assert result["cash_forecast"]["from"].startswith("2026-09-30")
    assert result["cash_forecast"]["to"].startswith("2026-10-30")
    assert result["cash_forecast"]["known_debits"]
    assert result["cash_forecast"]["limitations"]


def test_chat_spending_goal_routes_to_evidence_backed_plan_without_cancelling_subscriptions(client):
    session_id = "chat-spending-plan-session"

    result = send(client, "帮我看看上个月为什么花得多，再看看下个月能不能少花300元", session_id=session_id)

    workflow = result["workflow"]
    assert workflow["view"] == "tasks"
    assert workflow["section"] == "plans"
    assert workflow["message"] == "帮我看看上个月为什么花得多，再看看下个月能不能少花300元"
    plan = client.get(f"/api/plans/{workflow['plan_id']}", params={"session_id": session_id}).json()
    assert plan["target_yuan"] == "300.00"
    assert plan["insight_report"]["period"] == "2026-08"
    assert {step["status"] for step in plan["steps"][:2]} == {"completed"}
    assert plan["steps"][2]["status"] == "awaiting_input"
    assert plan["steps"][3]["status"] == "blocked"
    assert "pending_action" not in result
    with db.db_session() as conn:
        assert [row["status"] for row in conn.execute("SELECT status FROM subscriptions ORDER BY id")] == ["active", "active"]
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE session_id=?", (session_id,)).fetchone()[0] == 0


def test_chat_category_correction_requires_confirmation_and_preserves_original_transaction(client):
    original_balance = client.get("/api/overview").json()["account"]["balance_yuan"]
    prepared = send(client, "把交易 tx-8 归类为差旅，因为是出差打车")
    action = prepared["pending_action"]
    assert action["type"] == "bill_classification"
    assert action["tier"] == "yellow"
    assert action["details"]["transaction_id"] == "tx-8"
    assert action["details"]["original_category"] == "交通"
    assert action["details"]["category"] == "差旅"
    assert action["details"]["transaction"]["amount_yuan"] == "235.00"
    with db.db_session() as conn:
        assert conn.execute("SELECT category FROM transactions WHERE id='tx-8'").fetchone()[0] == "交通"
        assert conn.execute("SELECT 1 FROM bill_category_overrides WHERE transaction_id='tx-8'").fetchone() is None

    result = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "test-session"})
    assert result.status_code == 200
    assert result.json()["category"] == "差旅"
    report = client.get("/api/bills?period=本月").json()
    row = next(row for row in report["transactions"] if row["id"] == "tx-8")
    assert row["category"] == "差旅"
    assert row["original_category"] == "交通"
    assert row["amount_yuan"] == "235.00"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == original_balance


def test_classification_how_to_question_does_not_prepare_an_override(client):
    question = send(client, "怎么把交易 tx-8 归类为差旅？", session_id="classification-help")

    assert "pending_action" not in question
    with db.db_session() as conn:
        assert conn.execute("SELECT 1 FROM bill_category_overrides WHERE transaction_id='tx-8'").fetchone() is None
        assert conn.execute("SELECT category FROM transactions WHERE id='tx-8'").fetchone()["category"] == "交通"
    assert "归类" in question["message"]


def test_classification_how_to_question_does_not_replace_a_pending_override(client):
    session_id = "classification-help-pending"
    action = send(client, "把交易 tx-8 归类为差旅，因为是出差打车", session_id=session_id)["pending_action"]

    question = send(client, "如何把交易 tx-9 归类为餐饮？", session_id=session_id)

    assert "pending_action" not in question
    with db.db_session() as conn:
        status = conn.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"]
    assert status == "pending"


def test_chat_category_correction_uses_single_transaction_from_recent_bill_question(client):
    report = send(client, "本月交通账单")
    assert report["insight_report"]["transactions"]
    assert [row["id"] for row in report["insight_report"]["transactions"]] == ["tx-8"]

    correction = send(client, "这笔是差旅，不是交通")

    assert correction["pending_action"]["type"] == "bill_classification"
    assert correction["pending_action"]["details"]["transaction_id"] == "tx-8"
    assert correction["pending_action"]["details"]["category"] == "差旅"


def test_chat_category_correction_accepts_generated_transaction_ids(client):
    transfer = send(client, "转给林悦80元")
    result = client.post(
        f"/api/actions/{transfer['pending_action']['id']}/confirm", json={"session_id": "test-session"},
    )
    assert result.status_code == 200
    transaction_id = result.json()["transaction_id"]
    assert transaction_id.startswith("transfer-")

    correction = send(client, f"把交易 {transaction_id} 归类为差旅，因为是因公往来")

    assert correction["pending_action"]["type"] == "bill_classification"
    assert correction["pending_action"]["details"]["transaction_id"] == transaction_id
    assert correction["pending_action"]["details"]["previous_category"] == "转账"


def test_chat_category_correction_asks_for_transaction_when_merchant_is_ambiguous(client):
    before = client.get("/api/overview").json()["account"]["balance_yuan"]
    result = send(client, "把城市咖啡的消费改为差旅，因为是出差")

    assert "pending_action" not in result
    assert len(result["classification_choices"]) >= 2
    assert {item["counterparty"] for item in result["classification_choices"]} == {"城市咖啡"}
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == before
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM bill_category_overrides").fetchone()[0] == 0


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


def test_subscription_how_to_question_does_not_prepare_cancellation(client):
    question = send(client, "怎么取消云影会员自动续费？", session_id="subscription-help")

    assert "pending_action" not in question
    assert "转账流程咨询" not in question["message"]
    assert "代扣" in question["message"] or "订阅" in question["message"]
    with db.db_session() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM actions WHERE session_id='subscription-help' AND type='subscription_cancel'"
        ).fetchone()[0]
    assert count == 0
    subscriptions = client.get("/api/overview").json()["subscriptions"]
    assert next(row for row in subscriptions if row["id"] == "sub-cloud")["status"] == "active"


def test_subscription_how_to_question_preserves_existing_cancellation(client):
    action = send(client, "取消云影会员自动续费", session_id="subscription-help-pending")["pending_action"]

    question = send(client, "如何取消订阅？", session_id="subscription-help-pending")

    assert "pending_action" not in question
    assert "转账流程咨询" not in question["message"]
    assert "代扣" in question["message"] or "订阅" in question["message"]
    with db.db_session() as conn:
        status = conn.execute("SELECT status FROM actions WHERE id=?", (action["id"],)).fetchone()["status"]
    assert status == "pending"


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
