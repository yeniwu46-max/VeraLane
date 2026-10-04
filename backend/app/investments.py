"""Fictional fixed-NAV investments with suitability and deferred cash settlement."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP
import json
import re
import secrets
import sqlite3
from typing import Any

from fastapi import HTTPException

from .clock import business_date, business_now
from .db import ACCOUNT_ID, audit, db_session, utc_now
from .execution_controls import available_cents, debit
from .service import cents_from_yuan, create_action, money


ASSESSMENT_VERSION = "risk-demo-1.0"
NOTICE = "全部产品、净值和测评均为虚构演示，不构成实际投资建议；固定演示净值不代表保本或保证收益。"
QUESTIONS = [
    {"id": "loss", "title": "对于这笔资金，你能接受的本金损失范围是？", "options": [{"value": 1, "label": "不能承受损失"}, {"value": 2, "label": "可承受少量损失"}, {"value": 3, "label": "可承受较大损失"}]},
    {"id": "experience", "title": "你的投资经验是？", "options": [{"value": 1, "label": "没有经验"}, {"value": 2, "label": "了解基础产品"}, {"value": 3, "label": "了解波动和交易风险"}]},
    {"id": "income", "title": "收入或现金流稳定程度是？", "options": [{"value": 1, "label": "不稳定"}, {"value": 2, "label": "基本稳定"}, {"value": 3, "label": "稳定且有充足结余"}]},
    {"id": "reserve", "title": "生活应急资金是否已经另行保留？", "options": [{"value": 1, "label": "尚未保留"}, {"value": 2, "label": "有部分储备"}, {"value": 3, "label": "已有充足储备"}]},
    {"id": "reaction", "title": "面对净值下降，你更可能如何处理？", "options": [{"value": 1, "label": "立即退出"}, {"value": 2, "label": "复核后决定"}, {"value": 3, "label": "理解风险后承受波动"}]},
]


def init_schema(conn: sqlite3.Connection) -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS investment_products (
            id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,version INTEGER NOT NULL,
            risk_level INTEGER NOT NULL CHECK(risk_level BETWEEN 1 AND 3),
            nav_10000 INTEGER NOT NULL CHECK(nav_10000 > 0),min_purchase_cents INTEGER NOT NULL CHECK(min_purchase_cents > 0),
            lock_days INTEGER NOT NULL CHECK(lock_days >= 0),redemption_days INTEGER NOT NULL CHECK(redemption_days > 0),
            buy_fee_bps INTEGER NOT NULL CHECK(buy_fee_bps BETWEEN 0 AND 1000),redeem_fee_bps INTEGER NOT NULL CHECK(redeem_fee_bps BETWEEN 0 AND 1000),
            status TEXT NOT NULL CHECK(status IN ('active','suspended')))""",
        """CREATE TABLE IF NOT EXISTS risk_assessments (
            id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),session_id TEXT NOT NULL,
            version TEXT NOT NULL,answers_json TEXT NOT NULL,score INTEGER NOT NULL,risk_level INTEGER NOT NULL CHECK(risk_level BETWEEN 1 AND 3),
            assessed_on TEXT NOT NULL,expires_on TEXT NOT NULL,created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS investment_holdings (
            id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),session_id TEXT NOT NULL,
            product_id TEXT NOT NULL REFERENCES investment_products(id),units_10000 INTEGER NOT NULL CHECK(units_10000 >= 0),
            acquired_on TEXT NOT NULL,unlock_on TEXT NOT NULL,source_action_id TEXT NOT NULL UNIQUE REFERENCES actions(id))""",
        """CREATE TABLE IF NOT EXISTS investment_orders (
            id TEXT PRIMARY KEY,action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),account_id TEXT NOT NULL REFERENCES accounts(id),session_id TEXT NOT NULL,
            product_id TEXT NOT NULL REFERENCES investment_products(id),holding_id TEXT NOT NULL REFERENCES investment_holdings(id),
            kind TEXT NOT NULL CHECK(kind IN ('buy','redeem')),units_10000 INTEGER NOT NULL CHECK(units_10000 > 0),
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),fee_cents INTEGER NOT NULL CHECK(fee_cents >= 0),
            status TEXT NOT NULL CHECK(status IN ('completed','pending_settlement')),settles_on TEXT,transaction_id TEXT UNIQUE REFERENCES transactions(id),
            snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL,settled_at TEXT)""",
        "CREATE INDEX IF NOT EXISTS investment_due ON investment_orders(status,settles_on)",
    ]
    for statement in statements:
        conn.execute(statement)
    conn.executemany("INSERT OR IGNORE INTO investment_products VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
        ("product-stable", "稳享", 1, 1, 10000, 10000, 0, 1, 0, 0, "active"),
        ("product-balanced", "平衡", 1, 2, 12500, 100000, 7, 1, 10, 5, "active"),
        ("product-growth", "进取", 1, 3, 20000, 100000, 30, 1, 20, 10, "active"),
    ])


def questions() -> dict[str, Any]:
    return {"version": ASSESSMENT_VERSION, "questions": QUESTIONS, "notice": NOTICE,
            "scoring": "5 题各 1–3 分，5–7 分为 1 级，8–11 分为 2 级，12–15 分为 3 级；本金损失承受题为 1 时最高 1 级。"}


def _assessment(conn: sqlite3.Connection, session_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM risk_assessments WHERE account_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1", (ACCOUNT_ID, session_id)).fetchone()
    if row is None:
        return None
    return {key: row[key] for key in ("id", "version", "score", "risk_level", "assessed_on", "expires_on")} | {"valid": row["assessed_on"] <= business_date(conn) < row["expires_on"]}


def assess(session_id: str, answers: list[int]) -> dict[str, Any]:
    if len(answers) != 5 or any(type(value) is not int or value not in (1, 2, 3) for value in answers):
        raise HTTPException(422, "请完成全部 5 题，每题选择 1、2 或 3")
    score = sum(answers)
    level = 1 if score <= 7 else 2 if score <= 11 else 3
    if answers[0] == 1:
        level = 1
    with db_session() as conn:
        today = date.fromisoformat(business_date(conn))
        assessment_id = "risk-" + secrets.token_urlsafe(15)
        conn.execute("INSERT INTO risk_assessments VALUES(?,?,?,?,?,?,?,?,?,?)", (assessment_id, ACCOUNT_ID, session_id, ASSESSMENT_VERSION,
                     json.dumps(answers), score, level, str(today), str(today + timedelta(days=30)), utc_now()))
        audit(conn, session_id, "risk_assessment_completed", {"assessment_id": assessment_id, "version": ASSESSMENT_VERSION, "risk_level": level})
        return {"assessment": _assessment(conn, session_id), "message": f"模拟测评结果为 {level} 级，有效期 30 个演示日。", "notice": NOTICE}


def _product(conn: sqlite3.Connection, product_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM investment_products WHERE id=?", (product_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该虚构理财产品")
    return row


def _product_public(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in ("id", "name", "version", "risk_level", "lock_days", "redemption_days", "buy_fee_bps", "redeem_fee_bps", "status")} | {
        "nav_yuan": f"{Decimal(row['nav_10000']) / 10000:.4f}", "min_purchase_yuan": money(row["min_purchase_cents"])}


def _match_reasons(product: sqlite3.Row, assessment: dict | None, horizon_days: int, liquidity_days: int) -> list[str]:
    reasons = []
    if not assessment or not assessment["valid"]:
        reasons.append("请先完成有效期内的风险测评")
    elif product["risk_level"] > assessment["risk_level"]:
        reasons.append("产品风险等级高于本次测评等级")
    if product["status"] != "active":
        reasons.append("产品暂停申购")
    if horizon_days < product["lock_days"]:
        reasons.append(f"计划持有 {horizon_days} 天短于 {product['lock_days']} 天锁定期")
    if liquidity_days < product["redemption_days"]:
        reasons.append(f"赎回需 {product['redemption_days']} 个演示工作日，超过可接受到账时间")
    return reasons


def products(session_id: str, horizon_days: int = 30, liquidity_days: int = 2) -> dict[str, Any]:
    with db_session() as conn:
        assessment = _assessment(conn, session_id)
        result = []
        for product in conn.execute("SELECT * FROM investment_products ORDER BY risk_level,id"):
            reasons = _match_reasons(product, assessment, horizon_days, liquidity_days)
            result.append(_product_public(product) | {"matched": not reasons, "reasons": reasons or ["风险等级、计划期限和赎回到账要求均符合当前条件；仍需自主选择。"]})
        return {"products": result, "assessment": assessment, "notice": NOTICE}


def _units(value: str) -> int:
    try:
        units = Decimal(value)
    except InvalidOperation as exc:
        raise HTTPException(422, "份额须为有效数字") from exc
    if not units.is_finite() or units <= 0 or units.as_tuple().exponent < -4 or units > 1_000_000_000:
        raise HTTPException(422, "份额须大于零且最多保留 4 位小数")
    return int(units * 10000)


def _units_text(units: int) -> str:
    return f"{Decimal(units) / 10000:.4f}"


def _fee(cents: int, basis_points: int) -> int:
    return int((Decimal(cents) * basis_points / 10000).to_integral_value(rounding=ROUND_UP))


def _next_workday(today: date, days: int) -> date:
    while days:
        today += timedelta(days=1)
        if today.weekday() < 5:
            days -= 1
    return today


def _holding(conn: sqlite3.Connection, holding_id: str, session_id: str) -> sqlite3.Row:
    holding = conn.execute("SELECT * FROM investment_holdings WHERE id=? AND account_id=? AND session_id=?", (holding_id, ACCOUNT_ID, session_id)).fetchone()
    if holding is None:
        raise HTTPException(404, "未找到该会话的理财持仓")
    return holding


def _buy_payload(conn: sqlite3.Connection, session_id: str, product_id: str, amount_yuan: str, horizon_days: int, liquidity_days: int) -> dict:
    product = _product(conn, product_id)
    assessment = _assessment(conn, session_id)
    reasons = _match_reasons(product, assessment, horizon_days, liquidity_days)
    if reasons:
        raise HTTPException(409, "；".join(reasons))
    amount = cents_from_yuan(amount_yuan)
    if amount is None:
        raise HTTPException(422, "请输入大于零、精确到分且不超过 100000 元的申购金额")
    if amount < product["min_purchase_cents"]:
        raise HTTPException(422, f"该产品起购金额为 ¥{money(product['min_purchase_cents'])}")
    if amount > available_cents(conn):
        raise HTTPException(409, "可用余额不足，不能使用预留资金申购")
    fee = _fee(amount, product["buy_fee_bps"])
    units = (amount - fee) * 1_000_000 // product["nav_10000"]
    if units <= 0:
        raise HTTPException(422, "扣除费用后不足以获得最小份额")
    return {"product": _product_public(product), "product_snapshot": dict(product), "assessment": assessment,
            "amount_cents": amount, "amount_yuan": money(amount), "fee_cents": fee, "fee_yuan": money(fee), "units_10000": units, "units": _units_text(units),
            "horizon_days": horizon_days, "liquidity_days": liquidity_days, "settles_on": None,
            "rounding_policy": "申购金额包含手续费；费率按分向上取整，份额按万分之一份向下取整，尾差不增加份额。"}


def prepare_buy(session_id: str, product_id: str, amount_yuan: str, horizon_days: int = 30, liquidity_days: int = 2) -> dict:
    with db_session() as conn:
        payload = _buy_payload(conn, session_id, product_id, amount_yuan, horizon_days, liquidity_days)
        action = create_action(conn, session_id, "investment_buy", "red", payload)
        return {"session_id": session_id, "mode": "offline", "message": "请核对产品、费用和份额，并完成与本操作绑定的模拟强验证后确认申购。", "pending_action": action}


def _redeem_payload(conn: sqlite3.Connection, session_id: str, holding_id: str, units_text: str) -> dict:
    holding = _holding(conn, holding_id, session_id)
    product = _product(conn, holding["product_id"])
    if holding["unlock_on"] > business_date(conn):
        raise HTTPException(409, f"该笔持仓锁定至 {holding['unlock_on']}，目前不可赎回")
    units = _units(units_text)
    if units > holding["units_10000"]:
        raise HTTPException(409, "可赎回份额不足")
    gross = units * product["nav_10000"] // 1_000_000
    fee = _fee(gross, product["redeem_fee_bps"])
    proceeds = gross - fee
    if proceeds <= 0:
        raise HTTPException(422, "本次赎回扣费后不足一分，请增加赎回份额")
    if proceeds > 10_000_000:
        raise HTTPException(422, "单笔模拟赎回到账上限为 100000 元，请减少份额分次赎回")
    settles = _next_workday(date.fromisoformat(business_date(conn)), product["redemption_days"])
    return {"holding_id": holding_id, "product": _product_public(product), "product_snapshot": dict(product), "assessment": _assessment(conn, session_id),
            "units_10000": units, "units": _units_text(units), "gross_yuan": money(gross), "fee_cents": fee, "fee_yuan": money(fee),
            "amount_cents": proceeds, "amount_yuan": money(proceeds), "settles_on": str(settles), "settles_at": f"{settles}T09:00:00+08:00", "requested_on": business_date(conn),
            "rounding_policy": "赎回总额向下取整到分，手续费向上取整到分；到账前资金不可使用。演示工作日只排除周末，不模拟法定节假日。"}


def prepare_redeem(session_id: str, holding_id: str, units: str) -> dict:
    with db_session() as conn:
        payload = _redeem_payload(conn, session_id, holding_id, units)
        action = create_action(conn, session_id, "investment_redeem", "red", payload)
        return {"session_id": session_id, "mode": "offline", "message": "请核对赎回份额、费用和预计到账日，并完成模拟强验证。确认只扣减持仓份额，到账后才增加余额。", "pending_action": action}


def _verify_product(conn: sqlite3.Connection, payload: dict) -> sqlite3.Row:
    product = _product(conn, payload["product"]["id"])
    if dict(product) != payload.get("product_snapshot"):
        raise HTTPException(409, "产品版本、净值或费用已变化，请重新核对")
    return product


def execute_buy(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    product = _verify_product(conn, payload)
    fresh = _buy_payload(conn, session_id, product["id"], payload["amount_yuan"], payload["horizon_days"], payload["liquidity_days"])
    if fresh != payload:
        raise HTTPException(409, "测评或申购参数已变化，请重新核对并验证")
    debit(conn, payload["amount_cents"])
    today = date.fromisoformat(business_date(conn))
    holding_id = "holding-" + action_id
    unlock = today + timedelta(days=product["lock_days"])
    conn.execute("INSERT INTO investment_holdings VALUES(?,?,?,?,?,?,?,?)", (holding_id, ACCOUNT_ID, session_id, product["id"], payload["units_10000"], str(today), str(unlock), action_id))
    transaction_id = "investment-buy-" + action_id
    conn.execute("INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note,action_id) VALUES(?,?,?,'out',?,?,'理财申购',?,?)",
                 (transaction_id, ACCOUNT_ID, str(today), payload["amount_cents"], product["name"], f"虚构产品申购 {payload['units']} 份，费用 ¥{payload['fee_yuan']}", transaction_id))
    order_id = "investment-" + action_id
    conn.execute("INSERT INTO investment_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (order_id, action_id, ACCOUNT_ID, session_id, product["id"], holding_id, "buy", payload["units_10000"], payload["amount_cents"], payload["fee_cents"], "completed", None, transaction_id, json.dumps(payload, ensure_ascii=False), utc_now(), utc_now()))
    audit(conn, session_id, "investment_bought", {"action_id": action_id, "holding_id": holding_id, "transaction_id": transaction_id})
    return {"status": "completed", "action_id": action_id, "order_id": order_id, "holding_id": holding_id, "transaction_id": transaction_id,
            "units": payload["units"], "amount_yuan": payload["amount_yuan"], "message": f"已模拟申购 {product['name']} {payload['units']} 份，总扣款 ¥{payload['amount_yuan']}（含费用）。"}


def execute_redeem(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    _verify_product(conn, payload)
    fresh = _redeem_payload(conn, session_id, payload["holding_id"], payload["units"])
    # A reassessment does not prevent exiting a position, but all financial
    # parameters and the quoted settlement date remain bound to consent.
    if {k: v for k, v in fresh.items() if k != "assessment"} != {k: v for k, v in payload.items() if k != "assessment"}:
        raise HTTPException(409, "赎回金额或到账日期已变化，请重新核对并验证")
    changed = conn.execute("UPDATE investment_holdings SET units_10000=units_10000-? WHERE id=? AND account_id=? AND session_id=? AND units_10000>=?",
                           (payload["units_10000"], payload["holding_id"], ACCOUNT_ID, session_id, payload["units_10000"])).rowcount
    if changed != 1:
        raise HTTPException(409, "可赎回份额不足，未创建赎回订单")
    order_id = "investment-" + action_id
    conn.execute("INSERT INTO investment_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (order_id, action_id, ACCOUNT_ID, session_id, payload["product"]["id"], payload["holding_id"], "redeem", payload["units_10000"], payload["amount_cents"], payload["fee_cents"], "pending_settlement", payload["settles_on"], None, json.dumps(payload, ensure_ascii=False), utc_now(), None))
    audit(conn, session_id, "investment_redemption_queued", {"action_id": action_id, "order_id": order_id, "settles_on": payload["settles_on"]})
    return {"status": "completed", "settlement_status": "pending_settlement", "action_id": action_id, "order_id": order_id, "holding_id": payload["holding_id"],
            "units": payload["units"], "amount_yuan": payload["amount_yuan"], "settles_on": payload["settles_on"],
            "message": f"已受理模拟赎回 {payload['units']} 份，预计 {payload['settles_on']} 09:00 到账 ¥{payload['amount_yuan']}；当前余额尚未增加。"}


def run_due_settlements(conn: sqlite3.Connection) -> int:
    from .execution_controls import credit
    if not conn.in_transaction:
        raise RuntimeError("Settlement requires a caller-owned transaction")
    rows = conn.execute("SELECT * FROM investment_orders WHERE account_id=? AND kind='redeem' AND status='pending_settlement' AND settles_on<=? ORDER BY settles_on,id", (ACCOUNT_ID, business_date(conn))).fetchall()
    now = business_now(conn)
    rows = [order for order in rows if order["settles_on"] < str(now.date()) or now.hour >= 9]
    for order in rows:
        transaction_id = "investment-settlement-" + order["id"]
        if conn.execute("SELECT 1 FROM transactions WHERE id=?", (transaction_id,)).fetchone():
            raise HTTPException(409, "赎回订单与入账记录状态不一致，请人工核对")
        snapshot = json.loads(order["snapshot_json"])
        credit(conn, order["amount_cents"])
        conn.execute("INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note,action_id) VALUES(?,?,?,'in',?,?,'理财赎回',?,?)",
                     (transaction_id, ACCOUNT_ID, business_date(conn), order["amount_cents"], snapshot["product"]["name"], f"虚构产品赎回到账 {snapshot['units']} 份", transaction_id))
        conn.execute("UPDATE investment_orders SET status='completed',transaction_id=?,settled_at=? WHERE id=? AND status='pending_settlement'", (transaction_id, utc_now(), order["id"]))
        audit(conn, order["session_id"], "investment_redemption_settled", {"order_id": order["id"], "transaction_id": transaction_id})
    return len(rows)


def portfolio(session_id: str) -> dict[str, Any]:
    with db_session() as conn:
        holdings = []
        for holding in conn.execute("SELECT * FROM investment_holdings WHERE account_id=? AND session_id=? ORDER BY rowid DESC", (ACCOUNT_ID, session_id)):
            product = _product(conn, holding["product_id"])
            holdings.append({"id": holding["id"], "product_id": product["id"], "product_name": product["name"], "units": _units_text(holding["units_10000"]),
                             "redeemable_units": _units_text(holding["units_10000"] if holding["unlock_on"] <= business_date(conn) else 0),
                             "nav_yuan": _product_public(product)["nav_yuan"], "estimated_value_yuan": money(holding["units_10000"] * product["nav_10000"] // 1_000_000),
                             "acquired_on": holding["acquired_on"], "unlock_on": holding["unlock_on"]})
        orders = []
        for order in conn.execute("SELECT * FROM investment_orders WHERE account_id=? AND session_id=? ORDER BY rowid DESC", (ACCOUNT_ID, session_id)):
            snapshot = json.loads(order["snapshot_json"])
            orders.append({key: order[key] for key in ("id", "kind", "status", "settles_on", "transaction_id", "created_at")} |
                          {"product_name": snapshot["product"]["name"], "amount_yuan": money(order["amount_cents"]), "fee_yuan": money(order["fee_cents"]), "units": _units_text(order["units_10000"]), "holding_id": order["holding_id"]})
        return {"assessment": _assessment(conn, session_id), "available_yuan": money(available_cents(conn)), "holdings": holdings, "orders": orders, "notice": NOTICE}


def interpret(session_id: str, message: str) -> dict[str, Any]:
    def clarification(text: str, **extra) -> dict:
        return {"status": "needs_clarification", "mode": "offline", "message": text, **extra}
    message = message.strip().strip("。！？?!")
    if re.search(r"申购|买入", message) and re.search(r"赎回|卖出", message):
        return clarification("请一次描述一项理财操作，先选择申购或赎回。")
    if re.fullmatch(r"(?:请|帮我)?\s*(?:比较|对比|推荐|看看)(?:一下)?\s*理财(?:产品)?", message):
        return {"status": "ok", "mode": "offline", "message": "以下为虚构产品对比；默认持有 30 天、接受 2 个演示工作日到账，可在页面调整后重新筛选。", **products(session_id)}
    with db_session() as conn:
        product_rows = list(conn.execute("SELECT * FROM investment_products"))
        matched = [row for row in product_rows if row["name"] in message]
    if len(matched) != 1:
        return clarification("请明确一个产品：稳享、平衡或进取。", choices=[_product_public(row) for row in product_rows])
    product = matched[0]
    if re.search(r"申购|买入", message):
        if not re.fullmatch(r"(?:请|帮我)?\s*(?:申购|买入)\s*" + re.escape(product["name"]) + r"\s*\d+(?:\.\d+)?\s*元", message):
            return clarification("请使用“申购稳享 100 元”等明确表达。持有期限和可接受到账时间请在页面填写，不支持预约理财操作。")
        amounts = re.findall(r"(?<![-\d.])(\d+(?:\.\d+)?)\s*元", message)
        if len(amounts) != 1:
            return clarification("请明确一个申购金额，例如“申购稳享 100 元”。")
        try:
            return {"status": "ok", **prepare_buy(session_id, product["id"], amounts[0])}
        except HTTPException as exc:
            if exc.status_code in (409, 422):
                return clarification(str(exc.detail))
            raise
    if re.search(r"赎回|卖出", message):
        if not re.fullmatch(r"(?:请|帮我)?\s*(?:赎回|卖出)\s*" + re.escape(product["name"]) + r"\s*\d+(?:\.\d+)?\s*份", message):
            return clarification("请使用“赎回稳享 10 份”等明确表达；不支持预约或多项合并理财操作。")
        amounts = re.findall(r"(?<![-\d.])(\d+(?:\.\d+)?)\s*份", message)
        if len(amounts) != 1:
            return clarification("请明确赎回份额，例如“赎回稳享 10 份”。")
        with db_session() as conn:
            holdings = list(conn.execute("SELECT id FROM investment_holdings WHERE account_id=? AND session_id=? AND product_id=? AND units_10000>0", (ACCOUNT_ID, session_id, product["id"])))
        if len(holdings) != 1:
            return clarification("没有唯一对应的持仓，请在持仓列表选择具体批次后赎回。", choices=[dict(row) for row in holdings])
        try:
            return {"status": "ok", **prepare_redeem(session_id, holdings[0]["id"], amounts[0])}
        except HTTPException as exc:
            if exc.status_code in (409, 422):
                return clarification(str(exc.detail))
            raise
    return clarification("可以说“比较理财产品”“申购稳享 100 元”或“赎回稳享 10 份”。")
