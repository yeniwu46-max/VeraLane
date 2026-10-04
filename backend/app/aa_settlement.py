from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException

from .clock import business_date, business_now
from .db import ACCOUNT_ID, USER_ID, audit, db_session, utc_now
from .execution_controls import available_cents, credit, debit, requires_red_tier
from .service import contact_fingerprint, create_action, mask_phone, money, reply

MAX_TOTAL_CENTS = 10_000_000
MAX_EXPENSES = 100


def _cents(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
            raise ValueError
        cents = int(amount * 100)
        if cents <= 0 or cents > MAX_TOTAL_CENTS:
            raise ValueError
        return cents
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, "每笔垫付金额须大于 0、精确到分，且不超过 ¥100,000") from None


def calculate_preview(participants: list[dict], expenses: list[dict], note: str = "") -> dict:
    if not 2 <= len(participants) <= 8:
        raise HTTPException(422, "结算参与人须为 2–8 人，包含本人")
    participant_ids = [item.get("id") for item in participants]
    if len(set(participant_ids)) != len(participant_ids) or "self" not in participant_ids:
        raise HTTPException(422, "参与人必须包含本人且不能重复")
    if not 1 <= len(expenses) <= MAX_EXPENSES:
        raise HTTPException(422, "请提供 1–100 笔垫付记录")

    by_id = {item["id"]: item for item in participants}
    paid = {participant_id: 0 for participant_id in participant_ids}
    normalized_expenses = []
    total = 0
    for expense in expenses:
        payer_id = expense.get("payer_id")
        if payer_id not in by_id:
            raise HTTPException(422, "每笔支出都必须指定本次结算参与人")
        cents = _cents(expense.get("amount_yuan", ""))
        total += cents
        if total > MAX_TOTAL_CENTS:
            raise HTTPException(422, "垫付总额不能超过 ¥100,000")
        paid[payer_id] += cents
        normalized_expenses.append({"payer_id": payer_id, "payer_name": by_id[payer_id]["name"],
                                    "amount_yuan": money(cents), "note": (expense.get("note") or "")[:100]})

    share, remainder = divmod(total, len(participants))
    participant_results = []
    net = {}
    for index, participant in enumerate(participants):
        share_cents = share + (1 if index < remainder else 0)
        balance_cents = paid[participant["id"]] - share_cents
        net[participant["id"]] = balance_cents
        participant_results.append({
            **participant,
            "paid_cents": paid[participant["id"]], "paid_yuan": money(paid[participant["id"]]),
            "share_cents": share_cents, "share_yuan": money(share_cents),
            "net_cents": balance_cents, "net_yuan": money(balance_cents),
            "direction": "receive" if balance_cents > 0 else "pay" if balance_cents < 0 else "settled",
        })

    debtors = [[person_id, -amount] for person_id, amount in net.items() if amount < 0]
    creditors = [[person_id, amount] for person_id, amount in net.items() if amount > 0]
    transfers = []
    debtor_index = creditor_index = 0
    while debtor_index < len(debtors) and creditor_index < len(creditors):
        debtor_id, debt = debtors[debtor_index]
        creditor_id, credit = creditors[creditor_index]
        amount = min(debt, credit)
        transfers.append({"from_id": debtor_id, "from_name": by_id[debtor_id]["name"],
                          "to_id": creditor_id, "to_name": by_id[creditor_id]["name"],
                          "amount_cents": amount, "amount_yuan": money(amount)})
        debtors[debtor_index][1] -= amount
        creditors[creditor_index][1] -= amount
        if debtors[debtor_index][1] == 0:
            debtor_index += 1
        if creditors[creditor_index][1] == 0:
            creditor_index += 1

    if any(value for _, value in debtors[debtor_index:]) or any(value for _, value in creditors[creditor_index:]):
        raise HTTPException(500, "结算金额无法平衡，请检查输入")
    return {"note": (note.strip() or "多人垫付结算")[:100], "total_cents": total, "total_yuan": money(total),
            "expense_count": len(normalized_expenses), "participants": participant_results,
            "expenses": normalized_expenses,
            "transfers": transfers,
            "notice": "这是本地计算的结算建议，不会发起转账、发送催收消息或改变任何账户余额。"}


def _inputs_for_session(conn, contact_ids: list[str], expenses: list[dict], note: str = "") -> tuple[dict, dict]:
    if not 1 <= len(contact_ids) <= 7 or len(set(contact_ids)) != len(contact_ids):
        raise HTTPException(422, "请选择 1–7 位不重复的已验证联系人，连同本人共 2–8 人")
    participants = [{"id": "self", "name": "我"}]
    fingerprints = {}
    for contact_id in contact_ids:
        contact = conn.execute("SELECT * FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
                               (contact_id, USER_ID)).fetchone()
        if contact is None:
            raise HTTPException(422, "参与人必须来自已验证联系人")
        participants.append({"id": contact["id"], "name": contact["name"]})
        fingerprints[contact["id"]] = contact_fingerprint(contact)
    preview = calculate_preview(participants, expenses, note)
    return preview, fingerprints


def preview_for_session(contact_ids: list[str], expenses: list[dict], note: str = "") -> dict:
    with db_session() as conn:
        preview, _ = _inputs_for_session(conn, contact_ids, expenses, note)
        return preview


def prepare_for_session(session_id: str, contact_ids: list[str], expenses: list[dict], note: str = "") -> dict:
    with db_session(immediate=True) as conn:
        preview, fingerprints = _inputs_for_session(conn, contact_ids, expenses, note)
        payload = {"contact_ids": contact_ids, "expenses": expenses, "note": note,
                   "preview": preview, "contact_fingerprints": fingerprints}
        action = create_action(conn, session_id, "aa_settlement", "yellow", payload)
        return reply("请核对多人垫付明细与净额路径。确认只会保存结算计划，不会自动转账。",
                     session_id, "offline", pending_action=action)


def _validate_plan(conn, session_id: str, settlement) -> dict:
    if settlement is None or settlement["session_id"] != session_id:
        raise HTTPException(404, "未找到当前会话的结算计划")
    payload = json.loads(settlement["payload_json"])
    preview, fingerprints = _inputs_for_session(conn, payload["contact_ids"], payload["expenses"], payload["note"])
    if preview != payload["preview"] or fingerprints != payload["contact_fingerprints"]:
        raise HTTPException(409, "参与人或垫付计划已变化，请重新创建结算计划")
    return payload


def execute_create(conn, action_id: str, session_id: str, payload: dict) -> dict:
    preview, fingerprints = _inputs_for_session(conn, payload["contact_ids"], payload["expenses"], payload["note"])
    fresh = {"contact_ids": payload["contact_ids"], "expenses": payload["expenses"], "note": payload["note"],
             "preview": preview, "contact_fingerprints": fingerprints}
    if payload != fresh:
        raise HTTPException(409, "参与人、金额或结算路径已变化，请重新核对")
    settlement_id = f"aa-settlement-{action_id}"
    executable = [item for item in preview["transfers"] if "self" in (item["from_id"], item["to_id"])]
    status = "owner_actions_complete" if not executable else "pending"
    conn.execute("INSERT INTO aa_settlements(id,session_id,status,payload_json,created_at) VALUES(?,?,?,?,?)",
                 (settlement_id, session_id, status, json.dumps(payload, ensure_ascii=False), utc_now()))
    for position, transfer in enumerate(preview["transfers"]):
        leg_status = "pending" if "self" in (transfer["from_id"], transfer["to_id"]) else "external"
        conn.execute("""INSERT INTO aa_settlement_legs(id,settlement_id,position,from_id,to_id,amount_cents,status)
            VALUES(?,?,?,?,?,?,?)""",
            (f"{settlement_id}-leg-{position}", settlement_id, position,
             transfer["from_id"], transfer["to_id"], transfer["amount_cents"], leg_status))
    audit(conn, session_id, "aa_settlement_plan_created", {"settlement_id": settlement_id,
          "amount_yuan": preview["total_yuan"], "leg_count": len(preview["transfers"]),
          "owner_action_count": len(executable), "simulated": True})
    return {"status": status, "settlement_id": settlement_id,
            "message": "多人垫付结算计划已保存；各笔转账仍需单独核对和授权。"}


def _public_settlement(conn, row) -> dict:
    payload = json.loads(row["payload_json"])
    legs = conn.execute("SELECT * FROM aa_settlement_legs WHERE settlement_id=? ORDER BY position",
                        (row["id"],)).fetchall()
    transfers = []
    for leg in legs:
        original = payload["preview"]["transfers"][leg["position"]]
        transfers.append({**original, "id": leg["id"], "status": leg["status"],
                          "transaction_id": leg["transaction_id"], "completed_at": leg["completed_at"],
                          "in_account": leg["status"] != "external"})
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"],
            **{key: payload["preview"][key] for key in ("note", "total_yuan", "expense_count", "participants", "expenses")},
            "transfers": transfers,
            "notice": "本人相关路径可在此逐笔授权并记入模拟账本；其他参与人之间的路径只保留建议，不代表已付款，不产生本人流水。"}


def list_settlements(session_id: str) -> dict:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM aa_settlements WHERE session_id=? ORDER BY created_at DESC,id DESC",
                            (session_id,)).fetchall()
        return {"settlements": [_public_settlement(conn, row) for row in rows]}


def get_settlement(settlement_id: str, session_id: str) -> dict:
    with db_session() as conn:
        row = conn.execute("SELECT * FROM aa_settlements WHERE id=? AND session_id=?",
                           (settlement_id, session_id)).fetchone()
        if row is None:
            raise HTTPException(404, "未找到当前会话的结算计划")
        _validate_plan(conn, session_id, row)
        return _public_settlement(conn, row)


def prepare_leg(settlement_id: str, leg_id: str, session_id: str) -> dict:
    with db_session(immediate=True) as conn:
        settlement = conn.execute("SELECT * FROM aa_settlements WHERE id=? AND session_id=?",
                                  (settlement_id, session_id)).fetchone()
        payload = _validate_plan(conn, session_id, settlement)
        leg = conn.execute("SELECT * FROM aa_settlement_legs WHERE id=? AND settlement_id=?",
                           (leg_id, settlement_id)).fetchone()
        if leg is None:
            raise HTTPException(404, "未找到这笔结算路径")
        if leg["status"] == "external":
            raise HTTPException(409, "这笔建议发生在其他参与人之间，不经过本人账户；VeraLane 不会代他们转账")
        if leg["status"] != "pending":
            raise HTTPException(409, "这笔结算路径已完成，不能重复执行")
        payer_is_self = leg["from_id"] == "self"
        counterparty_id = leg["to_id"] if payer_is_self else leg["from_id"]
        contact = conn.execute("SELECT * FROM contacts WHERE id=? AND user_id=? AND verified=1",
                               (counterparty_id, USER_ID)).fetchone()
        if contact is None or payload["contact_fingerprints"].get(counterparty_id) != contact_fingerprint(contact):
            raise HTTPException(409, "联系人资料已变化或不再验证，请重新核对结算计划")
        transfer = payload["preview"]["transfers"][leg["position"]]
        if payer_is_self:
            sent_today = conn.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM transactions
                WHERE account_id=? AND posted_on=? AND category IN ('转账','AA结算') AND direction='out'""",
                (ACCOUNT_ID, business_date(conn))).fetchone()[0]
            if leg["amount_cents"] > available_cents(conn):
                raise HTTPException(409, "可用余额不足，已预留资金不能用于结算转账")
            tier = "red" if requires_red_tier(sent_today + leg["amount_cents"]) else "yellow"
        else:
            tier = "yellow"
        if leg["pending_action_id"]:
            existing = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=?",
                                    (leg["pending_action_id"], session_id)).fetchone()
            if existing is not None and existing["status"] == "pending":
                if existing["tier"] == "red" or tier == "yellow":
                    return reply("请继续核对这笔结算转账。", session_id, "offline",
                                 pending_action={"id": existing["id"], "type": existing["type"],
                                                 "tier": existing["tier"], "status": existing["status"],
                                                 "details": json.loads(existing["payload_json"]),
                                                 "expires_at": existing["expires_at"]})
                conn.execute("UPDATE actions SET status='failed' WHERE id=? AND status='pending'", (existing["id"],))
                audit(conn, session_id, "aa_settlement_action_superseded",
                      {"settlement_id": settlement_id, "leg_id": leg_id,
                       "old_action_id": existing["id"], "reason": "daily_transfer_threshold_increased"})
        details = {"settlement_id": settlement_id, "leg_id": leg_id, "from_id": leg["from_id"],
                   "to_id": leg["to_id"], "from_name": transfer["from_name"], "to_name": transfer["to_name"],
                   "amount_cents": leg["amount_cents"], "amount_yuan": money(leg["amount_cents"]),
                   "counterparty_id": counterparty_id, "counterparty_name": contact["name"],
                   "counterparty_phone_masked": mask_phone(contact["phone"]),
                   "counterparty_fingerprint": contact_fingerprint(contact), "direction": "out" if payer_is_self else "in",
                   "settlement_note": payload["preview"]["note"]}
        action = create_action(conn, session_id, "aa_settlement_leg", tier, details)
        conn.execute("UPDATE aa_settlement_legs SET pending_action_id=? WHERE id=? AND status='pending'",
                     (action["id"], leg_id))
        return reply("请逐笔核对多人垫付结算。本人付款会检查可用余额；本人收款是模拟入账。",
                     session_id, "offline", pending_action=action)


def execute_leg(conn, action_id: str, session_id: str, payload: dict[str, Any]) -> dict:
    settlement = conn.execute("SELECT * FROM aa_settlements WHERE id=? AND session_id=?",
                              (payload["settlement_id"], session_id)).fetchone()
    plan = _validate_plan(conn, session_id, settlement)
    leg = conn.execute("SELECT * FROM aa_settlement_legs WHERE id=? AND settlement_id=?",
                       (payload["leg_id"], payload["settlement_id"])).fetchone()
    if (leg is None or leg["status"] != "pending" or leg["pending_action_id"] != action_id
            or leg["from_id"] != payload["from_id"] or leg["to_id"] != payload["to_id"]
            or leg["amount_cents"] != payload["amount_cents"]):
        raise HTTPException(409, "结算路径、状态或授权编号已变化，未执行")
    payer_is_self = leg["from_id"] == "self"
    counterparty = conn.execute("SELECT * FROM contacts WHERE id=? AND user_id=? AND verified=1",
                                (payload["counterparty_id"], USER_ID)).fetchone()
    if (counterparty is None or contact_fingerprint(counterparty) != payload["counterparty_fingerprint"]
            or plan["contact_fingerprints"].get(counterparty["id"]) != payload["counterparty_fingerprint"]):
        raise HTTPException(409, "联系人资料已变化或不再验证，未执行")
    amount = int(payload["amount_cents"])
    if payer_is_self:
        sent_today = conn.execute("""SELECT COALESCE(SUM(amount_cents),0) FROM transactions
            WHERE account_id=? AND posted_on=? AND category IN ('转账','AA结算') AND direction='out'""",
            (ACCOUNT_ID, business_date(conn))).fetchone()[0]
        if requires_red_tier(sent_today + amount) and action_id:
            action = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=?",
                                  (action_id, session_id)).fetchone()
            if action is None or action["tier"] != "red":
                raise HTTPException(409, "当日转账累计已超过 ¥1,000，请重新生成并完成强验证")
        debit(conn, amount)
    else:
        credit(conn, amount)
    transaction_id = f"aa-settle-{action_id}"
    conn.execute("""INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note,action_id)
        VALUES(?,?,?,?,?,?,'AA结算',?,?)""",
        (transaction_id, ACCOUNT_ID, business_date(conn), "out" if payer_is_self else "in", amount,
         counterparty["name"], f"多人垫付结算：{payload['settlement_note']}", action_id))
    completed_at = business_now(conn).isoformat(timespec="seconds")
    conn.execute("UPDATE aa_settlement_legs SET status='completed',transaction_id=?,completed_at=? WHERE id=? AND status='pending'",
                 (transaction_id, completed_at, payload["leg_id"]))
    remaining = conn.execute("SELECT COUNT(*) FROM aa_settlement_legs WHERE settlement_id=? AND status='pending'",
                             (payload["settlement_id"],)).fetchone()[0]
    if remaining == 0:
        conn.execute("UPDATE aa_settlements SET status='owner_actions_complete' WHERE id=?",
                     (payload["settlement_id"],))
    audit(conn, session_id, "aa_settlement_leg_completed", {"settlement_id": payload["settlement_id"],
          "leg_id": payload["leg_id"], "transaction_id": transaction_id,
          "direction": "out" if payer_is_self else "in", "amount_yuan": payload["amount_yuan"], "simulated": True})
    return {"status": "completed", "settlement_id": payload["settlement_id"], "leg_id": payload["leg_id"],
            "transaction_id": transaction_id, "amount_yuan": payload["amount_yuan"],
            "message": (f"已模拟向 {counterparty['name']} 转出" if payer_is_self else f"已模拟 {counterparty['name']} 向本人结算入账")
                       + f" ¥{payload['amount_yuan']}；这是模拟结算，不代表真实银行转账。"}
