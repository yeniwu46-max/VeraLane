"""User-confirmed categorization overlays, monthly budgets and cash scenarios."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
import re
import secrets
import sqlite3
from typing import Any

from fastapi import HTTPException

from .clock import business_date, business_now
from .db import ACCOUNT_ID, USER_ID, audit, db_session, utc_now
from .execution_controls import available_cents
from .service import create_action, money


ACTION_TYPES = ("bill_classification", "bill_budget_upsert", "bill_budget_delete")
DEFAULT_CATEGORIES = {"餐饮", "交通", "居住", "日用", "数字服务", "转账", "差旅", "医疗", "教育", "娱乐", "理财申购"}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS bill_category_overrides (
        transaction_id TEXT PRIMARY KEY REFERENCES transactions(id),account_id TEXT NOT NULL REFERENCES accounts(id),session_id TEXT NOT NULL,
        original_category TEXT NOT NULL,category TEXT NOT NULL,reason TEXT NOT NULL,version INTEGER NOT NULL CHECK(version>0),
        action_id TEXT NOT NULL REFERENCES actions(id),updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bill_category_history (
        action_id TEXT PRIMARY KEY REFERENCES actions(id),transaction_id TEXT NOT NULL REFERENCES transactions(id),account_id TEXT NOT NULL,
        session_id TEXT NOT NULL,original_category TEXT NOT NULL,previous_category TEXT NOT NULL,category TEXT NOT NULL,
        reason TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS bill_budgets (
        id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),session_id TEXT NOT NULL,month TEXT NOT NULL,
        category TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),version INTEGER NOT NULL CHECK(version>0),
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(account_id,session_id,month,category))""")


def apply_effective_categories(conn: sqlite3.Connection, rows) -> list[dict]:
    overrides = {row["transaction_id"]: row for row in conn.execute("SELECT * FROM bill_category_overrides WHERE account_id=?", (ACCOUNT_ID,))}
    result = []
    for row in rows:
        item = dict(row)
        override = overrides.get(item["id"])
        item["original_category"] = item.get("original_category", item["category"])
        item["classification_reason"] = override["reason"] if override else None
        item["classification_version"] = override["version"] if override else 0
        if override:
            item["category"] = override["category"]
        result.append(item)
    return result


def _category(value: str) -> str:
    value = value.strip()
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9_-]{1,20}", value):
        raise HTTPException(422, "分类须为 1–20 个汉字、字母、数字、下划线或短横线")
    return value


def _transaction(conn: sqlite3.Connection, transaction_id: str) -> dict:
    row = conn.execute("SELECT id,posted_on,direction,amount_cents,counterparty,category,note FROM transactions WHERE id=? AND account_id=? AND direction='out' AND posted_on<=?", (transaction_id, ACCOUNT_ID, business_date(conn))).fetchone()
    if row is None:
        raise HTTPException(404, "未找到当前账户已发生的支出交易")
    return dict(row)


def _override(conn: sqlite3.Connection, transaction_id: str):
    return conn.execute("SELECT * FROM bill_category_overrides WHERE transaction_id=? AND account_id=?", (transaction_id, ACCOUNT_ID)).fetchone()


def _owned_budget(conn: sqlite3.Connection, budget_id: str, session_id: str):
    row = conn.execute("SELECT * FROM bill_budgets WHERE id=? AND account_id=? AND session_id=?", (budget_id, ACCOUNT_ID, session_id)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该会话的预算")
    return row


def _amount(value: str) -> int:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise HTTPException(422, "预算金额无效") from exc
    if not amount.is_finite() or amount < 0 or amount > 100000 or amount.as_tuple().exponent < -2:
        raise HTTPException(422, "预算金额须为 0–100000 元且精确到分")
    return int(amount * 100)


def prepare_classification(session_id: str, transaction_id: str, category: str, reason: str) -> dict:
    category = _category(category)
    if not reason.strip() or len(reason.strip()) > 200:
        raise HTTPException(422, "请填写 1–200 字的归类原因")
    with db_session() as conn:
        transaction = _transaction(conn, transaction_id)
        previous = _override(conn, transaction_id)
        if previous and previous["session_id"] != session_id:
            raise HTTPException(404, "此交易的分类修改属于另一会话")
        payload = {"transaction_id": transaction_id, "transaction": {**transaction, "amount_yuan": money(transaction["amount_cents"])},
                   "transaction_snapshot": transaction, "original_category": transaction["category"],
                   "previous_category": previous["category"] if previous else transaction["category"], "category": category,
                   "reason": reason.strip(), "expected_version": previous["version"] if previous else 0, "previous": dict(previous) if previous else None}
        action = create_action(conn, session_id, "bill_classification", "yellow", payload)
        return {"mode": "offline", "message": "请核对这笔支出的新分类与原因。只修改统计分类，原始交易字段和金额保留。", "pending_action": action}


def execute_classification(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    transaction = _transaction(conn, payload["transaction_id"])
    if transaction != payload["transaction_snapshot"]:
        raise HTTPException(409, "原始交易记录已变化，请重新核对")
    current = _override(conn, transaction["id"])
    if (dict(current) if current else None) != payload["previous"]:
        raise HTTPException(409, "分类已被其他编辑更新，旧确认失效")
    if current and current["session_id"] != session_id:
        raise HTTPException(404, "此分类修改不属于当前会话")
    category = _category(payload["category"])
    version = payload["expected_version"] + 1
    now = utc_now()
    conn.execute("INSERT INTO bill_category_overrides VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(transaction_id) DO UPDATE SET category=excluded.category,reason=excluded.reason,version=excluded.version,action_id=excluded.action_id,updated_at=excluded.updated_at",
                 (transaction["id"], ACCOUNT_ID, session_id, transaction["category"], category, payload["reason"], version, action_id, now))
    conn.execute("INSERT INTO bill_category_history VALUES(?,?,?,?,?,?,?,?,?,?)", (action_id, transaction["id"], ACCOUNT_ID, session_id, transaction["category"], payload["previous_category"], category, payload["reason"], version, now))
    audit(conn, session_id, "bill_classification_updated", {"action_id": action_id, "transaction_id": transaction["id"], "original_category": transaction["category"], "category": category, "version": version})
    return {"status": "completed", "action_id": action_id, "transaction_id": transaction["id"], "category": category, "version": version,
            "message": f"已将该笔支出的统计分类改为“{category}”；原始分类“{transaction['category']}”及金额保留。"}


def prepare_budget(session_id: str, month: str, category: str | None, amount_yuan: str, budget_id: str | None = None) -> dict:
    category_key = _category(category) if category is not None else "*"
    amount = _amount(amount_yuan)
    with db_session() as conn:
        return prepare_budget_in_connection(conn, session_id, month, category, amount_yuan, budget_id)


def prepare_budget_in_connection(conn: sqlite3.Connection, session_id: str, month: str, category: str | None,
                                 amount_yuan: str, budget_id: str | None = None) -> dict:
    category_key = _category(category) if category is not None else "*"
    amount = _amount(amount_yuan)
    if month != business_date(conn)[:7]:
        raise HTTPException(422, "只支持为当前演示月份设置或修改预算")
    previous = _owned_budget(conn, budget_id, session_id) if budget_id else None
    duplicate = conn.execute("SELECT id FROM bill_budgets WHERE account_id=? AND session_id=? AND month=? AND category=?", (ACCOUNT_ID, session_id, month, category_key)).fetchone()
    if duplicate and (not previous or duplicate["id"] != previous["id"]):
        raise HTTPException(409, "该分类已有本月预算，请编辑原预算")
    payload = {"budget_id": budget_id or "budget-" + secrets.token_urlsafe(15), "month": month, "category": category,
               "amount_cents": amount, "amount_yuan": money(amount), "expected_version": previous["version"] if previous else 0,
               "previous": dict(previous) if previous else None}
    action = create_action(conn, session_id, "bill_budget_upsert", "yellow", payload)
    return {"mode": "offline", "message": "请确认本月预算。预算只用于对照统计，不冻结或划转资金。", "pending_action": action}


def execute_budget(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    if payload["month"] != business_date(conn)[:7]:
        raise HTTPException(409, "演示月份已变化，请重新设置预算")
    if payload["previous"]:
        previous = _owned_budget(conn, payload["budget_id"], session_id)
        if dict(previous) != payload["previous"]:
            raise HTTPException(409, "预算已变化，旧确认失效")
    elif conn.execute("SELECT 1 FROM bill_budgets WHERE id=?", (payload["budget_id"],)).fetchone():
        raise HTTPException(409, "该预算已存在")
    category = _category(payload["category"]) if payload["category"] is not None else "*"
    duplicate = conn.execute("SELECT id FROM bill_budgets WHERE account_id=? AND session_id=? AND month=? AND category=?", (ACCOUNT_ID, session_id, payload["month"], category)).fetchone()
    if duplicate and duplicate["id"] != payload["budget_id"]:
        raise HTTPException(409, "该分类预算已被其他操作保存，请重新核对")
    now = utc_now()
    if payload["previous"]:
        conn.execute("UPDATE bill_budgets SET month=?,category=?,amount_cents=?,version=version+1,updated_at=? WHERE id=?", (payload["month"], category, payload["amount_cents"], now, payload["budget_id"]))
    else:
        conn.execute("INSERT INTO bill_budgets VALUES(?,?,?,?,?,?,1,?,?)", (payload["budget_id"], ACCOUNT_ID, session_id, payload["month"], category, payload["amount_cents"], now, now))
    audit(conn, session_id, "bill_budget_saved", {"action_id": action_id, "budget_id": payload["budget_id"], "month": payload["month"]})
    return {"status": "completed", "action_id": action_id, "budget_id": payload["budget_id"], "message": f"已保存 {payload['month']} 的{payload['category'] or '全部支出'}预算 ¥{payload['amount_yuan']}，资金余额未改变。"}


def prepare_delete_budget(session_id: str, budget_id: str) -> dict:
    with db_session() as conn:
        row = _owned_budget(conn, budget_id, session_id)
        payload = {"budget_id": budget_id, "previous": dict(row), "category": None if row["category"] == "*" else row["category"], "month": row["month"], "amount_yuan": money(row["amount_cents"])}
        action = create_action(conn, session_id, "bill_budget_delete", "yellow", payload)
        return {"mode": "offline", "message": "请确认删除预算；不会删除流水、退款或释放任何预留资金。", "pending_action": action}


def execute_delete_budget(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    row = _owned_budget(conn, payload["budget_id"], session_id)
    if dict(row) != payload["previous"]:
        raise HTTPException(409, "预算已变化，请重新核对后删除")
    conn.execute("DELETE FROM bill_budgets WHERE id=?", (row["id"],))
    audit(conn, session_id, "bill_budget_deleted", {"action_id": action_id, "budget_id": row["id"]})
    return {"status": "completed", "action_id": action_id, "message": "已删除预算，原始账单及资金余额保留。"}


# Common dispatcher uses the same short delete name as other preference tools.
execute_delete = execute_delete_budget


def discard_action(session_id: str, action_id: str) -> dict:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=?", (action_id, session_id)).fetchone()
        if row is None or row["type"] not in ACTION_TYPES:
            raise HTTPException(404, "未找到本会话的账单编辑")
        if row["status"] == "completed":
            raise HTTPException(409, "这次编辑已执行，请刷新后重新修改")
        if row["status"] == "pending":
            conn.execute("UPDATE actions SET status='failed' WHERE id=?", (action_id,))
            audit(conn, session_id, "bill_edit_discarded", {"action_id": action_id})
        return {"status": "discarded"}


def _period_bounds(period: str, today: date) -> tuple[str, str]:
    if period in ("今年", "去年"):
        year = today.year - (period == "去年")
        return str(date(year, 1, 1)), str(min(today, date(year, 12, 31)))
    if period == "本月":
        start = today.replace(day=1)
    elif period in ("上个月", "上月"):
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        try:
            start = date.fromisoformat(period + "-01")
        except ValueError as exc:
            raise HTTPException(422, "期间须为本月、上个月、今年、去年或 YYYY-MM") from exc
    if start > today:
        raise HTTPException(422, "不能查询尚未发生的预算账单月份")
    return str(start), str(min(today, start.replace(day=monthrange(start.year, start.month)[1])))


def _all_expenses(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT id,posted_on,direction,amount_cents,counterparty,category,note FROM transactions WHERE account_id=? AND direction='out' AND posted_on<=? ORDER BY posted_on DESC,id", (ACCOUNT_ID, business_date(conn))).fetchall()
    return [{**row, "amount_yuan": money(row["amount_cents"])} for row in apply_effective_categories(conn, rows)]


def _forecast(conn: sqlite3.Connection) -> dict:
    now = business_now(conn)
    end = now + timedelta(days=30)
    available = available_cents(conn)
    balance = conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (ACCOUNT_ID,)).fetchone()[0]
    transfers = []
    for row in conn.execute("SELECT s.*,a.type AS action_type,a.status AS action_status,a.tier AS action_tier,a.session_id AS action_session,a.payload_json AS authorized_payload FROM scheduled_transfers s JOIN actions a ON a.id=s.id WHERE s.account_id=? AND s.status='pending' AND s.execute_at>? AND s.execute_at<=? ORDER BY s.execute_at", (ACCOUNT_ID, now.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))):
        payload = json.loads(row["payload_json"])
        if (row["action_status"] == "completed" and row["action_type"] == "scheduled_transfer" and row["action_tier"] == "yellow"
                and row["action_session"] == row["session_id"] and payload == json.loads(row["authorized_payload"])
                and row["execute_at"] == payload.get("execute_at") and row["expires_at"] == payload.get("window_expires_at")
                and type(payload.get("amount_cents")) is int and 0 < payload["amount_cents"] <= 100_000):
            transfers.append({"id": row["id"], "label": payload["recipient"], "at": row["execute_at"], "amount_yuan": money(payload["amount_cents"]), "amount_cents": payload["amount_cents"], "status": "authorized", "kind": "scheduled_transfer"})
    for row in conn.execute("""SELECT o.*,p.session_id,p.payload_json,p.status AS plan_status,a.type AS action_type,
        a.status AS action_status,a.session_id AS action_session,a.tier AS action_tier,a.payload_json AS authorized_payload
        FROM recurring_occurrences o JOIN recurring_plans p ON p.id=o.plan_id JOIN actions a ON a.id=p.id
        WHERE p.account_id=? AND p.status='active' AND o.status='pending' AND o.execute_at>? AND o.execute_at<=?
        ORDER BY o.execute_at,o.id""", (ACCOUNT_ID, now.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))):
        payload = json.loads(row["payload_json"])
        matches = [item for item in payload.get("occurrences", []) if item.get("index") == row["occurrence_index"]]
        authorized = (row["action_status"] == "completed" and row["action_type"] == "recurring_transfer"
                      and row["action_session"] == row["session_id"] and payload == json.loads(row["authorized_payload"])
                      and type(payload.get("amount_cents")) is int and 0 < payload["amount_cents"] <= 10_000_000
                      and type(payload.get("count")) is int and 1 <= payload["count"] <= 12
                      and len(payload.get("occurrences", [])) == payload["count"]
                      and payload.get("total_cents") == payload["amount_cents"] * payload["count"]
                      and (row["action_tier"] == "red" or payload["total_cents"] <= 100_000)
                      and len(matches) == 1 and row["id"] == f"recurring-{row['plan_id']}-{row['occurrence_index']}")
        if authorized and row["execute_at"] == matches[0].get("execute_at") and row["window_expires_at"] == matches[0].get("window_expires_at") and row["window_expires_at"] > now.isoformat(timespec="seconds"):
            transfers.append({"id": row["id"], "label": f"{payload['recipient']} · 周期第 {row['occurrence_index']} 期", "at": row["execute_at"],
                              "amount_cents": payload["amount_cents"], "amount_yuan": money(payload["amount_cents"]), "status": "authorized", "kind": "recurring_transfer"})
    transfers.sort(key=lambda item: (item["at"], item["id"]))
    debits = [{"id": row["id"], "label": row["merchant"], "at": row["renewal_on"], "amount_cents": row["amount_cents"], "amount_yuan": money(row["amount_cents"]), "status": "estimated"}
              for row in conn.execute("SELECT * FROM subscriptions WHERE user_id=? AND status='active' AND renewal_on>=? AND renewal_on<=? ORDER BY renewal_on", (USER_ID, str(now.date()), str(end.date())))]
    reservations = [{"id": row["id"], "purpose": row["purpose"], "remaining_yuan": money(row["remaining_cents"])} for row in conn.execute("SELECT * FROM fund_reservations WHERE account_id=? AND status='active'", (ACCOUNT_ID,))]
    settlements = [{"id": row["id"], "at": row["settles_on"] + "T09:00:00+08:00", "amount_yuan": money(row["amount_cents"]), "status": "pending_settlement"} for row in conn.execute("SELECT * FROM investment_orders WHERE account_id=? AND status='pending_settlement' AND settles_on<=? ORDER BY settles_on", (ACCOUNT_ID, str(end.date())))]
    transfers_total = sum(item["amount_cents"] for item in transfers)
    debits_total = sum(item["amount_cents"] for item in debits)
    return {"from": now.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds"), "balance_yuan": money(balance), "reserved_yuan": money(balance - available), "available_yuan": money(available),
            "authorized_transfers": transfers, "authorized_transfer_yuan": money(transfers_total), "known_debits": debits, "known_debit_yuan": money(debits_total), "reservations": reservations, "pending_settlements": settlements,
            "after_authorized_yuan": money(available - transfers_total), "after_known_debits_yuan": money(available - transfers_total - debits_total),
            "limitations": ["预测只使用已保存记录，未知消费、收入和执行失败会改变结果，不是保证余额。", "已发生支出已反映到账面余额；当前可用已扣预留，场景计算不再重复扣减。", "代扣按协议下一日期估计，不属于新授权；与已确认预约分开展示。", "AA 待收款及未到账理财赎回不计入可用现金。"]}


def preferences(session_id: str, period: str = "本月") -> dict[str, Any]:
    with db_session() as conn:
        today = date.fromisoformat(business_date(conn))
        start, end = _period_bounds(period, today)
        all_expenses = _all_expenses(conn)
        transactions = [row for row in all_expenses if start <= row["posted_on"] <= end]
        month = today.strftime("%Y-%m")
        month_rows = [row for row in all_expenses if row["posted_on"].startswith(month)]
        budgets = []
        for row in conn.execute("SELECT * FROM bill_budgets WHERE account_id=? AND session_id=? ORDER BY month DESC,category", (ACCOUNT_ID, session_id)):
            spend_rows = [tx for tx in all_expenses if tx["posted_on"].startswith(row["month"]) and (row["category"] == "*" or tx["category"] == row["category"])]
            spent = sum(tx["amount_cents"] for tx in spend_rows)
            budgets.append({"id": row["id"], "month": row["month"], "category": None if row["category"] == "*" else row["category"], "version": row["version"], "amount_yuan": money(row["amount_cents"]), "spent_yuan": money(spent), "remaining_yuan": money(row["amount_cents"] - spent), "over_yuan": money(max(0, spent - row["amount_cents"])), "transaction_ids": [tx["id"] for tx in spend_rows]})
        histories = [dict(row) for row in conn.execute("SELECT transaction_id,original_category,previous_category,category,reason,version,created_at FROM bill_category_history WHERE account_id=? AND session_id=? ORDER BY rowid DESC", (ACCOUNT_ID, session_id))]
        return {"current_month": month, "period_start": start, "period_end": end, "transactions": transactions, "categories": sorted(DEFAULT_CATEGORIES | {row["category"] for row in all_expenses}),
                "classification_history": histories, "budgets": budgets, "current_month_spent_yuan": money(sum(row["amount_cents"] for row in month_rows)), "forecast": _forecast(conn), "notice": "归类只改变统计口径，预算只用于对照；不修改原始流水，不自动限制或执行付款。"}
