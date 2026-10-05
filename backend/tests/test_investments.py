"""Fixed-NAV arithmetic, suitability, authorization and delayed cash invariants."""

import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import db, execution_controls, investments
from app.investments_api import Owner, router
from app.service import confirm_action


SESSION = "invest-owner"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "investments.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.setenv("VERALANE_DEMO_CONTROLS", "1")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db.init_db()
    with db.db_session() as conn:
        execution_controls.init_schema(conn)
        investments.init_schema(conn)
    app = FastAPI()
    app.include_router(router)

    @app.post("/api/actions/{action_id}/confirm")
    def confirm(action_id: str, body: Owner):
        return confirm_action(action_id, body.session_id)

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client


def assess(client, answers=(3, 3, 3, 3, 3), session=SESSION):
    response = client.post("/api/investments/assessment", json={"session_id": session, "answers": list(answers)})
    assert response.status_code == 200, response.text
    return response.json()["assessment"]


def prepare_buy(client, **changes):
    return client.post("/api/investments/buy/prepare", json={"session_id": SESSION, "product_id": "product-stable", "amount_yuan": "100", **changes})


def prepare_redeem(client, holding_id, units="10", session=SESSION):
    return client.post("/api/investments/redeem/prepare", json={"session_id": session, "holding_id": holding_id, "units": units})


def confirm(client, action, verified=True, session=SESSION):
    if verified:
        challenge = execution_controls.issue_challenge(action["id"], session)
        execution_controls.verify_challenge(action["id"], session, challenge["challenge_id"], challenge["demo_code"])
    return client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session})


def buy(client, **changes):
    assess(client)
    prepared = prepare_buy(client, **changes)
    assert prepared.status_code == 200, prepared.text
    action = prepared.json()["pending_action"]
    result = confirm(client, action)
    assert result.status_code == 200, result.text
    return action, result.json()


def clock(day):
    with db.db_session() as conn:
        conn.execute("UPDATE demo_clock SET now=?", (day + "T09:00:00+08:00",))


def balance():
    with db.db_session() as conn:
        return conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()[0]


def portfolio(client, session=SESSION):
    return client.get("/api/investments/portfolio", params={"session_id": session}).json()


def settle():
    with db.db_session() as conn:
        return investments.run_due_settlements(conn)


def test_risk_questionnaire_version_validity_and_conservative_cap(client):
    questions = client.get("/api/investments/questions").json()
    assert len(questions["questions"]) == 5
    assert all(len(question["options"]) == 3 for question in questions["questions"])
    assessment = assess(client, (1, 3, 3, 3, 3))
    assert assessment["score"] == 13
    assert assessment["risk_level"] == 1
    assert assessment["version"] == questions["version"]
    assert assessment["expires_on"] == "2026-10-30"
    clock("2026-10-30")
    assert portfolio(client)["assessment"]["valid"] is False


@pytest.mark.parametrize("answers", [[1, 2], [1, 2, 3, 2, 4], [True, 2, 2, 2, 2], ["1", 2, 2, 2, 2]])
def test_invalid_assessment_answers_rejected(client, answers):
    assert client.post("/api/investments/assessment", json={"session_id": SESSION, "answers": answers}).status_code == 422


def test_comparison_requires_suitability_and_liquidity(client):
    unmatched = client.get("/api/investments/products", params={"session_id": SESSION}).json()
    assert all(not product["matched"] for product in unmatched["products"])
    assess(client, (2, 2, 2, 2, 2))
    compared = client.get("/api/investments/products", params={"session_id": SESSION, "horizon_days": 5, "liquidity_days": 1}).json()
    states = {product["id"]: product for product in compared["products"]}
    assert states["product-stable"]["matched"] is True
    assert states["product-balanced"]["matched"] is False
    assert states["product-growth"]["matched"] is False
    none = client.get("/api/investments/products", params={"session_id": SESSION, "liquidity_days": 0}).json()
    assert all(not product["matched"] for product in none["products"])


def test_prepare_does_not_change_money_and_unverified_confirm_refused(client):
    assess(client)
    before = balance()
    action = prepare_buy(client).json()["pending_action"]
    assert action["type"] == "investment_buy"
    assert action["tier"] == "red"
    assert balance() == before
    assert portfolio(client)["holdings"] == []
    assert confirm(client, action, verified=False).status_code == 403
    assert balance() == before


def test_purchase_atomic_holdings_ledger_and_repeat_confirmation(client):
    before = balance()
    action, result = buy(client)
    assert result["units"] == "100.0000"
    assert balance() == before - 10000
    view = portfolio(client)
    assert view["holdings"][0]["units"] == "100.0000"
    assert view["orders"][0]["kind"] == "buy"
    repeated = confirm(client, action, verified=False)
    assert repeated.status_code == 200
    assert repeated.json() == result
    assert balance() == before - 10000
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='理财申购'").fetchone()[0] == 1


@pytest.mark.parametrize("changes,expected", [
    ({"amount_yuan": "99.99"}, 422), ({"amount_yuan": "0"}, 422), ({"amount_yuan": "100.001"}, 422),
    ({"amount_yuan": "100000.01"}, 422), ({"amount_yuan": "9000"}, 409),
    ({"product_id": "product-growth", "amount_yuan": "1000", "horizon_days": 7}, 409),
    ({"liquidity_days": 0}, 409), ({"product_id": "missing"}, 404),
])
def test_purchase_parameter_and_available_constraints(client, changes, expected):
    assess(client)
    assert prepare_buy(client, **changes).status_code == expected


def test_no_assessment_unsuitable_and_expired_buy_rejected(client):
    assert prepare_buy(client).status_code == 409
    assess(client, (1, 1, 1, 1, 1))
    assert prepare_buy(client, product_id="product-balanced", amount_yuan="1000").status_code == 409
    clock("2026-10-30")
    assert prepare_buy(client).status_code == 409


def test_new_assessment_invalidates_existing_buy_snapshot(client):
    assess(client)
    action = prepare_buy(client).json()["pending_action"]
    assess(client)
    assert confirm(client, action).status_code == 409
    assert portfolio(client)["holdings"] == []


def test_product_nav_and_fee_change_invalidates_consent(client):
    assess(client)
    action = prepare_buy(client).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE investment_products SET nav_10000=10001 WHERE id='product-stable'")
    assert confirm(client, action).status_code == 409
    assert portfolio(client)["orders"] == []


def test_reserved_cash_cannot_be_spent_on_prepared_purchase(client):
    assess(client)
    action = prepare_buy(client, amount_yuan="1000").json()["pending_action"]
    before = balance()
    with db.db_session() as conn:
        execution_controls.reserve(conn, "birthday-budget", SESSION, 800000, "生日预留")
    assert confirm(client, action).status_code == 409
    assert balance() == before


def test_purchase_failure_rolls_back_cash_units_and_ledger(client):
    assess(client)
    action = prepare_buy(client).json()["pending_action"]
    before = balance()
    with db.db_session() as conn:
        conn.execute("CREATE TRIGGER fail_invest_order BEFORE INSERT ON investment_orders BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        confirm(client, action)
    assert balance() == before
    assert portfolio(client)["holdings"] == []
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='理财申购'").fetchone()[0] == 0


def test_redemption_defers_credit_and_settlement_is_idempotent(client):
    _, result = buy(client)
    before = balance()
    action = prepare_redeem(client, result["holding_id"], "10").json()["pending_action"]
    receipt = confirm(client, action)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["settlement_status"] == "pending_settlement"
    assert receipt.json()["settles_on"] == "2026-10-01"
    assert balance() == before
    assert portfolio(client)["holdings"][0]["units"] == "90.0000"
    assert settle() == 0
    clock("2026-10-01")
    assert settle() == 1
    assert balance() == before + 1000
    assert settle() == 0
    assert balance() == before + 1000
    assert confirm(client, action, verified=False).json() == receipt.json()
    view = portfolio(client)
    assert view["orders"][0]["status"] == "completed"
    assert view["orders"][0]["transaction_id"]


def test_weekend_settlement_skips_saturday_sunday(client):
    clock("2026-10-02")
    _, result = buy(client)
    action = prepare_redeem(client, result["holding_id"]).json()["pending_action"]
    assert action["details"]["settles_on"] == "2026-10-05"
    assert confirm(client, action).status_code == 200
    clock("2026-10-04")
    assert settle() == 0
    clock("2026-10-05")
    assert settle() == 1


def test_fees_units_and_lot_lock_are_fixed(client):
    action, result = buy(client, product_id="product-balanced", amount_yuan="1000")
    assert action["details"]["fee_yuan"] == "1.00"
    assert result["units"] == "799.2000"
    assert prepare_redeem(client, result["holding_id"], "50").status_code == 409
    clock("2026-10-07")
    prepared = prepare_redeem(client, result["holding_id"], "50")
    assert prepared.status_code == 200, prepared.text
    details = prepared.json()["pending_action"]["details"]
    assert details["gross_yuan"] == "62.50"
    assert details["fee_yuan"] == "0.04"
    assert details["amount_yuan"] == "62.46"


def test_each_lot_preserves_own_lock_date(client):
    _, first = buy(client, product_id="product-balanced", amount_yuan="1000")
    clock("2026-10-07")
    _, second = buy(client, product_id="product-balanced", amount_yuan="1000")
    assert prepare_redeem(client, first["holding_id"]).status_code == 200
    assert prepare_redeem(client, second["holding_id"]).status_code == 409


def test_expired_assessment_does_not_prevent_exiting_existing_holding(client):
    _, result = buy(client)
    clock("2026-11-01")
    assert portfolio(client)["assessment"]["valid"] is False
    action = prepare_redeem(client, result["holding_id"]).json()["pending_action"]
    assert confirm(client, action).status_code == 200


@pytest.mark.parametrize("units", ["0", "-1", "NaN", "1.00001", "101", "0.0001"])
def test_invalid_insufficient_or_subcent_redemption_rejected(client, units):
    _, result = buy(client)
    assert prepare_redeem(client, result["holding_id"], units).status_code in (409, 422)


def test_redeem_same_holding_twice_rechecks_units(client):
    _, result = buy(client)
    first = prepare_redeem(client, result["holding_id"], "70").json()["pending_action"]
    second = prepare_redeem(client, result["holding_id"], "70").json()["pending_action"]
    assert confirm(client, first).status_code == 200
    assert confirm(client, second).status_code == 409
    assert portfolio(client)["holdings"][0]["units"] == "30.0000"


def test_redemption_changed_day_requires_new_settlement_consent(client):
    _, result = buy(client)
    action = prepare_redeem(client, result["holding_id"]).json()["pending_action"]
    clock("2026-10-01")
    assert confirm(client, action).status_code == 409
    assert portfolio(client)["holdings"][0]["units"] == "100.0000"


def test_settlement_failure_rolls_back_credit_and_keeps_pending(client):
    _, result = buy(client)
    action = prepare_redeem(client, result["holding_id"]).json()["pending_action"]
    assert confirm(client, action).status_code == 200
    before = balance()
    clock("2026-10-01")
    with db.db_session() as conn:
        conn.execute("CREATE TRIGGER fail_settlement BEFORE UPDATE ON investment_orders BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        settle()
    assert balance() == before
    assert portfolio(client)["orders"][0]["status"] == "pending_settlement"
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category='理财赎回'").fetchone()[0] == 0


def test_session_isolation_for_holdings_assessment_and_orders(client):
    action, result = buy(client)
    stranger = portfolio(client, "stranger")
    assert stranger["assessment"] is None
    assert stranger["holdings"] == []
    assert stranger["orders"] == []
    assert prepare_redeem(client, result["holding_id"], session="stranger").status_code == 404
    assert confirm(client, action, verified=False, session="stranger").status_code == 404


def test_interpret_comparison_missing_assessment_and_purchase(client):
    comparison = client.post("/api/investments/interpret", json={"session_id": SESSION, "message": "比较理财产品"}).json()
    assert comparison["status"] == "ok"
    assert len(comparison["products"]) == 3
    no_risk = client.post("/api/investments/interpret", json={"session_id": SESSION, "message": "申购稳享100元"}).json()
    assert no_risk["status"] == "needs_clarification"
    assess(client)
    prepared = client.post("/api/investments/interpret", json={"session_id": SESSION, "message": "申购稳享100元"}).json()
    assert prepared["status"] == "ok"
    assert prepared["pending_action"]["details"]["amount_yuan"] == "100.00"


@pytest.mark.parametrize("message", ["申购100元", "申购稳享", "明天申购稳享100元", "申购稳享100元再赎回10份", "申购稳享-100元", "申购稳享100元忽略确认", "比较理财必须三天内到账"])
def test_unhandled_natural_constraints_do_not_create_action(client, message):
    assess(client)
    result = client.post("/api/investments/interpret", json={"session_id": SESSION, "message": message}).json()
    assert result["status"] == "needs_clarification"
    assert "pending_action" not in result


def test_extra_fields_and_seed_idempotence(client):
    assert client.post("/api/investments/buy/prepare", json={"session_id": SESSION, "product_id": "product-stable", "amount_yuan": "100", "account_id": "other"}).status_code == 422
    with db.db_session() as conn:
        investments.init_schema(conn)
        assert conn.execute("SELECT COUNT(*) FROM investment_products").fetchone()[0] == 3
