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
