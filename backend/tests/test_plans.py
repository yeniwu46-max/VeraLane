"""Consent/version boundaries for fixed-tool spending optimization plans."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app import db, plans, subscription_intelligence
from app.history_fixture import load_history_fixture
from app.plans_api import PlanOwner, router
from app.service import confirm_action


SESSION = "plan-owner"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "plans.sqlite3")
    monkeypatch.setattr(db, "DEMO_DATE", "2026-09-30")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    db.init_db()
    with db.db_session() as conn:
        subscription_intelligence.init_schema(conn)
        plans.init_schema(conn)
    app = FastAPI()
    app.include_router(router)

    @app.post("/api/actions/{action_id}/confirm")
    def confirm(action_id: str, body: PlanOwner):
        return confirm_action(action_id, body.session_id)

    with TestClient(app) as test_client:
        yield test_client


def preview(client, message="下个月少花300元", session=SESSION):
    response = client.post("/api/plans/preview", json={"session_id": session, "message": message})
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, plan, ids=("sub-music",), session=SESSION):
    return client.post(f"/api/plans/{plan['id']}/prepare", json={"session_id": session, "subscription_ids": list(ids)})


def confirm(client, action, session=SESSION):
    return client.post(f"/api/actions/{action['id']}/confirm", json={"session_id": session})


def states():
    with db.db_session() as conn:
        return {row["id"]: row["status"] for row in conn.execute("SELECT id,status FROM subscriptions")}


def test_preview_actual_evidence_and_no_side_effects(client):
    result = preview(client, "帮我看看上个月为什么花得多，再看看下个月能不能少花300元")
    assert result["status"] == "ok"
    plan = result["plan"]
    assert plan["target_yuan"] == "300.00"
    assert plan["period_start"] == "2026-10-01"
    assert plan["period_end"] == "2026-10-31"
    assert plan["insight_report"]["period"] == "2026-08"
    assert "比较基期" in plan["insight_summary"]
    assert "无法判断支出变化原因" in plan["insight_summary"]
    assert len(plan["steps"]) == 4
    assert plan["steps"][2]["status"] == "awaiting_input"
    assert {option["subscription_id"] for option in plan["options"]} == {"sub-cloud", "sub-music"}
    assert all(option["evidence"] for option in plan["options"])
    assert plan["expected_savings_yuan"] == "0.00"
    assert states() == {"sub-cloud": "active", "sub-music": "active"}
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0


def test_preview_explains_change_when_historical_comparison_evidence_exists(client):
    with db.db_session() as conn:
        assert load_history_fixture(conn) == 32

    plan = preview(client, "帮我看看上个月为什么花得多，再看看下个月能不能少花300元")["plan"]

    assert plan["insight_report"]["comparison"]["previous_total_yuan"] == "602.00"
    assert "支出增加 ¥58.00" in plan["insight_summary"]
    assert "金额变化最大的分类是餐饮，增加 ¥85.00" in plan["insight_summary"]
    assert "无法判断支出变化原因" not in plan["insight_summary"]


@pytest.mark.parametrize("message", ["下个月省钱", "少花300元", "下个月少花0元", "下个月少花1.001元", "下个月少花300元或200元", "下周少花300元", "下个月少花300元保留音乐", "下个月少花300元，不取消任何订阅", "下个月少花300元，再转给林悦100元"])
def test_missing_or_unsupported_goal_clarifies_without_creating_plan(client, message):
    result = preview(client, message)
    assert result["status"] == "needs_clarification"
    assert "plan" not in result
    assert client.get("/api/plans", params={"session_id": SESSION}).json()["plans"] == []


def test_selection_requires_confirmation_and_exact_gap(client):
    plan = preview(client)["plan"]
    response = prepare(client, plan)
    assert response.status_code == 200, response.text
    selection = response.json()
    assert selection["pending_action"]["type"] == "spending_plan"
    assert selection["plan"]["version"] == 2
    assert selection["plan"]["remaining_target_yuan"] == "172.00"
    assert states()["sub-music"] == "active"
    result = confirm(client, selection["pending_action"])
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "completed"
    assert body["expected_savings_yuan"] == "128.00"
    assert body["remaining_target_yuan"] == "172.00"
    assert states() == {"sub-music": "cancelled", "sub-cloud": "active"}
    assert len(body["items"]) == 1
    stored = client.get(f"/api/plans/{plan['id']}", params={"session_id": SESSION}).json()
    assert stored["result"] == body
    assert stored["steps"][3]["status"] == "completed"


def test_updated_selection_invalidates_old_consent(client):
    plan = preview(client)["plan"]
    old = prepare(client, plan).json()["pending_action"]
    new = prepare(client, plan, ["sub-cloud"]).json()["pending_action"]
    response = confirm(client, old)
    assert response.status_code == 409
    assert all(status == "active" for status in states().values())
    assert confirm(client, new).status_code == 200
    assert states() == {"sub-cloud": "cancelled", "sub-music": "active"}


def test_edit_begin_invalidates_old_consent_even_without_new_selection(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    endpoint = f"/api/plans/{plan['id']}/invalidate"
    response = client.post(endpoint, json={"session_id": SESSION})
    assert response.status_code == 200
    draft = response.json()
    assert draft["version"] == 3
    assert draft["status"] == "draft"
    assert draft["pending_action_id"] is None
    assert draft["selected_subscription_ids"] == []
    assert draft["expected_savings_yuan"] == "0.00"
    assert client.post(endpoint, json={"session_id": SESSION}).json()["version"] == 3
    assert confirm(client, action).status_code == 409
    assert all(status == "active" for status in states().values())
    assert client.post(endpoint, json={"session_id": "other"}).status_code == 404
    new_action = prepare(client, draft).json()["pending_action"]
    assert confirm(client, new_action).status_code == 200
    assert client.post(endpoint, json={"session_id": SESSION}).status_code == 409


def test_owner_isolation_and_unknown_selection(client):
    plan = preview(client)["plan"]
    assert client.get("/api/plans", params={"session_id": "stranger"}).json()["plans"] == []
    assert client.get(f"/api/plans/{plan['id']}", params={"session_id": "stranger"}).status_code == 404
    assert prepare(client, plan, session="stranger").status_code == 404
    assert client.post(f"/api/plans/{plan['id']}/cancel", json={"session_id": "stranger"}).status_code == 404
    assert prepare(client, plan, ["unknown"]).status_code == 422
    assert prepare(client, plan, ["sub-cloud", "sub-cloud"]).status_code == 422
    assert prepare(client, plan, []).status_code == 422
    action = prepare(client, plan).json()["pending_action"]
    assert confirm(client, action, "stranger").status_code == 404


def test_partial_result_does_not_count_failed_item(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan, ["sub-music", "sub-cloud"]).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE subscriptions SET amount_cents=16900 WHERE id='sub-cloud'")
    result = confirm(client, action).json()
    assert result["status"] == "partially_completed"
    assert [item["status"] for item in result["items"]] == ["completed", "failed"]
    assert result["expected_savings_yuan"] == "128.00"
    assert result["remaining_target_yuan"] == "172.00"
    assert states() == {"sub-music": "cancelled", "sub-cloud": "active"}


def test_all_failed_is_failed_and_still_full_gap(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE subscriptions SET amount_cents=999 WHERE id='sub-music'")
    result = confirm(client, action).json()
    assert result["status"] == "failed"
    assert result["expected_savings_yuan"] == "0.00"
    assert result["remaining_target_yuan"] == "300.00"
    assert states()["sub-music"] == "active"


def test_success_repeat_confirmation_and_concurrency_is_idempotent(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(client, action), range(2)))
    assert [result.status_code for result in results] == [200, 200]
    assert results[0].json() == results[1].json()
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subscription_closures WHERE subscription_id='sub-music'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit WHERE event='spending_plan_executed'").fetchone()[0] == 1


def test_cancel_draft_invalidates_consent_and_no_savings(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    cancelled = client.post(f"/api/plans/{plan['id']}/cancel", json={"session_id": SESSION}).json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["expected_savings_yuan"] == "0.00"
    assert cancelled["remaining_target_yuan"] == "300.00"
    assert confirm(client, action).status_code == 409
    assert prepare(client, plan).status_code == 409
    assert all(status == "active" for status in states().values())


def test_cancel_completed_does_not_reverse_execution(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    assert confirm(client, action).status_code == 200
    cancelled = client.post(f"/api/plans/{plan['id']}/cancel", json={"session_id": SESSION}).json()
    assert cancelled["status"] == "completed"
    assert states()["sub-music"] == "cancelled"


def test_not_due_in_plan_month_does_not_count_cash_savings(client):
    with db.db_session() as conn:
        conn.execute("UPDATE subscriptions SET renewal_on='2026-12-16' WHERE id='sub-music'")
    plan = preview(client)["plan"]
    option = next(item for item in plan["options"] if item["subscription_id"] == "sub-music")
    assert option["eligible_in_period"] is False
    action = prepare(client, plan).json()["pending_action"]
    assert action["details"]["expected_savings_yuan"] == "0.00"
    result = confirm(client, action).json()
    assert result["status"] == "completed"
    assert result["expected_savings_yuan"] == "0.00"
    assert result["remaining_target_yuan"] == "300.00"


def test_changed_candidate_before_prepare_requires_new_plan(client):
    plan = preview(client)["plan"]
    with db.db_session() as conn:
        conn.execute("UPDATE subscriptions SET renewal_on='2026-11-16' WHERE id='sub-music'")
    assert prepare(client, plan).status_code == 409


def test_authorized_snapshot_tampering_rejected(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    with db.db_session() as conn:
        payload = json.loads(conn.execute("SELECT payload_json FROM actions WHERE id=?", (action["id"],)).fetchone()[0])
        payload["items"][0]["subscription_id"] = "sub-cloud"
        conn.execute("UPDATE actions SET payload_json=? WHERE id=?", (json.dumps(payload), action["id"]))
    assert confirm(client, action).status_code == 409
    assert all(status == "active" for status in states().values())


def test_period_ended_and_expired_consent_do_not_execute(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE demo_clock SET now='2026-11-01T09:00:00+08:00'")
    assert confirm(client, action).status_code == 409
    assert all(status == "active" for status in states().values())


def test_expired_real_time_consent_does_not_execute(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("UPDATE actions SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (action["id"],))
    assert confirm(client, action).status_code == 409
    assert all(status == "active" for status in states().values())


def test_technical_failure_rolls_back_partial_cancellations_and_plan(client):
    plan = preview(client)["plan"]
    action = prepare(client, plan, ["sub-music", "sub-cloud"]).json()["pending_action"]
    with db.db_session() as conn:
        conn.execute("CREATE TRIGGER fail_plan_result BEFORE UPDATE OF result_json ON spending_plans BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        confirm(client, action)
    assert all(status == "active" for status in states().values())
    with db.db_session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM subscription_closures").fetchone()[0] == 0
        assert conn.execute("SELECT status FROM spending_plans WHERE id=?", (plan["id"],)).fetchone()[0] == "awaiting_confirmation"


def test_persistent_plan_and_extra_fields_rejected(client):
    plan = preview(client)["plan"]
    with db.db_session() as conn:
        plans.init_schema(conn)
    assert client.get("/api/plans", params={"session_id": SESSION}).json()["plans"][0]["id"] == plan["id"]
    assert client.post("/api/plans/preview", json={"session_id": SESSION, "message": "下个月少花300元", "account_id": "other"}).status_code == 422
    assert client.post(f"/api/plans/{plan['id']}/prepare", json={"session_id": SESSION, "subscription_ids": ["sub-music"], "amount_yuan": "1"}).status_code == 422
