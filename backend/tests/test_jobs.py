"""Cross-scenario clock advancement must respect the global earliest event."""

from datetime import datetime

import pytest
from fastapi import HTTPException

from app import db, execution_controls as controls, investments, jobs, life_tasks, recurring
from app.agent import Intent
from app.service import confirm_action, prepare_transfer


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db.init_db()


def confirm(action, session):
    if action["tier"] == "red":
        challenge = controls.issue_challenge(action["id"], session)
        controls.verify_challenge(action["id"], session, challenge["challenge_id"], challenge["demo_code"])
    return confirm_action(action["id"], session)


def schedule(session, at):
    with db.db_session() as conn:
        result = prepare_transfer(conn, session, "offline", Intent(action="transfer", recipient="林悦", amount_yuan="1"), "转给林悦1元", {"execute_at": at})
    action = result["pending_action"]
    confirm(action, session)
    return action["id"]


def birthday(session="owner"):
    action = life_tasks.prepare({"session_id": session, "goal": "爱人生日", "birthday": "2026-10-05", "budget_yuan": "1000",
        "recipient_label": "林悦（虚构）", "delivery_note": "虚构配送地址", "product_ids": ["flowers-classic", "cake-classic"]})["pending_action"]
    return confirm(action, session)["task_id"]


def redemption(session="investor"):
    investments.assess(session, [3, 3, 3, 3, 3])
    bought = confirm(investments.prepare_buy(session, "product-stable", "100")["pending_action"], session)
    return confirm(investments.prepare_redeem(session, bought["holding_id"], "50")["pending_action"], session)


def schedule_status(id):
    with db.db_session() as conn:
        return conn.execute("SELECT status FROM scheduled_transfers WHERE id=?", (id,)).fetchone()[0]


def account_balance():
    with db.db_session() as conn:
        return conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()[0]


def test_advance_from_later_session_processes_earlier_other_session_event_first():
    early = schedule("early-owner", "2026-10-01T08:00:00+08:00")
    late = schedule("later-owner", "2026-10-01T10:00:00+08:00")
    snapshot = jobs.events_snapshot("later-owner")
    assert len(snapshot["events"]) == 1 and snapshot["events"][0]["id"] == late
    assert snapshot["next_at"] == "2026-10-01T08:00:00+08:00"
    jobs.advance_events("later-owner")
    assert schedule_status(early) == "completed" and schedule_status(late) == "pending"
    assert jobs.events_snapshot("later-owner")["clock"]["now"] == "2026-10-01T08:00:00+08:00"
    jobs.advance_events("later-owner")
    assert schedule_status(late) == "completed"


def test_same_timestamp_events_all_execute_without_duplicate_charges():
    first = schedule("first", "2026-10-01T09:00:00+08:00")
    second = schedule("second", "2026-10-01T09:00:00+08:00")
    before = account_balance()
    jobs.advance_events("first")
    assert schedule_status(first) == schedule_status(second) == "completed"
    assert account_balance() == before - 200
    with db.db_session() as conn:
        assert jobs.run_due(conn) == 0


def test_birthday_advancement_creates_orders_then_delivers_on_separate_dates():
    id = birthday()
    assert jobs.events_snapshot("owner")["events"][0]["label"] == "生日订单创建"
    jobs.advance_events("owner")
    assert life_tasks.get_task(id, "owner")["status"] == "ordered"
    snapshot = jobs.events_snapshot("owner")
    assert len(snapshot["events"]) == 2 and {event["label"] for event in snapshot["events"]} == {"模拟生日配送"}
    assert snapshot["next_at"] == "2026-10-05T09:00:00+08:00"
    before = account_balance()
    jobs.advance_events("owner")
    assert life_tasks.get_task(id, "owner")["status"] == "completed" and account_balance() == before
    assert jobs.events_snapshot("owner")["events"] == []


def test_task_cancellation_does_not_remove_existing_paid_orders_from_event_queue():
    id = birthday()
    jobs.advance_events("owner")
    cancel = life_tasks.prepare_cancel(id, "owner")["pending_action"]
    confirm(cancel, "owner")
    assert len(jobs.events_snapshot("owner")["events"]) == 2
    jobs.advance_events("owner")
    detail = life_tasks.get_task(id, "owner")
    assert detail["status"] == "cancelled" and all(order["status"] == "delivered" for order in detail["orders"])


def test_t_plus_one_redemption_is_not_spendable_before_settlement_event():
    result = redemption()
    before = account_balance()
    assert result["settlement_status"] == "pending_settlement"
    assert jobs.events_snapshot("investor")["next_at"] == "2026-10-01T09:00:00+08:00"
    jobs.advance_events("investor")
    assert account_balance() == before + 5000
    assert jobs.events_snapshot("investor")["events"] == []
    with db.db_session() as conn:
        assert jobs.run_due(conn) == 0


def test_earlier_morning_event_does_not_settle_nine_oclock_redemption_early():
    redemption()
    schedule("early-owner", "2026-10-01T08:00:00+08:00")
    before = account_balance()
    jobs.advance_events("investor")
    assert account_balance() == before - 100
    assert investments.portfolio("investor")["orders"][0]["status"] == "pending_settlement"
    jobs.advance_events("investor")
    assert account_balance() == before - 100 + 5000


def test_disabled_clock_control_preserves_pending_authorized_events(monkeypatch):
    id = birthday()
    before = jobs.events_snapshot("owner")["clock"]["now"]
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "0")
    with pytest.raises(HTTPException) as failure:
        jobs.advance_events("owner")
    assert failure.value.status_code == 403
    snapshot = jobs.events_snapshot("owner")
    assert snapshot["controls_enabled"] is False and snapshot["clock"]["now"] == before
    assert life_tasks.get_task(id, "owner")["status"] == "scheduled"


def test_session_without_events_cannot_advance_somebody_elses_clock():
    birthday("owner")
    before = jobs.events_snapshot("owner")["clock"]["now"]
    with pytest.raises(HTTPException) as failure:
        jobs.advance_events("stranger")
    assert failure.value.status_code == 409
    assert jobs.events_snapshot("owner")["clock"]["now"] == before


def test_recurring_event_is_ordered_globally_before_other_sessions_birthday():
    birthday("birthday-owner")
    with db.db_session() as conn:
        contact_id = conn.execute("SELECT id FROM contacts WHERE name='林悦'").fetchone()[0]
    action = recurring.prepare_recurring({'session_id':'recurring-owner','contact_id':contact_id,'amount_yuan':'1','note':'周期',
        'first_at':'2026-10-01T09:00','monthly_day':1,'count':2})['pending_action']
    confirm(action,'recurring-owner')
    jobs.advance_events('birthday-owner')
    value = recurring.get_recurring(action['id'],'recurring-owner')
    assert value['occurrences'][0]['status']=='completed' and value['occurrences'][1]['status']=='pending'
    assert jobs.events_snapshot('birthday-owner')['next_at']=='2026-10-03T09:00:00+08:00'
    cancel = recurring.prepare_cancel(action['id'],'recurring-owner')['pending_action']
    confirm(cancel,'recurring-owner')
    assert jobs.events_snapshot('recurring-owner')['events']==[]
