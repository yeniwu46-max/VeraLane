from __future__ import annotations

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
