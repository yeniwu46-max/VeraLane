from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import db, execution_controls as controls
from app.controls_api import router
from app.service import create_action


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "controls.sqlite3")
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db.init_db()
    with db.db_session() as conn:
        controls.init_schema(conn)
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


def balance():
    with db.db_session() as conn:
        return conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()[0]


def action(tier="red", session="owner", amount=120000):
    with transaction() as conn:
        return create_action(conn, session, "test_operation", tier, {"amount_cents": amount})


def action_row(conn, id):
    return conn.execute("SELECT * FROM actions WHERE id=?", (id,)).fetchone()


def test_amount_tier_threshold_is_strictly_over_one_thousand_yuan():
    assert controls.HIGH_RISK_THRESHOLD_CENTS == 100_000
    assert not controls.requires_red_tier(100_000)
    assert controls.requires_red_tier(100_001)


def challenge_state(id):
    with db.db_session() as conn:
        return dict(conn.execute("SELECT * FROM action_challenges WHERE id=?", (id,)).fetchone())


def test_reserve_release_are_idempotent_and_never_change_ledger_balance(client):
    before = balance()
    with transaction() as conn:
        first = controls.reserve(conn, "r1", "owner", 100000, "生日预算")
        second = controls.reserve(conn, "r1", "owner", 100000, "生日预算")
        assert first == second and controls.available_cents(conn) == before - 100000
    assert balance() == before
    with transaction() as conn:
        assert controls.release(conn, "r1", "owner")["released_cents"] == 100000
        assert controls.release(conn, "r1", "owner")["status"] == "released"
        assert controls.available_cents(conn) == before
    assert balance() == before


def test_debit_cannot_spend_funds_reserved_by_any_session(client):
    before = balance()
    with transaction() as conn:
        controls.reserve(conn, "r1", "one", before - 100, "用途一")
    with pytest.raises(HTTPException) as failure, transaction() as conn:
        controls.debit(conn, 101)
    assert failure.value.status_code == 409 and balance() == before
    with transaction() as conn:
        controls.debit(conn, 100)
        assert controls.available_cents(conn) == 0


def test_consume_debits_once_reduces_only_own_hold_and_tracks_partial_release(client):
    before = balance()
    with transaction() as conn:
        controls.reserve(conn, "r1", "owner", 100000, "生日预算")
        controls.reserve(conn, "r2", "other", before - 100000, "其他预算")
        receipt = controls.consume(conn, "r1", "owner", 30000)
        assert receipt["remaining_cents"] == 70000 and receipt["consumed_cents"] == 30000
        assert controls.available_cents(conn) == 0
    assert balance() == before - 30000
    with transaction() as conn:
        receipt = controls.release(conn, "r1", "owner")
        assert receipt["released_cents"] == 70000 and receipt["consumed_cents"] == 30000
        assert controls.available_cents(conn) == 70000
    assert balance() == before - 30000


def test_full_consumption_cannot_be_repeated(client):
    with transaction() as conn:
        controls.reserve(conn, "r1", "owner", 500, "午餐")
        assert controls.consume(conn, "r1", "owner", 500)["status"] == "consumed"
    before = balance()
    with pytest.raises(HTTPException), transaction() as conn:
        controls.consume(conn, "r1", "owner", 1)
    assert balance() == before


def test_consume_rolls_back_balance_and_hold_when_later_step_fails(client):
    with transaction() as conn:
        controls.reserve(conn, "r1", "owner", 50000, "采购")
    before = balance()
    with pytest.raises(RuntimeError), transaction() as conn:
        controls.consume(conn, "r1", "owner", 20000)
        raise RuntimeError("downstream write failed")
    assert balance() == before
    snapshot = client.get("/api/funds", params={"session_id": "owner"}).json()
    assert snapshot["reservations"][0]["remaining_cents"] == 50000


def test_overconsumption_rejects_before_debit(client):
    with transaction() as conn:
        controls.reserve(conn, "r1", "owner", 500, "午餐")
    before = balance()
    with pytest.raises(HTTPException), transaction() as conn:
        controls.consume(conn, "r1", "owner", 501)
    assert balance() == before


def test_fund_resources_are_session_bound_and_amount_idempotency_is_strict(client):
    with transaction() as conn:
        controls.reserve(conn, "r1", "owner", 500, "午餐")
    for operation in (lambda c: controls.consume(c, "r1", "stranger", 100),
                      lambda c: controls.release(c, "r1", "stranger"),
                      lambda c: controls.reserve(c, "r1", "stranger", 500, "午餐")):
        with pytest.raises(HTTPException) as failure, transaction() as conn:
            operation(conn)
        assert failure.value.status_code == 404
    with pytest.raises(HTTPException) as failure, transaction() as conn:
        controls.reserve(conn, "r1", "owner", 600, "午餐")
    assert failure.value.status_code == 409
    assert client.get("/api/funds", params={"session_id": "stranger"}).json()["reservations"] == []


def test_concurrent_reservations_cannot_overbook_same_balance(client):
    def attempt(index):
        try:
            with transaction() as conn:
                controls.reserve(conn, f"r{index}", "owner", 600000, "预算")
            return True
        except HTTPException:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(attempt, range(4))) == 1


def test_concurrent_debit_and_reserve_cannot_spend_same_funds(client):
    def attempt(kind):
        try:
            with transaction() as conn:
                if kind == "reserve":
                    controls.reserve(conn, "r", "owner", 600000, "预算")
                else:
                    controls.debit(conn, 600000)
            return True
        except HTTPException:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, ("reserve", "debit"))) == 1


@pytest.mark.parametrize("amount", [0, -1, 1.2, True, 10000001])
def test_invalid_cents_never_change_money(client, amount):
    before = balance()
    with pytest.raises(HTTPException), transaction() as conn:
        controls.reserve(conn, "bad", "owner", amount, "用途")
    with pytest.raises(HTTPException), transaction() as conn:
        controls.debit(conn, amount)
    assert balance() == before


def test_helpers_require_caller_transaction(client):
    conn = db.connect()
    try:
        with pytest.raises(RuntimeError):
            controls.debit(conn, 100)
    finally:
        conn.close()


def test_prepare_only_creates_action_and_rechecks_balance_at_execution(client):
    before = balance()
    response = client.post("/api/funds/reservations/prepare", json={"session_id": "owner", "amount_yuan": "8000", "purpose": "生日预算"})
    prepared = response.json()["pending_action"]
    assert prepared["type"] == "fund_reserve" and balance() == before
    assert client.get("/api/funds", params={"session_id": "owner"}).json()["reservations"] == []
    with transaction() as conn:
        controls.debit(conn, 100000)
    with pytest.raises(HTTPException), transaction() as conn:
        controls.execute_reserve(conn, prepared["id"], "owner", prepared["details"])


def test_release_confirmation_snapshot_cannot_ignore_intervening_consumption(client):
    with transaction() as conn:
        controls.reserve(conn, "r", "owner", 10000, "预算")
    prepared = client.post("/api/funds/reservations/r/release/prepare", json={"session_id": "owner"}).json()["pending_action"]
    with transaction() as conn:
        controls.consume(conn, "r", "owner", 1000)
    with pytest.raises(HTTPException) as failure, transaction() as conn:
        controls.execute_release(conn, prepared["id"], "owner", prepared["details"])
    assert failure.value.status_code == 409


def test_valid_reserve_and_release_execution_helpers(client):
    with transaction() as conn:
        result = controls.execute_reserve(conn, "operation", "owner", {"amount_cents": 10000, "purpose": "预算"})
    assert result["reservation_id"] == "reserve-operation"
    prepared = controls.prepare_release("owner", result["reservation_id"])["pending_action"]
    with transaction() as conn:
        released = controls.execute_release(conn, prepared["id"], "owner", prepared["details"])
    assert released["reservation"]["status"] == "released"


def test_challenge_is_bound_to_action_and_success_does_not_execute_it(client):
    plan = action()
    challenge = client.post(f"/api/actions/{plan['id']}/challenge", json={"session_id": "owner"}).json()
    assert challenge["mode"] == "simulated" and len(challenge["demo_code"]) == 6
    before = balance()
    result = client.post(f"/api/actions/{plan['id']}/verify", json={"session_id": "owner", "challenge_id": challenge["challenge_id"], "code": challenge["demo_code"]})
    assert result.status_code == 200 and result.json()["verified"] is True
    assert balance() == before
    with db.db_session() as conn:
        row = action_row(conn, plan["id"])
        assert row["status"] == "pending"
        controls.require_verified(conn, row)
    stored = challenge_state(challenge["challenge_id"])
    assert stored["code_hash"] != challenge["demo_code"] and "demo_code" not in stored


def test_three_wrong_codes_persist_lock_across_failed_http_requests(client):
    plan = action()
    challenge = controls.issue_challenge(plan["id"], "owner")
    wrong = "000000" if challenge["demo_code"] != "000000" else "999999"
    for attempt in range(1, 4):
        response = client.post(f"/api/actions/{plan['id']}/verify", json={"session_id": "owner", "challenge_id": challenge["challenge_id"], "code": wrong})
        assert response.status_code == 403
        assert challenge_state(challenge["challenge_id"])["attempts"] == attempt
    assert challenge_state(challenge["challenge_id"])["status"] == "locked"
    with pytest.raises(HTTPException):
        controls.verify_challenge(plan["id"], "owner", challenge["challenge_id"], challenge["demo_code"])


def test_new_challenge_revokes_old_verified_authorization(client):
    plan = action()
    first = controls.issue_challenge(plan["id"], "owner")
    controls.verify_challenge(plan["id"], "owner", first["challenge_id"], first["demo_code"])
    second = controls.issue_challenge(plan["id"], "owner")
    assert challenge_state(first["challenge_id"])["status"] == "revoked"
    with pytest.raises(HTTPException), db.db_session() as conn:
        controls.require_verified(conn, action_row(conn, plan["id"]))
    controls.verify_challenge(plan["id"], "owner", second["challenge_id"], second["demo_code"])


def test_cross_session_and_cross_action_verification_rejected(client):
    first, second = action(), action()
    challenge = controls.issue_challenge(first["id"], "owner")
    assert client.post(f"/api/actions/{first['id']}/challenge", json={"session_id": "stranger"}).status_code == 404
    for id, session in ((first["id"], "stranger"), (second["id"], "owner")):
        with pytest.raises(HTTPException) as failure:
            controls.verify_challenge(id, session, challenge["challenge_id"], challenge["demo_code"])
        assert failure.value.status_code == 404


def test_tampered_payload_revokes_challenge_and_old_authorization(client):
    plan = action()
    challenge = controls.issue_challenge(plan["id"], "owner")
    with transaction() as conn:
        conn.execute("UPDATE actions SET payload_json=? WHERE id=?", (json.dumps({"amount_cents": 900000}), plan["id"]))
    with pytest.raises(HTTPException) as failure:
        controls.verify_challenge(plan["id"], "owner", challenge["challenge_id"], challenge["demo_code"])
    assert failure.value.status_code == 409
    assert challenge_state(challenge["challenge_id"])["status"] == "revoked"


def test_authorization_rechecks_database_payload_at_execution(client):
    plan = action()
    challenge = controls.issue_challenge(plan["id"], "owner")
    controls.verify_challenge(plan["id"], "owner", challenge["challenge_id"], challenge["demo_code"])
    with transaction() as conn:
        stale = action_row(conn, plan["id"])
        conn.execute("UPDATE actions SET payload_json=? WHERE id=?", ('{"amount_cents":1}', plan["id"]))
        with pytest.raises(HTTPException):
            controls.require_verified(conn, stale)


def test_challenge_and_verified_grant_expire_using_real_time(client, monkeypatch):
    plan = action()
    challenge = controls.issue_challenge(plan["id"], "owner")
    controls.verify_challenge(plan["id"], "owner", challenge["challenge_id"], challenge["demo_code"])
    monkeypatch.setattr(controls, "_now", lambda: datetime.now(timezone.utc) + timedelta(minutes=4))
    with pytest.raises(HTTPException), db.db_session() as conn:
        controls.require_verified(conn, action_row(conn, plan["id"]))
    with pytest.raises(HTTPException):
        controls.verify_challenge(plan["id"], "owner", challenge["challenge_id"], challenge["demo_code"])
    assert challenge_state(challenge["challenge_id"])["status"] == "expired"


def test_action_expiration_cannot_be_extended_by_new_challenge(client):
    plan = action()
    soon = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat(timespec="seconds")
    with transaction() as conn:
        conn.execute("UPDATE actions SET expires_at=? WHERE id=?", (soon, plan["id"]))
    assert controls.issue_challenge(plan["id"], "owner")["expires_at"] == soon


def test_completed_action_cannot_reuse_verification(client):
    plan = action()
    challenge = controls.issue_challenge(plan["id"], "owner")
    controls.verify_challenge(plan["id"], "owner", challenge["challenge_id"], challenge["demo_code"])
    with transaction() as conn:
        conn.execute("UPDATE actions SET status='completed' WHERE id=?", (plan["id"],))
    with pytest.raises(HTTPException):
        controls.issue_challenge(plan["id"], "owner")
    with pytest.raises(HTTPException), db.db_session() as conn:
        controls.require_verified(conn, action_row(conn, plan["id"]))


def test_demo_disabled_blocks_challenges_and_existing_grants_but_not_yellow(client, monkeypatch):
    plan = action()
    challenge = controls.issue_challenge(plan["id"], "owner")
    controls.verify_challenge(plan["id"], "owner", challenge["challenge_id"], challenge["demo_code"])
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "0")
    assert client.post(f"/api/actions/{plan['id']}/challenge", json={"session_id": "owner"}).status_code == 403
    with pytest.raises(HTTPException), db.db_session() as conn:
        controls.require_verified(conn, action_row(conn, plan["id"]))
    yellow = action(tier="yellow")
    with db.db_session() as conn:
        controls.require_verified(conn, action_row(conn, yellow["id"]))


def test_api_rejects_extra_account_amount_or_authorization_fields(client):
    plan = action()
    assert client.post(f"/api/actions/{plan['id']}/challenge", json={"session_id": "owner", "verified": True}).status_code == 422
    assert client.post("/api/funds/reservations/prepare", json={"session_id": "owner", "amount_yuan": "10", "purpose": "预算", "account_id": "other"}).status_code == 422
    assert client.post("/api/funds/reservations/prepare", json={"session_id": "owner", "amount_yuan": "10", "purpose": "   "}).status_code == 422
