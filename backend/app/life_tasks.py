"""Whitelisted birthday plans, reserved budgets, and fictional merchant orders.

No merchant API, messages, payment network, or real-world delivery is called.
Every price, recipient, address, and execution window is shown before consent.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from . import db
from .clock import SHANGHAI, business_now
from .execution_controls import available_cents, consume, credit, release, require_verified, reserve


CATALOG = (
    ("flowers-classic", "flowers", "晨光花束", "模拟花店", 1, 19800),
    ("flowers-premium", "flowers", "星河花礼", "模拟花店", 1, 66800),
    ("cake-classic", "cake", "六寸生日蛋糕", "模拟蛋糕店", 1, 23800),
    ("cake-premium", "cake", "双层生日蛋糕", "模拟蛋糕店", 1, 58800),
)
PRODUCT_IDS = {row[0] for row in CATALOG}
NOTICE = "仅本地模拟商品、订单、扣款和送达；配送信息请使用虚构内容，不连接真实商户。"


def interpret(session_id: str, message: str, reset: bool = False) -> dict[str, Any]:
    """Collect explicitly supplied text fields only; never select or authorize goods."""
    from .service import get_context, set_context
    fields = ("goal", "birthday", "budget_yuan", "recipient_label", "delivery_note")
    issues = []
    with db.db_session() as conn:
        context = get_context(conn, session_id)
        previous = {} if reset else context.get("pending_birthday", {})
        draft = {key: previous.get(key) for key in fields}
        # Address text is untrusted data, not another command source.
        address = re.search(r"(?:配送信息|配送地址|收货地址|地址)\s*[:：为是]?\s*(.+)", message)
        instructions = message[:address.start()] if address else message
        if address:
            draft["delivery_note"] = address[1].strip()[:200]
        recipient = re.search(r"(?:收货人|收件人|对象)\s*[:：为是]?\s*([^，,。；;\n]+)", instructions)
        if recipient:
            draft["recipient_label"] = recipient[1].strip()[:60]
        if "生日" in instructions:
            draft["goal"] = "生日鲜花与蛋糕安排"
        goal = re.search(r"目标\s*[:：为是]?\s*([^，,。；;\n]+)", instructions)
        if goal:
            draft["goal"] = goal[1].strip()[:120]
        dates = re.findall(r"(?<!\d)(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日|号)?(?!\d)", instructions)
        if len(dates) == 1:
            try:
                parsed_date = date(*map(int, dates[0]))
                if parsed_date <= business_now(conn).date():
                    raise ValueError
                draft["birthday"] = parsed_date.isoformat()
            except ValueError:
                draft["birthday"] = None
                issues.append("生日日期无效或不是未来日期，请明确年份、月份和日期。")
        elif dates:
            draft["birthday"] = None
            issues.append("出现多个日期，请明确唯一生日。")
        elif re.search(r"\d{1,2}月\d{1,2}(?:日|号)|明年|今年|下个月|明天", instructions):
            draft["birthday"] = None
            issues.append("请补充生日的完整年份与日期，例如 2026-11-20；不会自动猜测年份。")
        amounts = re.findall(r"([+\-−\d.,，eE]+)\s*元", instructions)
        if amounts:
            if len(amounts) == 1 and re.fullmatch(r"\d+(?:\.\d{1,2})?", amounts[0]):
                from .service import cents_from_yuan
                cents = cents_from_yuan(amounts[0])
                draft["budget_yuan"] = _money(cents) if cents else None
            else:
                draft["budget_yuan"] = None
                issues.append("请说明一个总预算金额，使用普通数字并精确到分。")
        labels = {"goal": "生日目标", "birthday": "完整生日日期", "budget_yuan": "总预算",
                  "recipient_label": "明确收货人（使用“收货人：…”）", "delivery_note": "虚构配送地址（使用“地址：…”）"}
        missing = [key for key in fields if not draft[key]]
        issues.extend(f"请补充{labels[key]}。" for key in missing)
        issues.append("请在商品列表明确选择鲜花或蛋糕；解析不会自动选择商品、预留资金或下单。")
        context["pending_birthday"] = draft
        set_context(conn, session_id, context)
        return {"status": "needs_clarification" if missing else "draft", "mode": "offline", "draft": draft,
                "message": "已整理生日计划草稿；请补齐信息并选择商品后再生成待确认计划。",
                "needs_review": list(dict.fromkeys(issues)), "missing_fields": missing}


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS life_products (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, merchant TEXT NOT NULL,
        version INTEGER NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents > 0),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
    )""")
    conn.executemany("INSERT OR IGNORE INTO life_products(id,kind,name,merchant,version,price_cents) VALUES (?,?,?,?,?,?)", CATALOG)
    conn.execute("""CREATE TABLE IF NOT EXISTS life_tasks (
        id TEXT PRIMARY KEY REFERENCES actions(id), session_id TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id), payload_json TEXT NOT NULL,
        reservation_id TEXT NOT NULL UNIQUE REFERENCES fund_reservations(id),
        birthday TEXT NOT NULL, order_at TEXT NOT NULL, order_expires_at TEXT NOT NULL,
        delivery_at TEXT NOT NULL, delivery_expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('scheduled','ordered','completed','cancelled','failed','expired')),
        failure_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS life_orders (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES life_tasks(id),
        product_id TEXT NOT NULL REFERENCES life_products(id), snapshot_json TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
        status TEXT NOT NULL CHECK(status IN ('placed','delivered','delivery_failed','cancelled')),
        transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(id),
        refund_transaction_id TEXT UNIQUE REFERENCES transactions(id),
        refunded_cents INTEGER NOT NULL DEFAULT 0 CHECK(refunded_cents>=0 AND refunded_cents<=amount_cents),
        placed_at TEXT NOT NULL, delivered_at TEXT, cancelled_at TEXT,
        UNIQUE(task_id,product_id)
    )""")


def _money(value: int) -> str:
    from .service import money
    return money(value)


def _product(row: sqlite3.Row) -> dict[str, Any]:
    return {**{key: row[key] for key in ("id", "kind", "name", "merchant", "version", "price_cents")},
            "price_yuan": _money(row["price_cents"]), "fee_cents": 0, "fee_yuan": "0.00"}


def catalog() -> dict[str, Any]:
    with db.db_session() as conn:
        rows = conn.execute("SELECT * FROM life_products WHERE active=1 ORDER BY kind,id").fetchall()
        return {"products": [_product(row) for row in rows if row["id"] in PRODUCT_IDS], "notice": NOTICE}


def _build(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    from .service import cents_from_yuan
    try:
        birthday = date.fromisoformat(data["birthday"])
        if birthday.isoformat() != data["birthday"]:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise HTTPException(422, "请提供 YYYY-MM-DD 格式的生日日期")
    now = business_now(conn)
    if birthday <= now.date():
        raise HTTPException(422, "生日必须是业务时钟之后的未来日期")
    order_at = datetime.combine(birthday - timedelta(days=2), time(9), SHANGHAI)
    if order_at <= now:
        raise HTTPException(422, "生日前两天 09:00 已到或已过，请选择更晚的生日日期")
    delivery_at = datetime.combine(birthday, time(9), SHANGHAI)
    raw_budget = data.get("budget_yuan")
    amount = cents_from_yuan(raw_budget) if isinstance(raw_budget, str) and re.fullmatch(r"\d+(?:\.\d{1,2})?", raw_budget.strip()) else None
    if amount is None:
        raise HTTPException(422, "预算须大于零且精确到分")
    for field, label, limit in (("goal", "生日目标", 120), ("recipient_label", "收货人", 60), ("delivery_note", "虚构配送信息", 200)):
        if not isinstance(data.get(field), str) or not data[field].strip() or len(data[field]) > limit:
            raise HTTPException(422, f"请明确填写{label}，不根据亲属称谓猜测联系人")
    ids = data.get("product_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids) or any(id not in PRODUCT_IDS for id in ids):
        raise HTTPException(422, "请选择 1 至 2 件目录商品，鲜花和蛋糕各最多一件")
    products = []
    for id in ids:
        product = conn.execute("SELECT * FROM life_products WHERE id=? AND active=1", (id,)).fetchone()
        if product is None:
            raise HTTPException(409, "商品已下架，请重新选择")
        products.append(_product(product))
    if len({product["kind"] for product in products}) != len(products):
        raise HTTPException(422, "鲜花和蛋糕各最多选择一件")
    total = sum(product["price_cents"] for product in products)
    if total > amount:
        raise HTTPException(422, "所选商品总额超过预算，请调整商品或预算")
    if amount > available_cents(conn):
        raise HTTPException(409, "可用余额不足以预留这笔生日预算")
    return {"goal": data["goal"].strip(), "recipient_label": data["recipient_label"].strip(),
            "delivery_note": data["delivery_note"].strip(), "birthday": birthday.isoformat(),
            "budget_cents": amount, "budget_yuan": _money(amount), "products": products,
            "total_cents": total, "total_yuan": _money(total), "fee_cents": 0, "fee_yuan": "0.00",
            "remaining_cents": amount - total, "remaining_yuan": _money(amount - total),
            "order_at": order_at.isoformat(timespec="seconds"),
            "order_expires_at": (order_at + timedelta(minutes=10)).isoformat(timespec="seconds"),
            "delivery_at": delivery_at.isoformat(timespec="seconds"),
            "delivery_expires_at": (delivery_at + timedelta(minutes=10)).isoformat(timespec="seconds"),
            "execution_window_minutes": 10,
            "steps": ["确认后预留预算", "生日前两天 09:00 模拟下单", "生日当天 09:00 模拟送达"],
            "notice": NOTICE}


def prepare(data: dict[str, Any]) -> dict[str, Any]:
    from .service import create_action, reply
    with db.db_session() as conn:
        payload = _build(conn, data)
        tier = "red" if payload["total_cents"] > 100000 else "yellow"
        action = create_action(conn, data["session_id"], "birthday_task", tier, payload)
        return reply("请核对收货人、虚构配送信息、商品与执行日期。确认后预留预算，到期按所列商品价格模拟下单。",
                     data["session_id"], "offline", pending_action=action)


def execute_create(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM life_tasks WHERE id=? AND session_id=?", (action_id, session_id)).fetchone()
    if existing is not None:
        return {"status": "completed", "action_id": action_id, "task_id": action_id, "message": "生日任务已建立，未重复预留。"}
    rebuilt = _build(conn, {**payload, "product_ids": [product["id"] for product in payload["products"]]})
    if rebuilt != payload:
        raise HTTPException(409, "生日计划、价格或商品版本已变化，请重新确认")
    action = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=? AND type='birthday_task'", (action_id, session_id)).fetchone()
    if action is None or json.loads(action["payload_json"]) != payload:
        raise HTTPException(409, "生日任务授权与计划不一致")
    if action["status"] != "pending" or datetime.fromisoformat(action["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(409, "生日任务授权已过期或失效，请重新生成计划")
    if payload["total_cents"] > 100000 and action["tier"] != "red":
        raise HTTPException(403, "该生日支出需要模拟强验证")
    require_verified(conn, action)
    reservation_id = f"life-{action_id}"
    reserve(conn, reservation_id, session_id, payload["budget_cents"], payload["goal"][:100])
    now = db.utc_now()
    conn.execute("""INSERT INTO life_tasks
        (id,session_id,account_id,payload_json,reservation_id,birthday,order_at,order_expires_at,
         delivery_at,delivery_expires_at,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,'scheduled',?,?)""", (
        action_id, session_id, db.ACCOUNT_ID, json.dumps(payload, ensure_ascii=False), reservation_id,
        payload["birthday"], payload["order_at"], payload["order_expires_at"], payload["delivery_at"],
        payload["delivery_expires_at"], now, now,
    ))
    db.audit(conn, session_id, "birthday_task_created", {"task_id": action_id, "reservation_id": reservation_id})
    return {"status": "completed", "action_id": action_id, "task_id": action_id,
            "message": f"生日任务已建立并预留 ¥{payload['budget_yuan']}，账面余额未扣款。"}


def _owned(conn: sqlite3.Connection, task_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM life_tasks WHERE id=? AND session_id=? AND account_id=?", (task_id, session_id, db.ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该生日任务")
    return row


def _order_public(row: sqlite3.Row) -> dict[str, Any]:
    snapshot = json.loads(row["snapshot_json"])
    return {**{key: row[key] for key in ("id", "task_id", "product_id", "status", "transaction_id", "refund_transaction_id", "placed_at", "delivered_at", "cancelled_at")},
            **snapshot, "amount_yuan": _money(row["amount_cents"]), "refunded_yuan": _money(row["refunded_cents"]),
            "can_cancel": row["status"] in ("placed", "delivery_failed")}


def public_task(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    orders = conn.execute("SELECT * FROM life_orders WHERE task_id=? ORDER BY id", (row["id"],)).fetchall()
    reservation = conn.execute("SELECT remaining_cents FROM fund_reservations WHERE id=?", (row["reservation_id"],)).fetchone()
    spent = sum(order["amount_cents"] for order in orders)
    refunded = sum(order["refunded_cents"] for order in orders)
    return {**payload, **{key: row[key] for key in ("id", "status", "reservation_id", "failure_reason", "created_at", "updated_at", "finished_at")},
            "reserved_remaining_yuan": _money(reservation["remaining_cents"] if reservation else 0),
            "spent_yuan": _money(spent), "refunded_yuan": _money(refunded), "net_spent_yuan": _money(spent - refunded),
            "orders": [_order_public(order) for order in orders], "can_cancel": row["status"] in ("scheduled", "ordered")}


def list_tasks(session_id: str) -> dict[str, Any]:
    with db.db_session() as conn:
        rows = conn.execute("SELECT * FROM life_tasks WHERE session_id=? AND account_id=? ORDER BY rowid DESC", (session_id, db.ACCOUNT_ID)).fetchall()
        return {"tasks": [public_task(conn, row) for row in rows], "notice": NOTICE}


def get_task(task_id: str, session_id: str) -> dict[str, Any]:
    with db.db_session() as conn:
        return public_task(conn, _owned(conn, task_id, session_id))


def prepare_cancel(task_id: str, session_id: str) -> dict[str, Any]:
    from .service import create_action, reply
    with db.db_session() as conn:
        row = _owned(conn, task_id, session_id)
        if row["status"] not in ("scheduled", "ordered"):
            raise HTTPException(409, "任务已结束，无需取消")
        detail = public_task(conn, row)
        payload = {"task_id": task_id, "goal": detail["goal"], "status": row["status"],
                   "reserved_remaining_yuan": detail["reserved_remaining_yuan"],
                   "order_ids": sorted(order["id"] for order in detail["orders"]),
                   "notice": "仅取消任务并释放未花预算。已下单商品不会退款或取消，需单独取消订单。"}
        action = create_action(conn, session_id, "birthday_cancel", "yellow", payload)
        return reply(payload["notice"], session_id, "offline", pending_action=action)


def _release_remaining(conn: sqlite3.Connection, task: sqlite3.Row) -> None:
    # Derive the id from the immutable action id, never trust a tampered index.
    reservation_id = f"life-{task['id']}"
    owned = conn.execute("SELECT 1 FROM fund_reservations WHERE id=? AND session_id=? AND account_id=?",
                         (reservation_id, task["session_id"], db.ACCOUNT_ID)).fetchone()
    if owned:
        release(conn, reservation_id, task["session_id"])


def execute_cancel(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = _owned(conn, payload["task_id"], session_id)
    if row["status"] == "cancelled":
        return {"status": "completed", "action_id": action_id, "task_id": row["id"], "message": "任务已取消，未重复处理。"}
    order_ids = sorted(item[0] for item in conn.execute("SELECT id FROM life_orders WHERE task_id=?", (row["id"],)))
    if row["status"] not in ("scheduled", "ordered") or row["status"] != payload["status"] or order_ids != payload["order_ids"]:
        raise HTTPException(409, "任务已发生下单或送达变化，请重新查看后取消")
    _release_remaining(conn, row)
    now = business_now(conn).isoformat(timespec="seconds")
    conn.execute("UPDATE life_tasks SET status='cancelled',updated_at=?,finished_at=? WHERE id=?", (now, now, row["id"]))
    db.audit(conn, session_id, "birthday_task_cancelled", {"task_id": row["id"], "orders_unchanged": order_ids})
    return {"status": "completed", "action_id": action_id, "task_id": row["id"],
            "message": "任务已取消，未花预算已释放。已下单商品未取消、未退款；可在订单明细单独处理。"}


def _order_owned(conn: sqlite3.Connection, order_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("""SELECT life_orders.* FROM life_orders JOIN life_tasks ON life_tasks.id=life_orders.task_id
        WHERE life_orders.id=? AND life_tasks.session_id=? AND life_tasks.account_id=?""", (order_id, session_id, db.ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该模拟订单")
    return row


def prepare_order_cancel(order_id: str, session_id: str) -> dict[str, Any]:
    from .service import create_action, reply
    with db.db_session() as conn:
        row = _order_owned(conn, order_id, session_id)
        if row["status"] not in ("placed", "delivery_failed"):
            raise HTTPException(409, "仅未送达且未取消的模拟订单可以取消退款")
        snapshot = json.loads(row["snapshot_json"])
        payload = {"order_id": order_id, "task_id": row["task_id"], "amount_cents": row["amount_cents"],
                   "amount_yuan": _money(row["amount_cents"]), "product_name": snapshot["product"]["name"],
                   "transaction_id": row["transaction_id"]}
        action = create_action(conn, session_id, "birthday_order_cancel", "yellow", payload)
        return reply("请确认取消这笔未送达的模拟订单。确认后原额退回模拟账户并生成退款回执。", session_id, "offline", pending_action=action)


def execute_order_cancel(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = _order_owned(conn, payload["order_id"], session_id)
    if row["amount_cents"] != payload["amount_cents"] or row["transaction_id"] != payload["transaction_id"]:
        raise HTTPException(409, "订单金额或原支付记录已变化，未退款")
    if row["status"] == "cancelled":
        return {"status": "completed", "action_id": action_id, "order_id": row["id"],
                "transaction_id": row["refund_transaction_id"], "message": "订单已取消并退款，未重复入账。"}
    if row["status"] not in ("placed", "delivery_failed"):
        raise HTTPException(409, "该订单已送达，不能取消退款")
    original = conn.execute("SELECT * FROM transactions WHERE id=? AND account_id=? AND direction='out'", (row["transaction_id"], db.ACCOUNT_ID)).fetchone()
    if original is None or original["amount_cents"] != row["amount_cents"]:
        raise HTTPException(409, "原支付流水无法核对，未退款")
    now = business_now(conn)
    refund_id = f"life-refund-{row['id']}"
    merchant = json.loads(row["snapshot_json"])["product"]["merchant"]
    credit(conn, row["amount_cents"])
    conn.execute("""INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note,action_id)
        VALUES (?,?,?,'in',?,?,'生日退款','未送达模拟订单取消退款',?)""",
                 (refund_id, db.ACCOUNT_ID, now.date().isoformat(), row["amount_cents"], merchant, refund_id))
    conn.execute("UPDATE life_orders SET status='cancelled',refund_transaction_id=?,refunded_cents=amount_cents,cancelled_at=? WHERE id=?",
                 (refund_id, now.isoformat(timespec="seconds"), row["id"]))
    remaining = conn.execute("SELECT COUNT(*) FROM life_orders WHERE task_id=? AND status!='cancelled'", (row["task_id"],)).fetchone()[0]
    if not remaining:
        conn.execute("UPDATE life_tasks SET status='cancelled',failure_reason='全部模拟订单已取消并退款',updated_at=?,finished_at=? WHERE id=? AND status='ordered'",
                     (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), row["task_id"]))
    db.audit(conn, session_id, "birthday_order_refunded", {"order_id": row["id"], "transaction_id": refund_id, "amount_cents": row["amount_cents"]})
    return {"status": "completed", "action_id": action_id, "order_id": row["id"], "transaction_id": refund_id,
            "message": f"模拟订单已取消，¥{_money(row['amount_cents'])} 已退回模拟账户。"}


def _authorized(conn: sqlite3.Connection, row: sqlite3.Row, payload: dict[str, Any]) -> bool:
    action = conn.execute("SELECT * FROM actions WHERE id=?", (row["id"],)).fetchone()
    return bool(action and action["type"] == "birthday_task" and action["status"] == "completed"
                and action["session_id"] == row["session_id"] and row["account_id"] == db.ACCOUNT_ID
                and json.loads(action["payload_json"]) == payload
                and action["tier"] == ("red" if payload["total_cents"] > 100000 else "yellow")
                and row["reservation_id"] == f"life-{row['id']}"
                and all(row[key] == payload[key] for key in ("birthday", "order_at", "order_expires_at", "delivery_at", "delivery_expires_at")))


def _fail(conn: sqlite3.Connection, row: sqlite3.Row, reason: str, status: str = "failed") -> None:
    _release_remaining(conn, row)
    now = business_now(conn).isoformat(timespec="seconds")
    conn.execute("UPDATE life_tasks SET status=?,failure_reason=?,updated_at=?,finished_at=? WHERE id=?", (status, reason, now, now, row["id"]))
    db.audit(conn, row["session_id"], f"birthday_task_{status}", {"task_id": row["id"], "reason": reason})


def _place_orders(conn: sqlite3.Connection, row: sqlite3.Row, payload: dict[str, Any]) -> None:
    for product in payload["products"]:
        current = conn.execute("SELECT * FROM life_products WHERE id=? AND active=1", (product["id"],)).fetchone()
        if product["id"] not in PRODUCT_IDS or current is None or _product(current) != product:
            raise HTTPException(409, "商品价格、版本或供应状态已变化，未下单；请重新确认计划")
    now = business_now(conn)
    for product in payload["products"]:
        order_id = f"life-{row['id']}-{product['id']}"
        transaction_id = f"life-purchase-{order_id}"
        snapshot = {"product": product, "recipient_label": payload["recipient_label"], "delivery_note": payload["delivery_note"],
                    "delivery_at": payload["delivery_at"], "fee_yuan": "0.00", "fee_cents": 0}
        consume(conn, row["reservation_id"], row["session_id"], product["price_cents"])
        conn.execute("""INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note,action_id)
            VALUES (?,?,?,'out',?,?,'生日采购',?,?)""", (transaction_id, db.ACCOUNT_ID, now.date().isoformat(), product["price_cents"],
                   product["merchant"], f"{payload['goal']}：{product['name']}", transaction_id))
        conn.execute("""INSERT INTO life_orders(id,task_id,product_id,snapshot_json,amount_cents,status,transaction_id,placed_at)
            VALUES (?,?,?,?,?,'placed',?,?)""", (order_id, row["id"], product["id"], json.dumps(snapshot, ensure_ascii=False),
                   product["price_cents"], transaction_id, now.isoformat(timespec="seconds")))
    _release_remaining(conn, row)
    conn.execute("UPDATE life_tasks SET status='ordered',updated_at=? WHERE id=?", (now.isoformat(timespec="seconds"), row["id"]))
    db.audit(conn, row["session_id"], "birthday_orders_placed", {"task_id": row["id"], "amount_cents": payload["total_cents"]})


def run_due_tasks(conn: sqlite3.Connection) -> int:
    if not conn.in_transaction:
        raise RuntimeError("Birthday task execution requires caller-owned transaction")
    now = business_now(conn)
    timestamp = now.isoformat(timespec="seconds")
    processed = 0
    tasks = conn.execute("SELECT * FROM life_tasks WHERE status='scheduled' AND order_at<=? ORDER BY order_at,id", (timestamp,)).fetchall()
    for row in tasks:
        payload = json.loads(row["payload_json"])
        processed += 1
        if not _authorized(conn, row, payload):
            _fail(conn, row, "计划或执行日期与原授权不一致，未下单")
        elif now >= datetime.fromisoformat(row["order_expires_at"]):
            _fail(conn, row, "已超过下单时间的 10 分钟窗口，未补扣款，剩余预留已释放", "expired")
        else:
            conn.execute("SAVEPOINT birthday_order_stage")
            try:
                _place_orders(conn, row, payload)
                conn.execute("RELEASE birthday_order_stage")
            except HTTPException as exc:
                conn.execute("ROLLBACK TO birthday_order_stage")
                conn.execute("RELEASE birthday_order_stage")
                _fail(conn, row, str(exc.detail))
    # Already placed orders survive task cancellation; order cancellation is a
    # distinct action with a separate, exactly-once refund ledger entry.
    deliveries = conn.execute("""SELECT DISTINCT life_tasks.* FROM life_tasks JOIN life_orders ON life_tasks.id=life_orders.task_id
        WHERE life_orders.status='placed' AND life_tasks.delivery_at<=? ORDER BY life_tasks.delivery_at,life_tasks.id""", (timestamp,)).fetchall()
    for row in deliveries:
        payload = json.loads(row["payload_json"])
        processed += 1
        authorized = _authorized(conn, row, payload)
        within_window = now < datetime.fromisoformat(row["delivery_expires_at"])
        if not authorized or not within_window:
            reason = "送达计划与授权不一致" if not authorized else "已超过送达时间的 10 分钟模拟窗口，订单未标为送达，可单独取消退款"
            conn.execute("UPDATE life_orders SET status='delivery_failed' WHERE task_id=? AND status='placed'", (row["id"],))
            if row["status"] != "cancelled":
                _fail(conn, row, reason)
        else:
            conn.execute("UPDATE life_orders SET status='delivered',delivered_at=? WHERE task_id=? AND status='placed'", (timestamp, row["id"]))
            if row["status"] == "ordered":
                conn.execute("UPDATE life_tasks SET status='completed',updated_at=?,finished_at=? WHERE id=?", (timestamp, timestamp, row["id"]))
            db.audit(conn, row["session_id"], "birthday_orders_delivered", {"task_id": row["id"], "mode": "simulated"})
    return processed
