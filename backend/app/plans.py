"""Finite, versioned spending plans: query, diagnose, select, then consent."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal
import json
import re
import secrets
import sqlite3
from typing import Any

from fastapi import HTTPException

from .clock import business_date
from .db import ACCOUNT_ID, USER_ID, audit, db_session, utc_now
from .insights import build_report
from .service import cents_from_yuan, create_action, money
from .subscription_intelligence import diagnosis, execute_batch


TERMINAL = {"completed", "partially_completed", "failed", "cancelled"}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS spending_plans (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
        session_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version > 0),
        status TEXT NOT NULL CHECK(status IN ('draft','awaiting_confirmation','completed','partially_completed','failed','cancelled')),
        draft_json TEXT NOT NULL, authorized_payload_json TEXT, pending_action_id TEXT,
        result_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS spending_plans_owner ON spending_plans(account_id,session_id,created_at)")


def _parse_goal(message: str) -> tuple[int, str]:
    if not re.search(r"下(?:个)?月", message):
        raise ValueError("请说明下个月希望减少的支出，例如“下个月少花 300 元”。")
    if re.search(r"最近|近期|周|季度|半年|每天|每月|明年|前年|\d{4}[-年]|\d+月", message):
        raise ValueError("首版优化计划只支持下一个自然月，请使用“下个月少花 300 元”。")
    if re.search(r"保留|不(?:要|能)?取消|除了|不含|不包括|只取消", message):
        raise ValueError("请先描述节省目标，再在计划中逐项选择允许取消的协议；未选中的项目会保留。")
    if re.search(r"转账|转给|冻结|挂失|买入|申购|赎回|执行代码|删除", message):
        raise ValueError("这个计划只支持账单查询、协议核实和选择后取消代扣。请单独描述下个月的节省目标。")
    amounts = re.findall(r"(?:少花|节省|省下|省|减少(?:支出|消费)?)[\s]*(\d+(?:\.\d+)?)\s*元", message)
    all_amounts = re.findall(r"\d+(?:\.\d+)?\s*元", message)
    if len(amounts) != 1 or len(all_amounts) != 1:
        raise ValueError("请提供一个明确的目标金额，例如“下个月少花 300 元”。")
    target = cents_from_yuan(amounts[0])
    if target is None:
        raise ValueError("目标金额须大于零、精确到分，且不超过 100000 元。")
    # The query step is separate from the future savings period.
    period = "上个月" if "上个月" in message or "上月" in message else "去年" if "去年" in message else "今年" if "今年" in message else "本月"
    return target, period


def _owned(conn: sqlite3.Connection, plan_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM spending_plans WHERE id=? AND account_id=? AND session_id=?", (plan_id, ACCOUNT_ID, session_id)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该会话的支出优化计划")
    return row


def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {"subscription_id": row["id"], "merchant": row["merchant"], "amount_cents": row["amount_cents"],
            "amount_yuan": money(row["amount_cents"]), "renewal_on": row["renewal_on"]}


def _public(row: sqlite3.Row) -> dict[str, Any]:
    draft = json.loads(row["draft_json"])
    authorization = json.loads(row["authorized_payload_json"]) if row["authorized_payload_json"] else None
    result = json.loads(row["result_json"]) if row["result_json"] else None
    chosen = [item["subscription_id"] for item in authorization["items"]] if authorization else []
    state = row["status"]
    expected = result["expected_savings_yuan"] if result else authorization["expected_savings_yuan"] if authorization and state != "cancelled" else "0.00"
    expected_cents = int(Decimal(expected) * 100)
    select_status = "completed" if chosen else "awaiting_input"
    execution_status = state if state in TERMINAL else "awaiting_confirmation" if chosen else "blocked"
    if state == "cancelled":
        select_status = "cancelled"
    steps = [
        {"id": "query", "label": "查询消费变化", "status": "completed", "depends_on": [],
         "evidence_ids": draft["insight_evidence_ids"], "summary": draft["insight_summary"]},
        {"id": "diagnose", "label": "核实代扣协议", "status": "completed", "depends_on": ["query"],
         "evidence_ids": [item["subscription_id"] for item in draft["options"]],
         "summary": f"找到 {len(draft['options'])} 项仍在生效的模拟银行代扣协议。"},
        {"id": "select", "label": "选择允许取消的项目", "status": select_status, "depends_on": ["diagnose"],
         "evidence_ids": chosen, "summary": f"已选择 {len(chosen)} 项；修改选择后必须重新确认。" if chosen else "请逐项选择，未选中项目保持生效。"},
        {"id": "cancel", "label": "确认并逐项取消代扣", "status": execution_status, "depends_on": ["select"],
         "evidence_ids": [item["subscription_id"] for item in (result or {}).get("items", [])],
         "summary": result["message"] if result else "确认后执行；不会自动退订商户会员或退回已扣费用。"},
    ]
    return {"id": row["id"], "version": row["version"], "status": state, "goal": draft["goal"],
            "target_yuan": money(draft["target_cents"]), "period_start": draft["period_start"], "period_end": draft["period_end"],
            "selected_subscription_ids": chosen, "expected_savings_yuan": expected,
            "remaining_target_yuan": money(max(0, draft["target_cents"] - expected_cents)),
            "options": [{key: value for key, value in item.items() if key != "amount_cents"} for item in draft["options"]],
            "steps": steps, "insight_summary": draft["insight_summary"], "insight_report": draft["insight_report"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "pending_action_id": row["pending_action_id"], "result": result}


def preview_plan(session_id: str, message: str) -> dict[str, Any]:
    try:
        target, query_period = _parse_goal(message)
    except ValueError as exc:
        return {"status": "needs_clarification", "mode": "offline", "message": str(exc), "clarification": str(exc)}
    with db_session() as conn:
        report = build_report(conn, {"period": query_period})
        diagnostics = diagnosis(conn)
        by_id = {item["subscription_id"]: item for item in diagnostics["items"] if item["subscription_id"]}
        today = date.fromisoformat(business_date(conn))
        first = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        last = first.replace(day=monthrange(first.year, first.month)[1])
        options = []
        for sub in conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND status='active' ORDER BY renewal_on,id", (USER_ID,)):
            options.append({**_snapshot(sub), "eligible_in_period": str(first) <= sub["renewal_on"] <= str(last),
                            "evidence": by_id.get(sub["id"], {}).get("evidence", [])})
        draft = {"goal": message, "target_cents": target, "period_start": str(first), "period_end": str(last),
                 "options": options, "insight_summary": report["summary"], "insight_report": report,
                 "insight_evidence_ids": list(dict.fromkeys(tx["id"] for tx in report["comparison"]["transactions"]))}
        plan_id, now = "plan-" + secrets.token_urlsafe(15), utc_now()
        conn.execute("INSERT INTO spending_plans(id,account_id,session_id,version,status,draft_json,created_at,updated_at) VALUES(?,?,?,1,'draft',?,?,?)",
                     (plan_id, ACCOUNT_ID, session_id, json.dumps(draft, ensure_ascii=False), now, now))
        audit(conn, session_id, "spending_plan_created", {"plan_id": plan_id, "target_yuan": money(target), "period_start": str(first), "option_count": len(options)})
        plan = _public(_owned(conn, plan_id, session_id))
        return {"status": "ok", "mode": "offline", "message": "已完成账单查询和协议核实。请选择允许取消的项目，确认前不会更改任何协议。", "plan": plan}


def list_plans(session_id: str) -> dict[str, Any]:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM spending_plans WHERE account_id=? AND session_id=? ORDER BY created_at DESC,rowid DESC", (ACCOUNT_ID, session_id)).fetchall()
        return {"plans": [_public(row) for row in rows]}


def get_plan(plan_id: str, session_id: str) -> dict[str, Any]:
    with db_session() as conn:
        return _public(_owned(conn, plan_id, session_id))


def prepare_plan(plan_id: str, session_id: str, subscription_ids: list[str]) -> dict[str, Any]:
    if not subscription_ids or len(subscription_ids) != len(set(subscription_ids)):
        raise HTTPException(422, "请选择不重复的具体代扣协议")
    with db_session() as conn:
        row = _owned(conn, plan_id, session_id)
        if row["status"] in TERMINAL:
            raise HTTPException(409, "该计划已结束；如需新的选择，请新建计划")
        draft = json.loads(row["draft_json"])
        if business_date(conn) > draft["period_end"]:
            raise HTTPException(409, "计划周期已经结束，请重新制定计划")
        original = {item["subscription_id"]: item for item in draft["options"]}
        items = []
        for subscription_id in subscription_ids:
            if subscription_id not in original:
                raise HTTPException(422, "所选协议不在本计划已核实的候选范围内")
            sub = conn.execute("SELECT * FROM subscriptions WHERE id=? AND user_id=? AND status='active'", (subscription_id, USER_ID)).fetchone()
            if sub is None or any(_snapshot(sub)[key] != original[subscription_id][key] for key in _snapshot(sub)):
                raise HTTPException(409, "协议状态、金额或日期已变化，请重新制定计划并核对")
            items.append(_snapshot(sub))
        expected = sum(item["amount_cents"] for item in items if max(draft["period_start"], business_date(conn)) <= item["renewal_on"] <= draft["period_end"])
        version = row["version"] + 1
        payload = {"plan_id": plan_id, "plan_version": version, "items": items, "period_start": draft["period_start"],
                   "period_end": draft["period_end"], "target_yuan": money(draft["target_cents"]),
                   "expected_savings_yuan": money(expected), "remaining_target_yuan": money(max(0, draft["target_cents"] - expected))}
        action = create_action(conn, session_id, "spending_plan", "yellow", payload)
        conn.execute("UPDATE spending_plans SET version=?,status='awaiting_confirmation',authorized_payload_json=?,pending_action_id=?,updated_at=? WHERE id=?",
                     (version, json.dumps(payload, ensure_ascii=False), action["id"], utc_now(), plan_id))
        audit(conn, session_id, "spending_plan_prepared", {"plan_id": plan_id, "version": version, "action_id": action["id"], "subscription_ids": subscription_ids})
        return {"mode": "offline", "message": "请核对本次选择和预计减少金额。只有确认后才会逐项取消模拟银行代扣。", "plan": _public(_owned(conn, plan_id, session_id)), "pending_action": action}


def execute_plan(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Called only inside the common action confirmation transaction."""
    row = _owned(conn, payload.get("plan_id", ""), session_id)
    if row["pending_action_id"] != action_id or row["version"] != payload.get("plan_version"):
        raise HTTPException(409, "计划选择已修改，旧授权失效；请核对最新版本")
    if row["status"] != "awaiting_confirmation":
        raise HTTPException(409, "该计划已结束或已取消")
    authorized = json.loads(row["authorized_payload_json"] or "null")
    action = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=? AND type='spending_plan'", (action_id, session_id)).fetchone()
    if authorized != payload or action is None or action["tier"] != "yellow" or action["status"] != "pending" or json.loads(action["payload_json"]) != authorized:
        raise HTTPException(409, "计划授权内容不一致，请重新核对")
    if business_date(conn) > payload["period_end"]:
        raise HTTPException(409, "计划周期已经结束，未取消任何协议")
    # Each selected snapshot is rechecked independently by the deterministic
    # cancellation tool. Business conflicts retain honest partial receipts;
    # technical errors roll back the common SQLite transaction in full.
    result = execute_batch(conn, action_id, session_id, payload)
    target = json.loads(row["draft_json"])["target_cents"]
    saved = int(Decimal(result["expected_savings_yuan"]) * 100)
    result.update(plan_id=row["id"], plan_version=row["version"], target_yuan=money(target), remaining_target_yuan=money(max(0, target - saved)))
    result["message"] += f" 该期间预计减少 ¥{money(saved)}，距目标还差 ¥{money(max(0, target - saved))}；不是已退款现金。"
    conn.execute("UPDATE spending_plans SET status=?,result_json=?,updated_at=? WHERE id=?", (result["status"], json.dumps(result, ensure_ascii=False), utc_now(), row["id"]))
    audit(conn, session_id, "spending_plan_executed", {"plan_id": row["id"], "version": row["version"], "status": result["status"], "expected_savings_yuan": result["expected_savings_yuan"]})
    return result


def cancel_plan(plan_id: str, session_id: str) -> dict[str, Any]:
    with db_session() as conn:
        row = _owned(conn, plan_id, session_id)
        if row["status"] not in TERMINAL:
            conn.execute("UPDATE spending_plans SET status='cancelled',version=version+1,updated_at=? WHERE id=?", (utc_now(), plan_id))
            audit(conn, session_id, "spending_plan_cancelled", {"plan_id": plan_id})
        return _public(_owned(conn, plan_id, session_id))


def invalidate_selection(plan_id: str, session_id: str) -> dict[str, Any]:
    """Editing a confirmed summary invalidates it before new choices are made."""
    with db_session() as conn:
        row = _owned(conn, plan_id, session_id)
        if row["status"] in TERMINAL:
            raise HTTPException(409, "该计划已经执行或结束，请刷新查看真实结果")
        if row["pending_action_id"] is not None:
            conn.execute("UPDATE spending_plans SET version=version+1,status='draft',authorized_payload_json=NULL,pending_action_id=NULL,updated_at=? WHERE id=?", (utc_now(), plan_id))
            audit(conn, session_id, "spending_plan_authorization_invalidated", {"plan_id": plan_id, "previous_version": row["version"]})
        return _public(_owned(conn, plan_id, session_id))
