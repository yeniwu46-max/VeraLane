"""Authorization and ledger invariants for durable simulated transfers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app import db, main, schedules, service


SESSION = "schedule-owner"
TARGET = "2026-10-06T20:00:00+08:00"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "schedules.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    # Keep scanner invocation explicit so boundary/race tests cannot depend on timing.
    monkeypatch.setattr(main, "run_due_transfers", lambda: 0)
    with TestClient(main.app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def send(client, message, session=SESSION):
    response = client.post("/api/chat", json={"session_id": session, "message": message})
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, message="10月6日晚上8点转给林悦300元，备注房租", session=SESSION):
    return send(client, message, session)["pending_action"]


def confirm(client, action, session=SESSION):
    return client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session})


def authorize(client, message="10月6日晚上8点转给林悦300元，备注房租", session=SESSION):
    action = prepare(client, message, session)
    response = confirm(client, action, session)
    assert response.status_code == 200, response.text
    return action


def task(client, action, session=SESSION):
    response = client.get("/api/schedules", params={"session_id": session})
    assert response.status_code == 200
    return next(row for row in response.json()["tasks"] if row["id"] == action["id"])


def set_clock(value):
    with db.db_session() as conn:
        conn.execute("UPDATE demo_clock SET now = ? WHERE id = 1", (value,))


def ledger(action):
    with db.db_session() as conn:
        balance = conn.execute("SELECT balance_cents FROM accounts WHERE id = ?", (db.ACCOUNT_ID,)).fetchone()[0]
        rows = conn.execute("SELECT * FROM transactions WHERE action_id = ?", (action["id"],)).fetchall()
        return balance, [dict(row) for row in rows]


def test_idle_scheduler_scan_does_not_open_writer_connection(client, monkeypatch):
    def unexpected_writer_connection():
        pytest.fail("idle scheduler scan should not open a writer connection")

    monkeypatch.setattr(schedules, "connect", unexpected_writer_connection)
    assert schedules.run_due_transfers() == 0


@pytest.mark.parametrize(("expression", "target"), [
    ("10月6日晚上8点", TARGET),
    ("2026-10-06 20:00", TARGET),
    ("明天上午九点半", "2026-10-01T09:30:00+08:00"),
    ("后天14:05", "2026-10-02T14:05:00+08:00"),
])
def test_natural_language_dates_produce_unexecuted_plans(client, expression, target):
    action = prepare(client, f"{expression}转给林悦300元，备注房租")
    assert action["type"] == "scheduled_transfer"
    assert action["details"]["execute_at"] == target
    assert action["details"]["amount_yuan"] == "300.00"
    assert action["details"]["note"] == "房租"
    assert client.get("/api/schedules", params={"session_id": SESSION}).json()["tasks"] == []
    assert ledger(action) == (888830, [])


def test_invalid_amount_correction_cannot_restore_previous_valid_amount(client):
    assert "pending_action" not in send(client, "明天转给林悦300元")
    result = send(client, "晚上8点，金额改为1,000元")
    assert "pending_action" not in result
    assert "金额" in result["message"]
    resolved = send(client, "200元")["pending_action"]
    assert resolved["details"]["amount_yuan"] == "200.00"


def test_memo_cannot_supply_missing_amount_during_follow_up(client):
    assert "pending_action" not in send(client, "明天转给林悦")
    result = send(client, "晚上8点，备注王明300元")
    assert "pending_action" not in result
    assert "金额" in result["message"]
    resolved = send(client, "200元")["pending_action"]
    assert resolved["details"]["recipient"] == "林悦"
    assert resolved["details"]["amount_yuan"] == "200.00"


def test_missing_fields_and_ambiguous_contact_resume_same_schedule(client):
    first = send(client, "明天转给王明，备注聚餐")
    assert "pending_action" not in first
    choices = send(client, "300元")["choices"]
    chosen = next(row for row in choices if row["phone"].endswith("2222"))
    resolved = client.post("/api/transfers/resolve", json={
        "session_id": SESSION, "contact_id": chosen["id"],
    })
    assert resolved.status_code == 200
    assert "pending_action" not in resolved.json()
    action = send(client, "晚上8点")["pending_action"]
    assert action["type"] == "scheduled_transfer"
    assert action["details"]["execute_at"] == "2026-10-01T20:00:00+08:00"
    assert action["details"]["phone_masked"].endswith("2222")
    assert action["details"]["amount_yuan"] == "300.00"
    assert action["details"]["note"] == "聚餐"
    assert confirm(client, action).status_code == 200
    set_clock("2026-10-01T20:00:00+08:00")
    schedules.run_due_transfers()
    assert task(client, action)["status"] == "completed"
    assert ledger(action)[0] == 858830


@pytest.mark.parametrize("when", [
    "10月6日8点", "10月6日25:00", "2月30日20:00", "9月29日20:00", "每月6日20:00", "下周20:00",
])
def test_unclear_invalid_past_or_repeating_dates_never_become_immediate_transfers(client, when):
    response = send(client, f"{when}转给林悦300元")
    assert "pending_action" not in response
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == "8888.30"
    assert client.get("/api/schedules", params={"session_id": SESSION}).json()["tasks"] == []


def test_memo_dates_are_data_not_execution_instructions(client):
    action = prepare(client, "转给林悦300元，备注10月6日晚上8点房租")
    assert action["type"] == "transfer"
    assert "execute_at" not in action["details"]
    assert action["details"]["note"] == "10月6日晚上8点房租"


def test_confirmation_persists_without_debit_and_concurrent_scans_debit_once(client):
    action = authorize(client)
    repeated = confirm(client, action)
    assert repeated.status_code == 200
    assert repeated.json()["schedule_id"] == action["id"]
    assert len(client.get("/api/schedules", params={"session_id": SESSION}).json()["tasks"]) == 1
    assert ledger(action) == (888830, [])
    # Initialization on a fresh connection preserves both authorization and clock.
    db.init_db()
    assert task(client, action)["status"] == "pending"
    assert schedules.run_due_transfers() == 0
    set_clock(TARGET)
    ready = Barrier(2)

    def scan():
        ready.wait(timeout=5)
        return schedules.run_due_transfers()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(scan) for _ in range(2)]
        assert sum(result.result(timeout=10) for result in results) == 1
    balance, rows = ledger(action)
    assert balance == 858830
    assert len(rows) == 1
    assert rows[0]["posted_on"] == "2026-10-06"
    completed = task(client, action)
    assert completed["status"] == "completed"
    assert completed["transaction_id"] == rows[0]["id"]
    db.init_db()
    assert schedules.run_due_transfers() == 0
    assert ledger(action) == (balance, rows)


def test_confirmation_and_schedule_operations_are_bound_to_session(client):
    action = prepare(client)
    assert confirm(client, action, "intruder").status_code == 404
    assert confirm(client, action).status_code == 200
    assert confirm(client, action, "intruder").status_code == 404
    assert client.get("/api/schedules", params={"session_id": "intruder"}).json()["tasks"] == []
    cancelled = client.post(f"/api/schedules/{action['id']}/cancel", json={"session_id": "intruder"})
    assert cancelled.status_code == 404
    advanced = client.post("/api/demo/clock/advance-next", json={"session_id": "intruder"})
    assert advanced.status_code == 409
    assert task(client, action)["status"] == "pending"
    assert ledger(action) == (888830, [])


def test_cancel_is_idempotent_and_prevents_future_execution(client):
    action = authorize(client)
    url = f"/api/schedules/{action['id']}/cancel"
    for _ in range(2):
        cancelled = client.post(url, json={"session_id": SESSION})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
    set_clock(TARGET)
    assert schedules.run_due_transfers() == 0
    assert ledger(action) == (888830, [])


def test_cancel_and_execute_race_has_one_consistent_winner(client):
    action = authorize(client)
    set_clock(TARGET)
    ready = Barrier(2)

    def cancel():
        ready.wait(timeout=5)
        return schedules.cancel_schedule(action["id"], SESSION)

    def execute():
        ready.wait(timeout=5)
        return schedules.run_due_transfers()

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled, executed = pool.submit(cancel), pool.submit(execute)
        cancel_result = cancelled.result(timeout=10)
        executed.result(timeout=10)
    final = task(client, action)
    balance, rows = ledger(action)
    assert final["status"] == cancel_result["status"]
    assert final["status"] in ("cancelled", "completed")
    count = 1 if final["status"] == "completed" else 0
    assert len(rows) == count
    assert balance == 888830 - count * 30000
    assert schedules.run_due_transfers() == 0


def test_balance_is_rechecked_at_execution_and_failure_is_terminal(client):
    action = authorize(client)
    with db.db_session() as conn:
        conn.execute("UPDATE accounts SET balance_cents = 10000 WHERE id = ?", (db.ACCOUNT_ID,))
    set_clock(TARGET)
    schedules.run_due_transfers()
    failed = task(client, action)
    assert failed["status"] == "failed"
    assert "余额不足" in failed["failure_reason"]
    assert ledger(action) == (10000, [])
    with db.db_session() as conn:
        conn.execute("UPDATE accounts SET balance_cents = 888830 WHERE id = ?", (db.ACCOUNT_ID,))
    assert schedules.run_due_transfers() == 0
    assert ledger(action) == (888830, [])


def test_daily_limit_is_rechecked_using_execution_date(client):
    action = authorize(client)
    set_clock("2026-10-06T19:00:00+08:00")
    spent = prepare(client, "转给林悦900元")
    assert confirm(client, spent).status_code == 200
    set_clock(TARGET)
    schedules.run_due_transfers()
    failed = task(client, action)
    assert failed["status"] == "failed"
    assert "日累计" in failed["failure_reason"]
    assert ledger(action) == (798830, [])


def test_previous_days_transfer_does_not_use_future_days_limit(client):
    spent = prepare(client, "转给林悦900元")
    assert confirm(client, spent).status_code == 200
    action = authorize(client)
    assert action["tier"] == "yellow"
    set_clock(TARGET)
    schedules.run_due_transfers()
    assert task(client, action)["status"] == "completed"
    assert ledger(action)[0] == 768830


def test_oversized_schedule_cannot_be_authorized(client):
    action = prepare(client, "10月6日晚上8点转给林悦1000.01元")
    assert action["tier"] == "red"
    assert confirm(client, action).status_code == 403
    assert client.get("/api/schedules", params={"session_id": SESSION}).json()["tasks"] == []
    assert ledger(action) == (888830, [])


@pytest.mark.parametrize(("field", "value"), [
    ("account_ref", "DEMO-CHANGED"), ("phone", "13800009876"), ("verified", 0),
])
def test_recipient_routing_changes_invalidate_authorization(client, field, value):
    action = authorize(client)
    with db.db_session() as conn:
        conn.execute(f"UPDATE contacts SET {field} = ? WHERE id = ?", (value, "contact-linyue"))
    set_clock(TARGET)
    schedules.run_due_transfers()
    assert task(client, action)["status"] == "failed"
    assert ledger(action) == (888830, [])


def test_recipient_change_between_preview_and_confirmation_creates_no_task(client):
    action = prepare(client)
    with db.db_session() as conn:
        conn.execute("UPDATE contacts SET account_ref = 'DEMO-CHANGED' WHERE id = 'contact-linyue'")
    response = confirm(client, action)
    assert response.status_code == 409
    assert client.get("/api/schedules", params={"session_id": SESSION}).json()["tasks"] == []
    assert ledger(action) == (888830, [])


def test_real_confirmation_ttl_and_simulated_execution_time_are_independent(client, monkeypatch):
    class RealClock(datetime):
        value = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", RealClock)
    action = prepare(client)
    # Advancing two simulated days does not expire ten minutes of real consent time.
    set_clock("2026-10-02T09:00:00+08:00")
    assert confirm(client, action).status_code == 200
    # Consent has become a durable schedule; expiry of the preview no longer revokes it.
    RealClock.value += timedelta(days=8)
    set_clock(TARGET)
    schedules.run_due_transfers()
    assert task(client, action)["status"] == "completed"
    assert ledger(action)[0] == 858830


def test_expired_real_confirmation_cannot_create_future_schedule(client, monkeypatch):
    class RealClock(datetime):
        value = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    monkeypatch.setattr(service, "datetime", RealClock)
    action = prepare(client)
    RealClock.value += timedelta(minutes=10, seconds=1)
    response = confirm(client, action)
    assert response.status_code == 409
    assert "过期" in response.json()["detail"]
    assert client.get("/api/schedules", params={"session_id": SESSION}).json()["tasks"] == []
    assert ledger(action) == (888830, [])


def test_time_reached_before_confirmation_requires_new_plan(client):
    action = prepare(client)
    set_clock(TARGET)
    assert confirm(client, action).status_code == 409
    assert client.get("/api/schedules", params={"session_id": SESSION}).json()["tasks"] == []
    assert ledger(action) == (888830, [])


@pytest.mark.parametrize(("delay_seconds", "status", "debit"), [
    (599, "completed", 30000), (600, "expired", 0), (3600, "expired", 0),
])
def test_restart_recovers_only_within_authorized_execution_window(client, delay_seconds, status, debit):
    action = authorize(client)
    resumed = datetime.fromisoformat(TARGET) + timedelta(seconds=delay_seconds)
    set_clock(resumed.isoformat(timespec="seconds"))
    db.init_db()
    schedules.run_due_transfers()
    result = task(client, action)
    assert result["status"] == status
    balance, rows = ledger(action)
    assert balance == 888830 - debit
    assert len(rows) == (1 if debit else 0)
    if not debit:
        assert "窗口" in result["failure_reason"]
    assert schedules.run_due_transfers() == 0


def test_changed_task_payload_cannot_execute_under_original_consent(client):
    action = authorize(client)
    with db.db_session() as conn:
        changed = dict(action["details"], amount_cents=10000, amount_yuan="100.00")
        conn.execute("UPDATE scheduled_transfers SET payload_json = ? WHERE id = ?", (json.dumps(changed), action["id"]))
    set_clock(TARGET)
    schedules.run_due_transfers()
    failed = task(client, action)
    assert failed["status"] == "failed"
    assert "授权无效" in failed["failure_reason"]
    assert ledger(action) == (888830, [])


@pytest.mark.parametrize(("field", "changed_time", "scan_time"), [
    ("execute_at", "2026-10-05T20:00:00+08:00", "2026-10-05T20:00:00+08:00"),
    ("expires_at", "2026-10-06T21:00:00+08:00", "2026-10-06T20:30:00+08:00"),
])
def test_task_timestamps_cannot_bypass_authorized_time_or_execution_window(client, field, changed_time, scan_time):
    action = authorize(client)
    # Leave both authorization payloads intact; tamper only with the indexed columns.
    with db.db_session() as conn:
        conn.execute(f"UPDATE scheduled_transfers SET {field} = ? WHERE id = ?", (changed_time, action["id"]))
    set_clock(scan_time)
    schedules.run_due_transfers()
    assert task(client, action)["status"] == "failed"
    assert ledger(action) == (888830, [])


def test_failure_after_ledger_write_rolls_back_then_restart_executes_once(client, monkeypatch):
    action = authorize(client)
    set_clock(TARGET)
    real_execute = schedules.execute_transfer

    def fail_after_write(*args):
        real_execute(*args)
        raise RuntimeError("injected storage interruption after ledger write")

    monkeypatch.setattr(schedules, "execute_transfer", fail_after_write)
    with pytest.raises(RuntimeError, match="injected storage interruption"):
        schedules.run_due_transfers()
    assert ledger(action) == (888830, [])
    assert task(client, action)["status"] == "pending"
    monkeypatch.setattr(schedules, "execute_transfer", real_execute)
    db.init_db()
    assert schedules.run_due_transfers() == 1
    assert task(client, action)["status"] == "completed"
    assert ledger(action)[0] == 858830
    assert len(ledger(action)[1]) == 1
    assert schedules.run_due_transfers() == 0


def test_demo_advance_executes_and_controls_can_be_disabled(client, monkeypatch):
    action = authorize(client)
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "0")
    response = client.post("/api/demo/clock/advance-next", json={"session_id": SESSION})
    assert response.status_code == 403
    assert ledger(action) == (888830, [])
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    response = client.post("/api/demo/clock/advance-next", json={"session_id": SESSION})
    assert response.status_code == 200
    assert response.json()["clock"]["now"] == TARGET
    assert task(client, action)["status"] == "completed"
    assert ledger(action)[0] == 858830
