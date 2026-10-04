"""Explicit alias consent, identity-safe resolution and nonblocking history hints."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import aliases, db
from app.aliases_api import Owner, router
from app.service import confirm_action, direct_prepare_transfer, process_message


SESSION = "alias-owner"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "aliases.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db.init_db()
    with db.db_session() as conn:
        aliases.init_schema(conn)
    app = FastAPI()
    app.include_router(router)

    @app.post("/api/actions/{action_id}/confirm")
    def confirm(action_id: str, body: Owner):
        return confirm_action(action_id, body.session_id)

    with TestClient(app) as test_client:
        yield test_client


def prepare(client, **changes):
    return client.post("/api/aliases/prepare", json={"session_id": SESSION, "alias": "房东", "contact_id": "contact-linyue", **changes})


def confirm(client, action, session=SESSION):
    return client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session})


def save(client, **changes):
    response = prepare(client, **changes)
    assert response.status_code == 200, response.text
    action = response.json()["pending_action"]
    completed = confirm(client, action, changes.get("session_id", SESSION))
    assert completed.status_code == 200, completed.text
    return action, completed.json()["alias"]


def test_explicit_relation_needs_confirmation(client):
    response = client.post("/api/aliases/interpret", json={"session_id": SESSION, "message": "房东是林悦"}).json()
    assert response["status"] == "ok"
    action = response["pending_action"]
    assert action["tier"] == "yellow"
    assert client.get("/api/aliases", params={"session_id": SESSION}).json()["aliases"] == []
    assert confirm(client, action).status_code == 200
    rows = client.get("/api/aliases", params={"session_id": SESSION}).json()["aliases"]
    assert rows[0]["alias"] == "房东"
    assert rows[0]["contact_id"] == "contact-linyue"
    assert rows[0]["phone_masked"] == "138****1234"
    assert "phone" not in rows[0]


def test_same_name_does_not_choose_arbitrarily(client):
    response = client.post("/api/aliases/interpret", json={"session_id": SESSION, "message": "同学是王明"}).json()
    assert response["status"] == "needs_clarification"
    assert {contact["id"] for contact in response["choices"]} == {"contact-wang1", "contact-wang2"}
    assert "pending_action" not in response


@pytest.mark.parametrize("alias", ["林悦", "王明", "13800001234", "转给房东", "明天", "备注", "<script>", "a" * 21])
def test_alias_cannot_shadow_real_identity_or_commands(client, alias):
    assert prepare(client, alias=alias).status_code == 422


def test_session_ownership_and_confirmation_replay(client):
    action, saved = save(client)
    assert client.get("/api/aliases", params={"session_id": "other"}).json()["aliases"] == []
    assert prepare(client, alias_id=saved["id"], session_id="other").status_code == 404
    assert confirm(client, action).status_code == 200
    assert confirm(client, action, "other").status_code == 404
    assert len(client.get("/api/aliases", params={"session_id": SESSION}).json()["aliases"]) == 1


def test_edit_changes_version_and_invalidates_stale_delete(client):
    _, saved = save(client)
    old_delete = client.post(f"/api/aliases/{saved['id']}/delete/prepare", json={"session_id": SESSION}).json()["pending_action"]
    _, changed = save(client, alias_id=saved["id"], contact_id="contact-chen")
    assert changed["version"] == 2
    assert changed["contact_id"] == "contact-chen"
    assert confirm(client, old_delete).status_code == 409
    current_delete = client.post(f"/api/aliases/{saved['id']}/delete/prepare", json={"session_id": SESSION}).json()["pending_action"]
    assert confirm(client, current_delete).status_code == 200
    assert client.get("/api/aliases", params={"session_id": SESSION}).json()["aliases"] == []


def test_duplicate_creation_rechecked_at_confirmation(client):
    first = prepare(client).json()["pending_action"]
    second = prepare(client, contact_id="contact-chen").json()["pending_action"]
    assert confirm(client, first).status_code == 200
    assert confirm(client, second).status_code == 409


def test_discarded_alias_draft_cannot_be_confirmed(client):
    action = prepare(client).json()["pending_action"]
    path = f"/api/aliases/actions/{action['id']}/discard"
    assert client.post(path, json={"session_id": "other"}).status_code == 404
    assert client.post(path, json={"session_id": SESSION}).status_code == 200
    assert client.post(path, json={"session_id": SESSION}).status_code == 200
    assert confirm(client, action).status_code == 409


def test_resolution_is_exact_owned_and_memo_is_not_instruction(client):
    save(client)
    with db.db_session() as conn:
        resolved = aliases.resolve_transfer_alias(conn, SESSION, "转给房东100元")
        assert resolved["status"] == "resolved"
        assert resolved["contact_id"] == "contact-linyue"
        assert resolved["phone"] == "13800001234"
        assert aliases.resolve_transfer_alias(conn, "other", "转给房东100元") is None
        assert aliases.resolve_transfer_alias(conn, SESSION, "给林悦100元，备注给房东") is None
        assert aliases.resolve_transfer_alias(conn, SESSION, "转给房东太太100元") is None
        assert aliases.resolve_transfer_alias(conn, SESSION, "房东")["status"] == "resolved"


def test_multiple_targets_and_stale_contact_require_clarification(client):
    save(client)
    save(client, alias="同学", contact_id="contact-chen")
    with db.db_session() as conn:
        assert aliases.resolve_transfer_alias(conn, SESSION, "转给房东和同学100元")["status"] == "ambiguous"
        assert aliases.resolve_transfer_alias(conn, SESSION, "转给房东和陌生人100元")["status"] == "ambiguous"
        conn.execute("UPDATE contacts SET account_ref='changed' WHERE id='contact-linyue'")
    with db.db_session() as conn:
        assert aliases.resolve_transfer_alias(conn, SESSION, "转给房东100元")["status"] == "stale"
    assert next(item for item in aliases.list_aliases(SESSION)["aliases"] if item["alias"] == "同学")["available"] is True
    assert next(item for item in aliases.list_aliases(SESSION)["aliases"] if item["alias"] == "房东")["available"] is False


def test_changed_contact_rejected_before_alias_creation(client):
    action = prepare(client).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE contacts SET account_ref='changed' WHERE id='contact-linyue'")
    assert confirm(client, action).status_code == 409


def test_similar_transfer_identity_uses_contact_id_not_same_name(client):
    for contact_id in ("contact-wang1", "contact-wang2"):
        action = direct_prepare_transfer(SESSION, contact_id, "50", "测试")["pending_action"]
        assert confirm(client, action).status_code == 200
    with db.db_session() as conn:
        matches = aliases.similar_transfers(conn, "contact-wang1", 5000)
        assert len(matches) == 1
        assert matches[0]["amount_yuan"] == "50.00"
        assert aliases.similar_transfers(conn, "contact-wang1", 5001) == []
        conn.execute("UPDATE demo_clock SET now='2026-10-03T09:00:00+08:00'")
    with db.db_session() as conn:
        assert aliases.similar_transfers(conn, "contact-wang1", 5000) == []


def test_chat_resolves_alias_to_verified_contact_and_preserves_confirmation(client):
    save(client)
    result = asyncio.run(process_message(SESSION, "转给房东100元"))
    assert result["pending_action"]["details"]["contact_id"] == "contact-linyue"
    assert result["pending_action"]["details"]["phone_masked"] == "138****1234"
    assert result["pending_action"]["status"] == "pending"


def test_note_does_not_create_memory_and_extras_rejected(client):
    action = direct_prepare_transfer(SESSION, "contact-linyue", "1", "房东是林悦")["pending_action"]
    assert confirm(client, action).status_code == 200
    assert aliases.list_aliases(SESSION)["aliases"] == []
    assert client.post("/api/aliases/prepare", json={"session_id": SESSION, "alias": "房东", "contact_id": "contact-linyue", "account_id": "other"}).status_code == 422
