from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.aa_settlement import calculate_preview
from app import db
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "demo.sqlite3")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with TestClient(app) as test_client:
        yield test_client


def test_multi_payer_preview_offsets_debts_and_splits_remainder_exactly():
    result = calculate_preview(
        [{"id": "self", "name": "我"}, {"id": "lin", "name": "林悦"}, {"id": "wang", "name": "王明"}],
        [{"payer_id": "self", "amount_yuan": "100.00", "note": "门票"}, {"payer_id": "lin", "amount_yuan": "50.00", "note": "打车"}],
        "周末出游",
    )
    assert result["total_yuan"] == "150.00"
    assert [row["share_yuan"] for row in result["participants"]] == ["50.00", "50.00", "50.00"]
    assert [row["net_yuan"] for row in result["participants"]] == ["50.00", "0.00", "-50.00"]
    assert result["transfers"] == [{"from_id": "wang", "from_name": "王明", "to_id": "self", "to_name": "我", "amount_cents": 5000, "amount_yuan": "50.00"}]
    assert result["note"] == "周末出游"


def test_multi_payer_preview_uses_deterministic_cent_remainder():
    result = calculate_preview(
        [{"id": "self", "name": "我"}, {"id": "lin", "name": "林悦"}, {"id": "wang", "name": "王明"}],
        [{"payer_id": "lin", "amount_yuan": "1.00", "note": ""}],
        "",
    )
    assert [row["share_yuan"] for row in result["participants"]] == ["0.34", "0.33", "0.33"]
    assert sum(row["share_cents"] for row in result["participants"]) == result["total_cents"]
    assert [(item["from_id"], item["to_id"], item["amount_yuan"]) for item in result["transfers"]] == [
        ("self", "lin", "0.34"), ("wang", "lin", "0.33")]


def test_multi_payer_preview_rejects_nonpositive_and_unknown_payers():
    with pytest.raises(HTTPException) as amount_error:
        calculate_preview([{"id": "self", "name": "我"}, {"id": "lin", "name": "林悦"}],
                          [{"payer_id": "lin", "amount_yuan": "0", "note": ""}], "")
    assert amount_error.value.status_code == 422
    with pytest.raises(HTTPException) as payer_error:
        calculate_preview([{"id": "self", "name": "我"}, {"id": "lin", "name": "林悦"}],
                          [{"payer_id": "unknown", "amount_yuan": "1.00", "note": ""}], "")
    assert payer_error.value.status_code == 422


def test_preview_api_uses_only_verified_contacts_and_never_changes_balance(client):
    contacts = client.get("/api/contacts").json()
    before = client.get("/api/overview").json()["account"]["balance_yuan"]
    response = client.post("/api/aa/settlements/preview", json={
        "session_id": "aa-settlement-session",
        "contact_ids": [contacts[0]["id"], contacts[1]["id"]],
        "note": "出游费用",
        "expenses": [
            {"payer_id": "self", "amount_yuan": "100.00", "note": "门票"},
            {"payer_id": contacts[0]["id"], "amount_yuan": "50.00", "note": "打车"},
        ],
    })
    assert response.status_code == 200
    result = response.json()
    assert result["total_yuan"] == "150.00"
    assert result["expense_count"] == 2
    assert "不会发起转账" in result["notice"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == before

    response = client.post("/api/aa/settlements/preview", json={
        "session_id": "aa-settlement-session", "contact_ids": ["missing-contact"],
        "expenses": [{"payer_id": "self", "amount_yuan": "10.00"}],
    })
    assert response.status_code == 422


def settlement_input(contact_ids, expenses, note="多人垫付结算", session="aa-settlement-session"):
    return {"session_id": session, "contact_ids": contact_ids, "expenses": expenses, "note": note}


def create_settlement(client, contact_ids, expenses, note="多人垫付结算", session="aa-settlement-session"):
    response = client.post("/api/aa/settlements/prepare", json=settlement_input(contact_ids, expenses, note, session))
    assert response.status_code == 200, response.text
    action = response.json()["pending_action"]
    assert action["type"] == "aa_settlement"
    saved = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session})
    assert saved.status_code == 200, saved.text
    return saved.json()["settlement_id"]


def test_settlement_plan_is_confirmed_persisted_and_session_owned(client):
    contacts = client.get("/api/contacts").json()
    ids = [contacts[0]["id"], contacts[1]["id"], contacts[2]["id"]]
    input_data = settlement_input(ids, [
        {"payer_id": "self", "amount_yuan": "100.00", "note": "门票"},
        {"payer_id": ids[0], "amount_yuan": "300.00", "note": "住宿"},
        {"payer_id": ids[2], "amount_yuan": "200.00", "note": "餐费"},
    ], "周末出游")
    balance_before = client.get("/api/overview").json()["account"]["balance_yuan"]
    response = client.post("/api/aa/settlements/prepare", json=input_data)
    assert response.status_code == 200, response.text
    action = response.json()["pending_action"]
    assert action["tier"] == "yellow"
    assert client.get("/api/aa/settlements", params={"session_id": input_data["session_id"]}).json()["settlements"] == []
    saved = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": input_data["session_id"]})
    assert saved.status_code == 200, saved.text
    settlement_id = saved.json()["settlement_id"]
    detail = client.get(f"/api/aa/settlements/{settlement_id}", params={"session_id": input_data["session_id"]})
    assert detail.status_code == 200
    data = detail.json()
    assert data["note"] == "周末出游"
    assert data["total_yuan"] == "600.00"
    assert [row["status"] for row in data["transfers"]] == ["pending", "external", "external"]
    assert "不代表已付款" in data["notice"]
    assert client.get("/api/aa/settlements", params={"session_id": input_data["session_id"]}).json()["settlements"][0]["id"] == settlement_id
    assert client.get(f"/api/aa/settlements/{settlement_id}", params={"session_id": "another-session"}).status_code == 404
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == balance_before


def test_only_owner_settlement_legs_execute_and_are_idempotent(client):
    contacts = client.get("/api/contacts").json()
    lin, chen = contacts[0]["id"], contacts[1]["id"]
    settlement_id = create_settlement(client, [lin, chen], [
        {"payer_id": lin, "amount_yuan": "1.00", "note": "聚餐"},
    ])
    plan = client.get(f"/api/aa/settlements/{settlement_id}", params={"session_id": "aa-settlement-session"}).json()
    assert [(item["from_id"], item["to_id"]) for item in plan["transfers"]] == [("self", lin), (chen, lin)]
    out_leg, external_leg = plan["transfers"]
    denied = client.post(f"/api/aa/settlements/{settlement_id}/legs/{external_leg['id']}/prepare",
                         json={"session_id": "aa-settlement-session"})
    assert denied.status_code == 409
    assert "不会代他们转账" in denied.json()["detail"]

    balance_before = client.get("/api/overview").json()["account"]["balance_yuan"]
    first = client.post(f"/api/aa/settlements/{settlement_id}/legs/{out_leg['id']}/prepare",
                        json={"session_id": "aa-settlement-session"}).json()["pending_action"]
    duplicate = client.post(f"/api/aa/settlements/{settlement_id}/legs/{out_leg['id']}/prepare",
                            json={"session_id": "aa-settlement-session"}).json()["pending_action"]
    assert duplicate["id"] == first["id"]
    result = client.post(f"/api/actions/{first['id']}/confirm", json={"session_id": "aa-settlement-session"})
    assert result.status_code == 200, result.text
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == f"{Decimal(balance_before) - Decimal('0.34'):.2f}"
    replay = client.post(f"/api/actions/{first['id']}/confirm", json={"session_id": "aa-settlement-session"})
    assert replay.status_code == 200
    assert replay.json()["transaction_id"] == result.json()["transaction_id"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == f"{Decimal(balance_before) - Decimal('0.34'):.2f}"
    refreshed = client.get(f"/api/aa/settlements/{settlement_id}", params={"session_id": "aa-settlement-session"}).json()
    assert [item["status"] for item in refreshed["transfers"]] == ["completed", "external"]
    assert refreshed["status"] == "owner_actions_complete"


def test_settlement_incoming_leg_credits_only_after_explicit_confirmation(client):
    contact = client.get("/api/contacts").json()[0]
    settlement_id = create_settlement(client, [contact["id"]], [
        {"payer_id": "self", "amount_yuan": "100.00", "note": "餐费"},
    ])
    plan = client.get(f"/api/aa/settlements/{settlement_id}", params={"session_id": "aa-settlement-session"}).json()
    leg = plan["transfers"][0]
    assert leg["from_id"] == contact["id"] and leg["to_id"] == "self"
    before = client.get("/api/overview").json()["account"]["balance_yuan"]
    action = client.post(f"/api/aa/settlements/{settlement_id}/legs/{leg['id']}/prepare",
                         json={"session_id": "aa-settlement-session"}).json()["pending_action"]
    assert action["details"]["direction"] == "in"
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == before
    done = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "aa-settlement-session"})
    assert done.status_code == 200, done.text
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == f"{float(before) + 50:.2f}"


def test_large_owner_settlement_outflow_requires_bound_demo_challenge(client):
    contact = client.get("/api/contacts").json()[0]
    settlement_id = create_settlement(client, [contact["id"]], [
        {"payer_id": contact["id"], "amount_yuan": "3000.00", "note": "住宿"},
    ])
    plan = client.get(f"/api/aa/settlements/{settlement_id}", params={"session_id": "aa-settlement-session"}).json()
    leg = plan["transfers"][0]
    action = client.post(f"/api/aa/settlements/{settlement_id}/legs/{leg['id']}/prepare",
                         json={"session_id": "aa-settlement-session"}).json()["pending_action"]
    assert action["tier"] == "red"
    assert client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "aa-settlement-session"}).status_code == 403
    challenge = client.post(f"/api/actions/{action['id']}/challenge", json={"session_id": "aa-settlement-session"}).json()
    verified = client.post(f"/api/actions/{action['id']}/verify", json={
        "session_id": "aa-settlement-session", "challenge_id": challenge["challenge_id"], "code": challenge["demo_code"],
    })
    assert verified.status_code == 200
    completed = client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": "aa-settlement-session"})
    assert completed.status_code == 200, completed.text
    assert completed.json()["amount_yuan"] == "1500.00"


def test_daily_transfer_threshold_change_invalidates_yellow_settlement_action(client):
    contact = client.get("/api/contacts").json()[0]
    settlement_id = create_settlement(client, [contact["id"]], [
        {"payer_id": contact["id"], "amount_yuan": "2.00", "note": "餐费"},
    ])
    plan = client.get(f"/api/aa/settlements/{settlement_id}", params={"session_id": "aa-settlement-session"}).json()
    leg = plan["transfers"][0]
    first = client.post(f"/api/aa/settlements/{settlement_id}/legs/{leg['id']}/prepare",
                        json={"session_id": "aa-settlement-session"}).json()["pending_action"]
    assert first["tier"] == "yellow"

    with db.db_session() as conn:
        conn.execute("UPDATE accounts SET balance_cents=balance_cents-99950 WHERE id=?", (db.ACCOUNT_ID,))
        conn.execute("""INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note)
            VALUES('prior-transfer',?,'2026-09-30','out',99950,'测试联系人','转账','日限额测试')""", (db.ACCOUNT_ID,))
    before_confirm = client.get("/api/overview").json()["account"]["balance_yuan"]
    rejected = client.post(f"/api/actions/{first['id']}/confirm", json={"session_id": "aa-settlement-session"})
    assert rejected.status_code == 409
    assert "强验证" in rejected.json()["detail"]
    assert client.get("/api/overview").json()["account"]["balance_yuan"] == before_confirm

    escalated = client.post(f"/api/aa/settlements/{settlement_id}/legs/{leg['id']}/prepare",
                            json={"session_id": "aa-settlement-session"}).json()["pending_action"]
    assert escalated["tier"] == "red"
    assert escalated["id"] != first["id"]
    assert client.post(f"/api/actions/{first['id']}/confirm", json={"session_id": "aa-settlement-session"}).status_code == 409
    challenge = client.post(f"/api/actions/{escalated['id']}/challenge", json={"session_id": "aa-settlement-session"}).json()
    verified = client.post(f"/api/actions/{escalated['id']}/verify", json={
        "session_id": "aa-settlement-session", "challenge_id": challenge["challenge_id"], "code": challenge["demo_code"],
    })
    assert verified.status_code == 200
    completed = client.post(f"/api/actions/{escalated['id']}/confirm", json={"session_id": "aa-settlement-session"})
    assert completed.status_code == 200, completed.text
