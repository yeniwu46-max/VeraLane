"""AA authorization, allocation and ledger invariants for fictional collections."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
import sqlite3
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from app import db, main


SESSION = "aa-owner"
LIN = "contact-linyue"
CHEN = "contact-chen"
WANG = "contact-wang1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "aa.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    monkeypatch.setattr(main, "run_due_transfers", lambda: 0)
    with TestClient(main.app) as test_client:
        yield test_client


def payload(**changes):
    return {
        "session_id": SESSION, "total_yuan": "100.00",
        "contact_ids": [LIN, CHEN], "include_self": True, "note": "聚餐",
        **changes,
    }


def preview(client, **changes):
    response = client.post("/api/aa/preview", json=payload(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, **changes):
    response = client.post("/api/aa/prepare", json=payload(**changes))
    assert response.status_code == 200, response.text
    action = response.json()["pending_action"]
    assert action["type"] == "aa_collection"
    return action


def confirm(client, action, session=SESSION):
    return client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session})


def get_collection(client, collection_id, session=SESSION):
    response = client.get(f"/api/aa/collections/{collection_id}", params={"session_id": session})
    assert response.status_code == 200, response.text
    return response.json()


def authorize(client, **changes):
    action = prepare(client, **changes)
    response = confirm(client, action, changes.get("session_id", SESSION))
    assert response.status_code == 200, response.text
    return action, get_collection(client, response.json()["collection_id"], changes.get("session_id", SESSION))


def money(value):
    return int(Decimal(value) * 100)


def ledger():
    with db.db_session() as conn:
        balance = conn.execute("SELECT balance_cents FROM accounts WHERE id = ?", (db.ACCOUNT_ID,)).fetchone()[0]
        transactions = [dict(row) for row in conn.execute(
            "SELECT * FROM transactions WHERE account_id = ? ORDER BY id", (db.ACCOUNT_ID,),
        ).fetchall()]
    return balance, transactions


def requests(collection):
    return [row for row in collection["participants"] if row.get("request_id")]


def payment(client, request, session=SESSION, **extra):
    return client.post(f"/api/aa/requests/{request['request_id']}/simulate-payment", json={
        "session_id": session, **extra,
    })


def close(client, collection, session=SESSION):
    return client.post(f"/api/aa/collections/{collection['id']}/close", json={"session_id": session})


def interpret(client, message, endpoint="/api/aa/interpret", session=SESSION):
    response = client.post(endpoint, json={"session_id": session, "message": message})
    assert response.status_code == 200, response.text
    value = response.json()
    assert "pending_action" not in value
    return value["aa_draft"]


@pytest.mark.parametrize(("total", "contacts", "expected"), [
    ("100", [LIN, CHEN], [3334, 3333, 3333]),
    ("368.50", [LIN, WANG, CHEN], [9213, 9213, 9212, 9212]),
    ("0.01", [LIN, CHEN], [1, 0, 0]),
])
def test_preview_allocation_conserves_cents_and_has_stable_remainders(client, total, contacts, expected):
    before = ledger()
    plan = preview(client, total_yuan=total, contact_ids=contacts)
    assert [row["id"] for row in plan["participants"]] == ["self", *contacts]
    assert [money(row["amount_yuan"]) for row in plan["participants"]] == expected
    assert sum(expected) == money(plan["total_yuan"])
    assert money(plan["self_yuan"]) == expected[0]
    assert money(plan["receivable_yuan"]) == sum(expected[1:])
    assert preview(client, total_yuan=total, contact_ids=contacts) == plan
    assert ledger() == before
    assert client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"] == []


def test_manual_adjustment_accepts_only_complete_balanced_cents(client):
    plan = preview(client, shares_yuan={"self": "20.00", LIN: "30.00", CHEN: "50.00"})
    assert [row["amount_yuan"] for row in plan["participants"]] == ["20.00", "30.00", "50.00"]
    assert plan["receivable_yuan"] == "80.00"


@pytest.mark.parametrize("shares", [
    {"self": "20", LIN: "30", CHEN: "49.99"},
    {"self": "20", LIN: "30"},
    {"self": "20", LIN: "30", CHEN: "50", WANG: "0"},
    {"self": "-1", LIN: "51", CHEN: "50"},
    {"self": "0.001", LIN: "49.999", CHEN: "50"},
    {"self": "NaN", LIN: "50", CHEN: "50"},
])
def test_invalid_manual_shares_cannot_prepare_a_confirmable_action(client, shares):
    before = ledger()
    response = client.post("/api/aa/prepare", json=payload(shares_yuan=shares))
    assert response.status_code in (400, 409, 422), response.text
    assert ledger() == before
    assert client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"] == []


@pytest.mark.parametrize("changes", [
    {"contact_ids": [LIN, LIN]}, {"contact_ids": []},
    {"contact_ids": ["unknown"]}, {"include_self": False},
    {"total_yuan": "0"}, {"total_yuan": "-1"},
    {"total_yuan": "1.001"}, {"total_yuan": "NaN"},
    {"total_yuan": "Infinity"},
])
def test_invalid_members_or_amounts_never_create_collectable_requests(client, changes):
    before = ledger()
    response = client.post("/api/aa/prepare", json=payload(**changes))
    assert response.status_code in (400, 404, 409, 422), response.text
    assert ledger() == before


@pytest.mark.parametrize(("field", "value"), [("verified", 0), ("user_id", "another-user")])
def test_only_current_users_verified_contacts_can_participate(client, field, value):
    with db.db_session() as conn:
        conn.execute(f"UPDATE contacts SET {field} = ? WHERE id = ?", (value, LIN))
    response = client.post("/api/aa/prepare", json=payload())
    assert response.status_code in (400, 404, 409, 422)


def test_confirm_creates_once_without_debit_and_persists_across_initialization(client):
    before = ledger()
    action = prepare(client)
    assert ledger() == before
    assert client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"] == []
    first = confirm(client, action)
    repeated = confirm(client, action)
    assert first.status_code == repeated.status_code == 200
    assert first.json()["collection_id"] == repeated.json()["collection_id"]
    collection_id = first.json()["collection_id"]
    collection = get_collection(client, collection_id)
    assert collection["status"] == "pending"
    assert collection["received_yuan"] == "0.00"
    assert collection["outstanding_yuan"] == "66.66"
    assert len(requests(collection)) == 2
    assert ledger() == before
    db.init_db()
    assert get_collection(client, collection_id) == collection
    assert len(client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"]) == 1
    assert ledger() == before


def test_expired_or_other_session_consent_cannot_create_collection(client):
    action = prepare(client)
    assert confirm(client, action, "intruder").status_code == 404
    with db.db_session() as conn:
        conn.execute("UPDATE actions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (action["id"],))
    assert confirm(client, action).status_code == 409
    assert client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"] == []
    assert ledger()[0] == 888830


def test_collection_read_close_and_payment_are_bound_to_authorizing_session(client):
    action, collection = authorize(client)
    assert confirm(client, action, "intruder").status_code == 404
    assert client.get("/api/aa/collections", params={"session_id": "intruder"}).json()["collections"] == []
    assert client.get(f"/api/aa/collections/{collection['id']}", params={"session_id": "intruder"}).status_code == 404
    assert close(client, collection, "intruder").status_code == 404
    assert payment(client, requests(collection)[0], "intruder").status_code == 404
    assert get_collection(client, collection["id"])["status"] == "pending"
    assert ledger()[0] == 888830


def test_zero_shares_have_no_payment_requests_and_do_not_wait_for_payment(client):
    _, collection = authorize(client, total_yuan="0.01")
    assert collection["status"] == "completed"
    assert collection["receivable_yuan"] == collection["outstanding_yuan"] == "0.00"
    assert requests(collection) == []
    assert [row["status"] for row in collection["participants"]] == ["self", "not_required", "not_required"]
    assert ledger()[0] == 888830


def test_source_transaction_amount_is_server_bound_and_creation_does_not_duplicate_expense(client):
    before = ledger()
    report_before = client.get("/api/bills").json()
    forged = client.post("/api/aa/preview", json=payload(total_yuan="1.00", source_transaction_id="tx-1"))
    if forged.status_code == 200:
        assert forged.json()["total_yuan"] == "45.90"
    else:
        assert forged.status_code in (400, 409, 422)
    plan = preview(client, total_yuan="45.90", source_transaction_id="tx-1")
    assert plan["source_transaction"]["id"] == "tx-1"
    _, collection = authorize(client, total_yuan="45.90", source_transaction_id="tx-1")
    assert ledger() == before
    paid = payment(client, requests(collection)[0])
    assert paid.status_code == 200, paid.text
    assert ledger()[0] == before[0] + 1530
    assert client.get("/api/bills").json() == report_before


@pytest.mark.parametrize("source", ["unknown", "tx-7"])
def test_missing_or_incoming_transactions_cannot_be_used_as_paid_expense(client, source):
    response = client.post("/api/aa/prepare", json=payload(source_transaction_id=source))
    assert response.status_code in (400, 404, 409, 422)


def test_another_accounts_transaction_cannot_be_used_as_collection_source(client):
    with db.db_session() as conn:
        conn.execute("INSERT INTO accounts VALUES ('foreign-account', 'other-user', '其他账户', 10000)")
        conn.execute("""INSERT INTO transactions
            (id, account_id, posted_on, direction, amount_cents, counterparty, category, note)
            VALUES ('foreign-expense', 'foreign-account', '2026-09-30', 'out', 10000, '商家', '餐饮', '')""")
    response = client.post("/api/aa/prepare", json=payload(source_transaction_id="foreign-expense"))
    assert response.status_code in (400, 404, 409, 422)


def test_source_amount_changes_after_preview_require_new_consent(client):
    action = prepare(client, total_yuan="45.90", source_transaction_id="tx-1")
    with db.db_session() as conn:
        conn.execute("UPDATE transactions SET amount_cents = 9999 WHERE id = 'tx-1'")
    assert confirm(client, action).status_code == 409
    assert client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"] == []


def test_collection_creation_does_not_spend_daily_outgoing_limit(client):
    authorize(client, total_yuan="3000", contact_ids=[LIN])
    transfer = client.post("/api/transfers/prepare", json={
        "session_id": SESSION, "contact_id": LIN, "amount_yuan": "1000", "note": "正常转账",
    })
    assert transfer.status_code == 200
    action = transfer.json()["pending_action"]
    assert action["tier"] == "yellow"
    assert confirm(client, action).status_code == 200
    assert ledger()[0] == 788830


def test_duplicate_source_requires_closed_unpaid_collection_before_rebuilding(client):
    _, first = authorize(client, total_yuan="45.90", source_transaction_id="tx-1")
    duplicate = client.post("/api/aa/prepare", json=payload(total_yuan="45.90", source_transaction_id="tx-1"))
    assert duplicate.status_code == 409
    assert close(client, first).status_code == 200
    _, replacement = authorize(client, total_yuan="45.90", source_transaction_id="tx-1")
    assert replacement["id"] != first["id"]
    assert payment(client, requests(replacement)[0]).status_code == 200
    assert close(client, replacement).status_code == 200
    duplicate = client.post("/api/aa/prepare", json=payload(total_yuan="45.90", source_transaction_id="tx-1"))
    assert duplicate.status_code == 409


def test_source_is_rechecked_at_confirmation_when_two_previews_compete(client):
    first = prepare(client, total_yuan="45.90", source_transaction_id="tx-1")
    second = prepare(client, total_yuan="45.90", source_transaction_id="tx-1")
    assert confirm(client, first).status_code == 200
    assert confirm(client, second).status_code == 409
    assert len(client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"]) == 1


def test_concurrent_payment_and_replay_after_restart_credit_exactly_once(client):
    _, collection = authorize(client)
    request = requests(collection)[0]
    before_balance, before_rows = ledger()
    ready = Barrier(2)

    def pay():
        ready.wait(timeout=5)
        return payment(client, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(pay) for _ in range(2)]
        responses = [result.result(timeout=10) for result in results]
    assert all(response.status_code == 200 for response in responses)
    after_balance, after_rows = ledger()
    assert after_balance == before_balance + money(request["amount_yuan"])
    added = [row for row in after_rows if row not in before_rows]
    assert len(added) == 1
    assert added[0]["direction"] == "in"
    assert added[0]["amount_cents"] == money(request["amount_yuan"])
    updated = get_collection(client, collection["id"])
    assert updated["status"] == "partial"
    paid = next(row for row in requests(updated) if row["request_id"] == request["request_id"])
    assert paid["status"] == "paid"
    assert paid["transaction_id"] == added[0]["id"]
    assert paid["paid_at"]
    db.init_db()
    assert payment(client, request).status_code == 200
    assert ledger() == (after_balance, after_rows)


def test_aa_request_supports_idempotent_installments_then_pays_only_the_remainder(client):
    _, collection = authorize(client, total_yuan="100.00", contact_ids=[LIN])
    request = requests(collection)[0]
    balance_before = ledger()[0]
    url = f"/api/aa/requests/{request['request_id']}/installments"
    body = {"session_id": SESSION, "amount_yuan": "10.00", "idempotency_key": "installment-test-1"}

    first = client.post(url, json=body)
    retry = client.post(url, json=body)
    assert first.status_code == retry.status_code == 200
    result = first.json()
    person = next(row for row in result["participants"] if row["request_id"] == request["request_id"])
    assert person["status"] == "partial"
    assert person["received_yuan"] == "10.00"
    assert person["outstanding_yuan"] == "40.00"
    assert retry.json()["participants"] == result["participants"]
    assert ledger()[0] == balance_before + 1000

    paid = payment(client, request)
    assert paid.status_code == 200, paid.text
    settled = next(row for row in paid.json()["participants"] if row["request_id"] == request["request_id"])
    assert settled["status"] == "paid"
    assert settled["received_yuan"] == settled["amount_yuan"] == "50.00"
    assert settled["outstanding_yuan"] == "0.00"
    assert len(settled["payments"]) == 2
    assert ledger()[0] == balance_before + 5000


def test_aa_installment_rejects_overpayment_and_idempotency_key_reuse(client):
    _, collection = authorize(client, total_yuan="100.00", contact_ids=[LIN])
    request = requests(collection)[0]
    url = f"/api/aa/requests/{request['request_id']}/installments"
    body = {"session_id": SESSION, "amount_yuan": "49.99", "idempotency_key": "same-key"}
    first = client.post(url, json=body)
    assert first.status_code == 200
    balance_after = ledger()[0]
    assert client.post(url, json=body).json()["participants"] == first.json()["participants"]
    assert client.post(url, json={**body, "amount_yuan": "49.98"}).status_code == 409
    assert client.post(url, json={**body, "amount_yuan": "0.02", "idempotency_key": "overpay"}).status_code == 409
    assert ledger()[0] == balance_after


def test_full_receipt_matches_sum_of_unique_income_transactions(client):
    _, collection = authorize(client)
    before_balance, before_rows = ledger()
    for request in requests(collection):
        result = payment(client, request)
        assert result.status_code == 200, result.text
    final = get_collection(client, collection["id"])
    after_balance, after_rows = ledger()
    added = [row for row in after_rows if row not in before_rows]
    assert final["status"] == "completed"
    assert final["received_yuan"] == final["receivable_yuan"] == "66.66"
    assert final["outstanding_yuan"] == "0.00"
    assert sum(row["amount_cents"] for row in added) == after_balance - before_balance == 6666
    assert all(row["direction"] == "in" for row in added)


def test_payment_updates_roll_back_together_on_storage_interruption(client):
    _, collection = authorize(client)
    before = ledger()
    with db.db_session() as conn:
        conn.execute("""CREATE TRIGGER fail_aa_income AFTER INSERT ON transactions
            WHEN NEW.direction = 'in'
            BEGIN SELECT RAISE(ABORT, 'injected AA ledger failure'); END""")
    with pytest.raises(sqlite3.DatabaseError, match="injected AA ledger failure"):
        payment(client, requests(collection)[0])
    assert ledger() == before
    assert get_collection(client, collection["id"]) == collection
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM aa_payment_events").fetchone()[0] == 0
        conn.execute("DROP TRIGGER fail_aa_income")
    db.init_db()
    assert payment(client, requests(collection)[0]).status_code == 200
    assert ledger()[0] == before[0] + 3333


def test_closing_partially_paid_collection_preserves_receipt_and_stops_remaining_requests(client):
    _, collection = authorize(client)
    first, second = requests(collection)
    assert payment(client, first).status_code == 200
    before_close = ledger()
    for _ in range(2):
        result = close(client, collection)
        assert result.status_code == 200
        final = result.json()
        assert final["status"] == "closed"
        assert final["received_yuan"] == final["outstanding_yuan"] == "33.33"
    assert payment(client, second).status_code == 409
    assert payment(client, first).status_code == 200
    assert ledger() == before_close


def test_close_and_payment_race_preserves_a_consistent_terminal_collection(client):
    _, collection = authorize(client)
    first, second = requests(collection)
    before_balance, before_rows = ledger()
    ready = Barrier(2)

    def race_close():
        ready.wait(timeout=5)
        return close(client, collection)

    def race_pay():
        ready.wait(timeout=5)
        return payment(client, first)

    with ThreadPoolExecutor(max_workers=2) as pool:
        close_future, pay_future = pool.submit(race_close), pool.submit(race_pay)
        closed, paid = close_future.result(timeout=10), pay_future.result(timeout=10)
    assert closed.status_code == 200
    assert paid.status_code in (200, 409)
    final = get_collection(client, collection["id"])
    assert final["status"] == "closed"
    after_balance, after_rows = ledger()
    received = money(final["received_yuan"])
    assert received in (0, 3333)
    assert after_balance == before_balance + received
    assert len(after_rows) - len(before_rows) == (1 if received else 0)
    assert money(final["outstanding_yuan"]) == 6666 - received
    assert payment(client, second).status_code == 409


@pytest.mark.parametrize("extra", [{"amount_yuan": "9999"}, {"amount_cents": 999999}, {"account_id": "other"}])
def test_simulation_cannot_accept_caller_controlled_amount_or_account(client, extra):
    _, collection = authorize(client)
    before = ledger()
    assert payment(client, requests(collection)[0], **extra).status_code == 422
    assert ledger() == before


def test_disabled_demo_control_blocks_simulated_money_while_reading_remains_available(client, monkeypatch):
    _, collection = authorize(client)
    before = ledger()
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "0")
    assert payment(client, requests(collection)[0]).status_code == 403
    listing = client.get("/api/aa/collections", params={"session_id": SESSION}).json()
    assert listing["demo_controls_enabled"] is False
    assert len(listing["collections"]) == 1
    assert ledger() == before


def test_receipt_uses_shared_business_clock_without_changing_existing_expense_report(client):
    _, collection = authorize(client)
    with db.db_session() as conn:
        conn.execute("UPDATE demo_clock SET now = '2026-10-05T20:15:00+08:00' WHERE id = 1")
    response = payment(client, requests(collection)[0])
    assert response.status_code == 200
    paid = next(row for row in requests(response.json()) if row["status"] == "paid")
    assert paid["paid_at"].startswith("2026-10-05T20:15")
    with db.db_session() as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE id = ?", (paid["transaction_id"],)).fetchone()
    assert tx["posted_on"] == "2026-10-05"
    assert tx["direction"] == "in"


@pytest.mark.parametrize("endpoint", ["/api/aa/interpret", "/api/chat"])
def test_multi_turn_phone_disambiguation_preserves_every_other_member(client, endpoint):
    before = ledger()
    first = interpret(client, "聚餐我垫了368.50元，我、林悦、王明、陈晨四个人AA", endpoint)
    assert first["total_yuan"] == "368.50"
    assert first["include_self"] is True
    assert first["participant_count"] == 4
    assert [row["name"] for row in first["participants"]] == ["林悦", "王明", "陈晨"]
    assert first["participants"][1]["contact_id"] is None
    assert len(first["participants"][1]["choices"]) == 2
    resolved = interpret(client, "13900002222", endpoint)
    assert [row["contact_id"] for row in resolved["participants"]] == [LIN, "contact-wang2", CHEN]
    assert resolved["total_yuan"] == "368.50"
    assert resolved["note"] == "聚餐"
    assert resolved["needs_review"] == []
    assert ledger() == before
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM aa_requests").fetchone()[0] == 0


def test_explicit_named_weights_prefill_verified_contact_ratio_ids(client):
    draft = interpret(client, "聚餐我垫了120元，我一份，林悦两份，陈晨一份AA")
    assert draft["suggested_share_ratios"] == {"self": 1, LIN: 2, CHEN: 1}
    assert draft["include_self"] is True
    assert draft["payer_is_self"] is True
    assert draft["requires_custom_shares"] is True
    assert draft["needs_review"]
    plan = preview(client, total_yuan="120", shares_ratio=draft["suggested_share_ratios"])
    assert plan["allocation_method"] == "proportional"
    assert plan["share_ratios"] == {"self": 1, LIN: 2, CHEN: 1}
    assert [row["amount_yuan"] for row in plan["participants"]] == ["30.00", "60.00", "30.00"]


def test_ambiguous_contact_name_never_gets_an_automatic_weight(client):
    draft = interpret(client, "我垫了120元，我一份，王明两份，林悦一份AA")
    assert draft["suggested_share_ratios"] is None
    assert any("联系人" in issue and "份数" in issue for issue in draft["needs_review"])


def test_complete_percentages_prefill_ratios_and_incomplete_total_is_rejected(client):
    draft = interpret(client, "我垫了100元，我承担20%，林悦30%，陈晨50%AA")
    assert draft["suggested_share_ratios"] == {"self": 20, LIN: 30, CHEN: 50}
    plan = preview(client, total_yuan="100", shares_ratio=draft["suggested_share_ratios"])
    assert [row["amount_yuan"] for row in plan["participants"]] == ["20.00", "30.00", "50.00"]

    incomplete = interpret(client, "我垫了100元，我承担20%，林悦30%，陈晨40%AA", session="incomplete-percentages")
    assert incomplete["suggested_share_ratios"] is None
    assert any("恰好为 100%" in issue for issue in incomplete["needs_review"])

    mixed = interpret(client, "我垫了100元，我一份，林悦30%，陈晨一份AA", session="mixed-share-units")
    assert mixed["suggested_share_ratios"] is None
    assert any("百分比必须全部使用百分比格式" in issue for issue in mixed["needs_review"])


@pytest.mark.parametrize("endpoint", ["/api/aa/interpret", "/api/chat"])
def test_follow_up_note_cannot_supply_missing_amount_members_or_self_exclusion(client, endpoint):
    first = interpret(client, "我和林悦AA", endpoint)
    assert first["total_yuan"] is None
    memo = interpret(client, "备注陈晨300元，不包括我", endpoint)
    assert memo["total_yuan"] is None
    assert memo["include_self"] is True
    assert [row["contact_id"] for row in memo["participants"]] == [LIN]
    resolved = interpret(client, "100元", endpoint)
    assert resolved["total_yuan"] == "100.00"
    assert resolved["needs_review"] == []
    assert [row["contact_id"] for row in resolved["participants"]] == [LIN]
    assert ledger()[0] == 888830


@pytest.mark.parametrize("endpoint", ["/api/aa/interpret", "/api/chat"])
def test_invalid_amount_correction_cannot_silently_recover_earlier_aa_amount(client, endpoint):
    first = interpret(client, "我和林悦AA100元", endpoint)
    assert first["total_yuan"] == "100.00"
    invalid = interpret(client, "1,000元", endpoint)
    assert invalid["total_yuan"] is None
    still_missing = interpret(client, "包括我", endpoint)
    assert still_missing["total_yuan"] is None
    assert still_missing["needs_review"]
    resolved = interpret(client, "200元", endpoint)
    assert resolved["total_yuan"] == "200.00"
    assert [row["contact_id"] for row in resolved["participants"]] == [LIN]
    assert resolved["needs_review"] == []


def test_unknown_people_are_retained_and_paying_does_not_imply_sharing(client):
    unknown = interpret(client, "我和林悦、张三AA100元")
    assert [row["name"] for row in unknown["participants"]] == ["林悦", "张三"]
    assert unknown["participants"][1]["contact_id"] is None
    assert any("张三" in issue for issue in unknown["needs_review"])
    first = interpret(client, "我垫付100元，林悦、陈晨AA", session="payer-clarification")
    assert first["include_self"] is None
    assert first["needs_review"]
    resolved = interpret(client, "包括我", session="payer-clarification")
    assert resolved["include_self"] is True
    assert [row["contact_id"] for row in resolved["participants"]] == [LIN, CHEN]
    assert resolved["needs_review"] == []


def test_unfinished_aa_context_is_not_shared_across_sessions_or_other_tasks(client):
    interpret(client, "我和林悦AA", endpoint="/api/chat")
    intruder = client.post("/api/chat", json={"session_id": "intruder", "message": "100元"})
    assert intruder.status_code == 200
    assert "aa_draft" not in intruder.json()
    assert "pending_action" not in intruder.json()
    balance = client.post("/api/chat", json={"session_id": SESSION, "message": "查余额"})
    assert balance.status_code == 200
    follow_up = client.post("/api/chat", json={"session_id": SESSION, "message": "100元"})
    assert follow_up.status_code == 200
    assert "aa_draft" not in follow_up.json()
    assert "pending_action" not in follow_up.json()


@pytest.mark.parametrize("tamper", ["collection_snapshot", "request_amount", "request_member"])
def test_payment_rejects_changed_authorized_snapshot_or_request_binding(client, tamper):
    _, collection = authorize(client)
    request = requests(collection)[0]
    before = ledger()
    with db.db_session() as conn:
        if tamper == "collection_snapshot":
            saved = json.loads(conn.execute("SELECT payload_json FROM aa_collections WHERE id = ?", (collection["id"],)).fetchone()[0])
            saved["note"] = "Changed after consent"
            conn.execute("UPDATE aa_collections SET payload_json = ? WHERE id = ?", (json.dumps(saved), collection["id"]))
        elif tamper == "request_amount":
            conn.execute("UPDATE aa_requests SET amount_cents = amount_cents + 1 WHERE id = ?", (request["request_id"],))
        else:
            conn.execute("UPDATE aa_requests SET contact_id = ? WHERE id = ?", (WANG, request["request_id"]))
    response = payment(client, request)
    assert response.status_code == 409, response.text
    assert ledger() == before
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM aa_payment_events").fetchone()[0] == 0


def test_aa_collection_reminder_uses_demo_clock_deduplicates_and_tracks_live_status(client):
    action = prepare(client)
    assert action["details"]["reminder_on"] == "2026-10-03"
    created = confirm(client, action).json()
    collection = get_collection(client, created["collection_id"])
    request = requests(collection)[0]

    events = client.get("/api/demo/events", params={"session_id": SESSION}).json()
    aa_event = next(event for event in events["events"] if event["label"] == "AA 未收款站内提醒")
    assert aa_event["at"] == "2026-10-03T09:00:00+08:00"
    assert not any(item["kind"] == "aa_collection" for item in client.get(
        "/api/reminders", params={"session_id": SESSION}).json()["items"])

    advanced = client.post("/api/demo/clock/advance-next", json={"session_id": SESSION})
    assert advanced.status_code == 200, advanced.text
    reminders = client.get("/api/reminders", params={"session_id": SESSION}).json()["items"]
    reminder = next(item for item in reminders if item["kind"] == "aa_collection")
    assert reminder["due_on"] == "2026-10-03"
    assert reminder["source_status"] == "pending"
    assert "不发送消息" in reminder["body"]
    assert client.get("/api/demo/events", params={"session_id": SESSION}).json()["events"] == []

    # Repeated refreshes keep one reminder; it continues to reflect current collection state.
    refreshed = client.get("/api/reminders", params={"session_id": SESSION}).json()["items"]
    assert [item["id"] for item in refreshed].count(reminder["id"]) == 1
    response = client.post(f"/api/aa/requests/{request['request_id']}/installments", json={
        "session_id": SESSION, "amount_yuan": "10.00", "idempotency_key": "reminder-partial-1",
    })
    assert response.status_code == 200, response.text
    live = next(item for item in client.get("/api/reminders", params={"session_id": SESSION}).json()["items"] if item["id"] == reminder["id"])
    assert live["source_status"] == "partial"

    assert client.post(f"/api/reminders/{reminder['id']}/read", json={"session_id": SESSION}).status_code == 200
    read = next(item for item in client.get("/api/reminders", params={"session_id": SESSION}).json()["items"] if item["id"] == reminder["id"])
    assert read["read_at"]


def test_aa_reminder_is_not_scheduled_or_created_after_full_payment(client):
    _, collection = authorize(client)
    requests_to_pay = requests(collection)
    for request in requests_to_pay:
        result = payment(client, request)
        assert result.status_code == 200, result.text
    assert not any(event["label"] == "AA 未收款站内提醒" for event in client.get(
        "/api/demo/events", params={"session_id": SESSION}).json()["events"])
    client.post("/api/demo/clock/advance-next", json={"session_id": SESSION})
    assert not any(item["kind"] == "aa_collection" for item in client.get(
        "/api/reminders", params={"session_id": SESSION}).json()["items"])


def test_legacy_open_collection_gets_a_reminder_date_during_schema_upgrade(client):
    _, collection = authorize(client)
    with db.db_session() as conn:
        conn.execute("UPDATE aa_collections SET reminder_on=NULL WHERE id=?", (collection["id"],))

    db.init_db()
    with db.db_session() as conn:
        reminder_on = conn.execute("SELECT reminder_on FROM aa_collections WHERE id=?", (collection["id"],)).fetchone()[0]
    assert reminder_on == "2026-10-03"


def test_prepared_aa_reminder_date_is_reauthorized_if_demo_clock_moves(client):
    action = prepare(client)
    with db.db_session() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-10-01T09:00:00+08:00' WHERE id=1")
    response = confirm(client, action)
    assert response.status_code == 409
    assert "重新核对站内提醒日期" in response.json()["detail"]
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM aa_collections").fetchone()[0] == 0


def test_proportional_aa_shares_use_largest_remainder_cents_and_authorized_snapshot(client):
    ratio = {"self": 1, LIN: 2, CHEN: 3}
    response = client.post("/api/aa/preview", json=payload(total_yuan="100.01", shares_ratio=ratio))
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["allocation_method"] == "proportional"
    assert preview["share_ratios"] == ratio
    assert [person["amount_yuan"] for person in preview["participants"]] == ["16.67", "33.34", "50.00"]
    assert sum(int(Decimal(person["amount_yuan"]) * 100) for person in preview["participants"]) == 10001

    action = client.post("/api/aa/prepare", json=payload(total_yuan="100.01", shares_ratio=ratio)).json()["pending_action"]
    assert action["details"]["share_ratios"] == ratio
    assert action["details"]["allocation_method"] == "proportional"
    confirmed = confirm(client, action)
    assert confirmed.status_code == 200, confirmed.text
    collection = get_collection(client, confirmed.json()["collection_id"])
    assert [person["amount_yuan"] for person in collection["participants"]] == ["16.67", "33.34", "50.00"]


@pytest.mark.parametrize("changes", [
    {"shares_ratio": {"self": 1, LIN: 2, "unknown": 3}},
    {"shares_ratio": {"self": 0, LIN: 0, CHEN: 0}},
    {"shares_ratio": {"self": -1, LIN: 2, CHEN: 3}},
    {"shares_ratio": {"self": 1.5, LIN: 2, CHEN: 3}},
    {"shares_ratio": {"self": 1, LIN: 2, CHEN: 3}, "shares_yuan": {"self": "20", LIN: "30", CHEN: "50"}},
])
def test_invalid_aa_ratio_plans_are_rejected_without_persisting_actions(client, changes):
    response = client.post("/api/aa/prepare", json=payload(**changes))
    assert response.status_code in (400, 422), response.text
    assert client.get("/api/aa/collections", params={"session_id": SESSION}).json()["collections"] == []
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions WHERE type='aa_collection'").fetchone()[0] == 0


def test_aa_receipt_refund_is_authorized_bounded_and_kept_as_a_separate_ledger_entry(client):
    _, collection = authorize(client, total_yuan="100.00", contact_ids=[LIN])
    request = requests(collection)[0]
    paid = payment(client, request).json()
    person = next(row for row in paid["participants"] if row["request_id"] == request["request_id"])
    original_receipt = person["payments"][0]
    balance_before_refund = ledger()[0]

    prepared = client.post(f"/api/aa/requests/{request['request_id']}/refund/prepare", json={
        "session_id": SESSION, "source_transaction_id": original_receipt["transaction_id"], "amount_yuan": "20.00",
    })
    assert prepared.status_code == 200, prepared.text
    action = prepared.json()["pending_action"]
    assert action["type"] == "aa_refund" and action["tier"] == "yellow"
    assert action["details"]["recipient_name"] == "林悦"
    assert action["details"]["remaining_refundable_yuan"] == "50.00"

    result = confirm(client, action)
    assert result.status_code == 200, result.text
    receipt = result.json()
    assert receipt["status"] == "completed" and receipt["amount_yuan"] == "20.00"
    assert confirm(client, action).json() == receipt
    assert ledger()[0] == balance_before_refund - 2000

    updated = get_collection(client, collection["id"])
    assert updated["received_yuan"] == "50.00"  # gross receipt history remains intact
    assert updated["refunded_yuan"] == "20.00"
    assert updated["net_received_yuan"] == "30.00"
    assert updated["net_advance_yuan"] == "70.00"
    person_after = next(row for row in updated["participants"] if row["request_id"] == request["request_id"])
    assert person_after["status"] == "paid"
    assert person_after["payments"][0]["refunded_yuan"] == "20.00"
    assert person_after["payments"][0]["net_received_yuan"] == "30.00"
    assert person_after["payments"][0]["refunds"][0]["transaction_id"] == receipt["transaction_id"]

    over = client.post(f"/api/aa/requests/{request['request_id']}/refund/prepare", json={
        "session_id": SESSION, "source_transaction_id": original_receipt["transaction_id"], "amount_yuan": "30.01",
    })
    assert over.status_code == 409, over.text


def test_aa_refund_rejects_unrelated_receipts_and_cross_session_requests(client):
    _, collection = authorize(client, total_yuan="100.00", contact_ids=[LIN])
    request = requests(collection)[0]
    paid = payment(client, request).json()
    participant = next(row for row in paid["participants"] if row["request_id"] == request["request_id"])
    receipt_id = participant["payments"][0]["transaction_id"]
    url = f"/api/aa/requests/{request['request_id']}/refund/prepare"
    body = {"session_id": SESSION, "source_transaction_id": receipt_id, "amount_yuan": "1.00"}
    assert client.post(url, json={**body, "source_transaction_id": "tx-1"}).status_code == 409
    assert client.post(url, json={**body, "session_id": "intruder"}).status_code == 404
    assert client.get("/api/aa/collections", params={"session_id": "intruder"}).json()["collections"] == []


def test_aa_refund_rechecks_competing_actions_and_available_reserved_funds(client):
    _, collection = authorize(client, total_yuan="100.00", contact_ids=[LIN])
    request = requests(collection)[0]
    paid = payment(client, request).json()
    participant = next(row for row in paid["participants"] if row["request_id"] == request["request_id"])
    source_id = participant["payments"][0]["transaction_id"]
    url = f"/api/aa/requests/{request['request_id']}/refund/prepare"
    body = {"session_id": SESSION, "source_transaction_id": source_id, "amount_yuan": "30.00"}
    first = client.post(url, json=body).json()["pending_action"]
    competing = client.post(url, json=body).json()["pending_action"]
    balance_before = ledger()[0]
    assert confirm(client, first).status_code == 200
    rejected = confirm(client, competing)
    assert rejected.status_code == 409
    assert ledger()[0] == balance_before - 3000

    with db.db_session() as conn:
        balance = conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()[0]
        reserved = balance - 50
        conn.execute("INSERT INTO fund_reservations(id,session_id,account_id,purpose,amount_cents,remaining_cents,status,created_at,updated_at) "
                     "VALUES('refund-reserve',?,?,?, ?,?,'active','now','now')",
                     (SESSION, db.ACCOUNT_ID, "测试预留", reserved, reserved))
    insufficient = client.post(url, json={**body, "amount_yuan": "1.00"})
    assert insufficient.status_code == 409
    assert "可用余额不足" in insufficient.json()["detail"]


def test_large_aa_refund_requires_separate_demo_verification(client):
    _, collection = authorize(client, total_yuan="3000.00", contact_ids=[LIN])
    request = requests(collection)[0]
    paid = payment(client, request).json()
    participant = next(row for row in paid["participants"] if row["request_id"] == request["request_id"])
    source_id = participant["payments"][0]["transaction_id"]
    prepared = client.post(f"/api/aa/requests/{request['request_id']}/refund/prepare", json={
        "session_id": SESSION, "source_transaction_id": source_id, "amount_yuan": "1500.00",
    }).json()["pending_action"]
    assert prepared["tier"] == "red"
    assert confirm(client, prepared).status_code == 403
    challenge = client.post(f"/api/actions/{prepared['id']}/challenge", json={"session_id": SESSION}).json()
    verified = client.post(f"/api/actions/{prepared['id']}/verify", json={
        "session_id": SESSION, "challenge_id": challenge["challenge_id"], "code": challenge["demo_code"],
    })
    assert verified.status_code == 200, verified.text
    assert confirm(client, prepared).status_code == 200


def test_itemized_preview_splits_each_line_in_cents_and_preserves_assignments(client):
    before = ledger()
    body = payload(total_yuan="30.00", contact_ids=[LIN, CHEN], itemized_items=[
        {"description": "共享披萨", "amount_yuan": "9.99", "participant_ids": [LIN, "self"]},
        {"description": "陈晨饮料", "amount_yuan": "20.01", "participant_ids": [CHEN]},
    ])
    response = client.post("/api/aa/preview", json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["allocation_method"] == "itemized"
    assert [money(row["amount_yuan"]) for row in plan["participants"]] == [500, 499, 2001]
    assert [row["participant_ids"] for row in plan["itemized_items"]] == [["self", LIN], [CHEN]]
    assert [[entry["amount_cents"] for entry in row["allocations"]] for row in plan["itemized_items"]] == [[500, 499], [2001]]
    assert ledger() == before


def test_itemized_plan_is_saved_only_after_confirmation_and_shown_in_collection(client):
    body = payload(total_yuan="10.01", contact_ids=[LIN], itemized_items=[
        {"description": "共享主食", "amount_yuan": "10.01", "participant_ids": [LIN, "self"]},
    ])
    before = ledger()
    prepared = client.post("/api/aa/prepare", json=body)
    assert prepared.status_code == 200, prepared.text
    action = prepared.json()["pending_action"]
    assert action["details"]["allocation_method"] == "itemized"
    assert confirm(client, action).status_code == 200
    collection = get_collection(client, action["id"])
    assert collection["allocation_method"] == "itemized"
    assert collection["itemized_items"][0]["description"] == "共享主食"
    assert sum(money(row["amount_yuan"]) for row in collection["participants"]) == 1001
    assert ledger()[0] == before[0]


@pytest.mark.parametrize("items", [
    [{"description": "菜品", "amount_yuan": "9.99", "participant_ids": ["self"]}],
    [{"description": "菜品", "amount_yuan": "10.00", "participant_ids": ["self", "self"]}],
    [{"description": "菜品", "amount_yuan": "10.00", "participant_ids": ["unverified"]}],
    [{"description": "菜品", "amount_yuan": "0.00", "participant_ids": ["self"]}],
    [{"description": "菜品", "amount_yuan": "10.001", "participant_ids": ["self"]}],
])
def test_itemized_preview_rejects_unbalanced_or_invalid_lines(client, items):
    response = client.post("/api/aa/preview", json=payload(total_yuan="10.00", itemized_items=items))
    assert response.status_code == 422, response.text
