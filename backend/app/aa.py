"""AA collection plans and atomic, explicitly simulated incoming payments."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from .clock import business_date, business_now
from .db import ACCOUNT_ID, USER_ID, audit, connect, db_session, utc_now
from .service import (cents_from_yuan, contact_fingerprint, create_action, get_context,
                      mask_phone, money, reply, set_context)
from .agent import Intent, parse_intent
from .aa_intent import parse_aa_message
from .schedule_time import instruction_text


def interpret_message(conn: sqlite3.Connection, session_id: str, message: str, mode: str,
                      intent: Intent, source_id: str | None = None, reset: bool = False) -> dict:
    contacts = conn.execute("SELECT * FROM contacts WHERE user_id = ? AND verified = 1 ORDER BY name, id", (USER_ID,)).fetchall()
    names = list(dict.fromkeys(row["name"] for row in contacts))
    state = {} if reset else get_context(conn, session_id).get("pending_aa", {})
    if source_id and source_id != state.get("source_transaction_id"):
        state = {"source_transaction_id": source_id}
    slots = parse_aa_message(message, names)
    if intent.action == "aa_split":
        # Keep deterministic constraints; model notes may provide a useful label.
        if slots["note"] is None and intent.note:
            slots["note"] = intent.note
        if intent.aa_non_equal:
            slots["aa_non_equal"] = True
        if intent.payer_is_self is False:
            slots["payer_is_self"] = False
    if slots["participants"]:
        tokens = slots["participants"]
        # A full phone number can disambiguate one member without losing the others.
        if len(tokens) == 1 and re.fullmatch(r"1[3-9]\d{9}", tokens[0]) and state.get("participants"):
            contact = next((row for row in contacts if row["phone"] == tokens[0]), None)
            if contact and contact["name"] in state["participants"]:
                tokens = [tokens[0] if token == contact["name"] else token for token in state["participants"]]
        state["participants"] = tokens
    text = instruction_text(message)
    if slots["amount_yuan"] is not None or re.search(r"元|块", text):
        state["amount_yuan"] = slots["amount_yuan"]
    for field in ("include_self", "participant_count", "note", "payer_is_self"):
        if slots[field] is not None:
            state[field] = slots[field]
    if slots["aa_non_equal"]:
        state["aa_non_equal"] = True
    elif re.search(r"均分|平摊|平均分", text):
        state["aa_non_equal"] = False
    source = source_transaction(conn, state.get("source_transaction_id"))
    total = source["amount_yuan"] if source else state.get("amount_yuan")
    amount = cents_from_yuan(total)
    issues = []
    if amount is None:
        issues.append("请补充有效的垫付总额，精确到分。")
    if source and state.get("amount_yuan") and cents_from_yuan(state["amount_yuan"]) != cents_from_yuan(source["amount_yuan"]):
        issues.append("已按原始账单锁定总额，请核对，原支出不会重复扣款。")
    if state.get("include_self") is not True:
        issues.append("请明确人数是否包含本人；首版需把本人列入分摊表，免付时可手动改为 0 元。")
    if state.get("payer_is_self") is False:
        issues.append("首版用于本人垫付后的收款；原句提到其他人垫付，请核对或改为本人垫付的支出。")
    if state.get("aa_non_equal"):
        issues.append("检测到非均分或多个金额，请先核对总额，再在分摊表手动调整每人份额；不会自动分配差额。")
    members, seen = [], set()
    for token in state.get("participants") or []:
        candidates = [row for row in contacts if token in (row["name"], row["phone"])]
        key = candidates[0]["id"] if len(candidates) == 1 else token
        if key in seen:
            issues.append(f"已合并重复的参与人“{token}”，请核对人数。")
            continue
        seen.add(key)
        if not candidates:
            issues.append(f"未找到“{token}”对应的已验证联系人，请在名单中重新选择。")
        elif len(candidates) > 1:
            issues.append(f"“{token}”存在同名联系人，请按手机号选择。")
        members.append({"name": candidates[0]["name"] if candidates else token,
                        "contact_id": candidates[0]["id"] if len(candidates) == 1 else None,
                        "phone_masked": mask_phone(candidates[0]["phone"]) if len(candidates) == 1 else None,
                        "choices": [{"id": row["id"], "name": row["name"], "phone_masked": mask_phone(row["phone"])} for row in candidates]})
    if not 1 <= len(members) <= 7:
        issues.append("请提供 1–7 位其他参与人，连同本人共 2–8 人。")
    if state.get("participant_count") is not None and state["participant_count"] != len(members) + 1:
        issues.append(f"原句说共 {state['participant_count']} 人，目前名单连同本人共 {len(members) + 1} 人，请核对。")
    draft = {"total_yuan": money(amount) if amount is not None else None, "note": state.get("note") or (source["counterparty"] if source else "AA 分摊"),
             "include_self": state.get("include_self"), "payer_is_self": state.get("payer_is_self"),
             "participant_count": state.get("participant_count"), "source_transaction": source,
             "participants": members, "needs_review": issues, "requires_custom_shares": bool(state.get("aa_non_equal"))}
    set_context(conn, session_id, {"pending_aa": state})
    return reply(" ".join(issues) if issues else "已提取分摊信息。请核对名单并计算预览，确认后才建立收款单。",
                 session_id, mode, aa_draft=draft)


async def interpret(session_id: str, message: str, source_id: str | None = None, reset: bool = False) -> dict:
    with db_session() as conn:
        names = [row[0] for row in conn.execute("SELECT DISTINCT name FROM contacts WHERE user_id = ?", (USER_ID,))]
    parsed = await parse_intent(message, names, [])
    with db_session() as conn:
        audit(conn, session_id, "intent_parsed", {"action": "aa_split", "mode": parsed.mode})
        if parsed.usage:
            conn.execute("INSERT INTO model_usage (at, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?)",
                         (utc_now(), "deepseek-flash", parsed.usage["prompt_tokens"], parsed.usage["completion_tokens"]))
        return interpret_message(conn, session_id, message, parsed.mode, parsed.intent, source_id, reset)


def source_transaction(conn: sqlite3.Connection, transaction_id: str | None) -> dict | None:
    if not transaction_id:
        return None
    row = conn.execute("SELECT id, posted_on, counterparty, amount_cents FROM transactions "
                       "WHERE id = ? AND account_id = ? AND direction = 'out'",
                       (transaction_id, ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到可分摊的本账户支出")
    return {"id": row["id"], "posted_on": row["posted_on"], "counterparty": row["counterparty"],
            "amount_yuan": money(row["amount_cents"])}


def _check_source_available(conn: sqlite3.Connection, source: dict | None) -> None:
    if source and conn.execute("SELECT 1 FROM aa_collections WHERE account_id = ? "
                               "AND source_transaction_id = ? AND source_locked = 1",
                               (ACCOUNT_ID, source["id"])).fetchone():
        raise HTTPException(409, "这笔支出已有分摊单，请查看原单；仅完全未回款且已关闭的单据可以重新发起")


def share_cents(value: str) -> int:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2 or amount > 100_000:
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, "每人份额须为非负、精确到分的金额") from None


def build_preview(conn: sqlite3.Connection, data: dict) -> dict:
    total = cents_from_yuan(data.get("total_yuan"))
    if total is None:
        raise HTTPException(422, "请输入大于零、精确到分且不超过 100000 元的总额")
    if data.get("include_self") is not True:
        raise HTTPException(422, "请明确本次分摊包含本人；如本人免付，可将本人份额调整为 0 元")
    ids = data.get("contact_ids", [])
    if not 1 <= len(ids) <= 7 or len(set(ids)) != len(ids) or "self" in ids:
        raise HTTPException(422, "请选择 1–7 位不重复的联系人，连同本人共 2–8 人")
    source = source_transaction(conn, data.get("source_transaction_id"))
    if source and source["amount_yuan"] != money(total):
        raise HTTPException(409, "分摊总额必须与原始支出一致，请重新读取账单")
    _check_source_available(conn, source)
    participants = [{"id": "self", "name": "我", "phone_masked": "本人"}]
    for contact_id in ids:
        row = conn.execute("SELECT * FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
                           (contact_id, USER_ID)).fetchone()
        if row is None:
            raise HTTPException(422, "参与人必须来自已验证联系人")
        participants.append({"id": row["id"], "name": row["name"], "phone_masked": mask_phone(row["phone"]),
                             "contact_fingerprint": contact_fingerprint(row)})
    shares = data.get("shares_yuan")
    if shares is not None:
        if set(shares) != {row["id"] for row in participants}:
            raise HTTPException(422, "调整份额时必须提交本人及全部参与人的金额")
        amounts = [share_cents(shares[row["id"]]) for row in participants]
        if sum(amounts) != total:
            raise HTTPException(422, f"份额合计与总额相差 ¥{money(total - sum(amounts))}，请调整后再生成计划")
        extras = [False] * len(participants)
    else:
        base, remainder = divmod(total, len(participants))
        amounts = [base + (index < remainder) for index in range(len(participants))]
        extras = [index < remainder for index in range(len(participants))]
    for index, participant in enumerate(participants):
        participant.update(amount_yuan=money(amounts[index]), amount_cents=amounts[index], rounding_extra=extras[index])
    return {"total_yuan": money(total), "total_cents": total,
            "self_yuan": money(amounts[0]), "receivable_yuan": money(total - amounts[0]),
            "note": (data.get("note") or "AA 分摊")[:100], "source_transaction": source,
            "source_type": "ledger" if source else "user", "participants": participants}


def preview(data: dict) -> dict:
    with db_session() as conn:
        return build_preview(conn, data)


def prepare(data: dict) -> dict:
    with db_session() as conn:
        payload = build_preview(conn, data)
        action = create_action(conn, data["session_id"], "aa_collection", "yellow", payload)
        set_context(conn, data["session_id"], {})
        return reply("请核对人数、每人份额和应收金额。确认只建立本地收款单，尚未到账，也未向联系人发送消息。",
                     data["session_id"], "offline", pending_action=action)


def create_collection(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    # Rebuild from the authorized amounts to recheck source, membership and arithmetic.
    people = payload["participants"]
    data = {"total_yuan": payload["total_yuan"], "include_self": True, "note": payload["note"],
            "contact_ids": [person["id"] for person in people if person["id"] != "self"],
            "source_transaction_id": (payload["source_transaction"] or {}).get("id"),
            "shares_yuan": {person["id"]: person["amount_yuan"] for person in people}}
    fresh = build_preview(conn, data)
    if any(a.get("contact_fingerprint") != b.get("contact_fingerprint") for a, b in zip(people, fresh["participants"])):
        raise HTTPException(409, "参与人信息已变化，请重新核对分摊计划")
    if payload["source_transaction"] != fresh["source_transaction"]:
        raise HTTPException(409, "原始支出信息已变化，请重新生成分摊计划")
    if (payload["total_cents"] != fresh["total_cents"] or payload["self_yuan"] != fresh["self_yuan"]
            or payload["receivable_yuan"] != fresh["receivable_yuan"]
            or any(a["amount_cents"] != b["amount_cents"] for a, b in zip(people, fresh["participants"]))):
        raise HTTPException(409, "分摊金额与授权不一致")
    timestamp = utc_now()
    state = "completed" if share_cents(payload["receivable_yuan"]) == 0 else "pending"
    conn.execute("INSERT INTO aa_collections (id, session_id, account_id, source_transaction_id, payload_json, status, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (action_id, session_id, ACCOUNT_ID, data["source_transaction_id"],
                  json.dumps(payload, ensure_ascii=False), state, timestamp))
    for person in people:
        if person["id"] != "self" and person["amount_cents"] > 0:
            conn.execute("INSERT INTO aa_requests (id, collection_id, contact_id, amount_cents, status) VALUES (?, ?, ?, ?, 'pending')",
                         (secrets.token_urlsafe(18), action_id, person["id"], person["amount_cents"]))
    audit(conn, session_id, "aa_collection_created", {"collection_id": action_id, "receivable_yuan": payload["receivable_yuan"]})
    return {"status": "completed", "collection_id": action_id, "action_id": action_id,
            "message": f"AA 收款单已建立，应收 ¥{payload['receivable_yuan']}。建单未改变账户余额；可在 AA 收款中查看进度。"}


def _owned(conn: sqlite3.Connection, collection_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM aa_collections WHERE id = ? AND session_id = ? AND account_id = ?",
                       (collection_id, session_id, ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该收款单")
    return row


def _received(conn: sqlite3.Connection, collection_id: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(e.amount_cents), 0) FROM aa_payment_events e "
                        "JOIN aa_requests r ON r.id = e.request_id WHERE r.collection_id = ?", (collection_id,)).fetchone()[0]


def public_collection(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"])
    requests = {item["contact_id"]: item for item in conn.execute("SELECT * FROM aa_requests WHERE collection_id = ?", (row["id"],))}
    received = _received(conn, row["id"])
    target = share_cents(payload["receivable_yuan"])
    participants = []
    for person in payload["participants"]:
        request = requests.get(person["id"])
        participants.append({"id": person["id"], "name": person["name"], "phone_masked": person["phone_masked"],
                             "amount_yuan": person["amount_yuan"], "request_id": request["id"] if request else None,
                             "status": request["status"] if request else "self" if person["id"] == "self" else "not_required",
                             "paid_at": request["paid_at"] if request else None,
                             "transaction_id": request["transaction_id"] if request else None})
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"], "closed_at": row["closed_at"],
            **{key: payload[key] for key in ("note", "total_yuan", "self_yuan", "receivable_yuan", "source_transaction", "source_type")},
            "received_yuan": money(received), "outstanding_yuan": money(target - received),
            "net_advance_yuan": money(payload["total_cents"] - received), "participants": participants}


def list_collections(session_id: str) -> dict:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM aa_collections WHERE session_id = ? AND account_id = ? ORDER BY created_at DESC, rowid DESC",
                            (session_id, ACCOUNT_ID)).fetchall()
        return {"collections": [public_collection(conn, row) for row in rows],
                "demo_controls_enabled": os.environ.get("VERALANE_DEMO_CONTROLS", "1") == "1"}


def get_collection(collection_id: str, session_id: str) -> dict:
    with db_session() as conn:
        return public_collection(conn, _owned(conn, collection_id, session_id))


def _validate_authorization(conn: sqlite3.Connection, collection: sqlite3.Row) -> dict:
    payload = json.loads(collection["payload_json"])
    action = conn.execute("SELECT * FROM actions WHERE id = ?", (collection["id"],)).fetchone()
    if (not action or action["type"] != "aa_collection" or action["status"] != "completed"
            or action["session_id"] != collection["session_id"] or action["tier"] != "yellow"
            or json.loads(action["payload_json"]) != payload
            or collection["source_transaction_id"] != (payload["source_transaction"] or {}).get("id")):
        raise HTTPException(409, "收款单与原授权不一致，未入账")
    return payload


def simulate_payment(request_id: str, session_id: str) -> dict:
    if os.environ.get("VERALANE_DEMO_CONTROLS", "1") != "1":
        raise HTTPException(403, "模拟付款控制已关闭")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        request = conn.execute("SELECT * FROM aa_requests WHERE id = ?", (request_id,)).fetchone()
        if request is None:
            raise HTTPException(404, "未找到收款请求")
        group = _owned(conn, request["collection_id"], session_id)
        payload = _validate_authorization(conn, group)
        person = next((p for p in payload["participants"] if p["id"] == request["contact_id"] and p["id"] != "self"), None)
        if person is None or person["amount_cents"] != request["amount_cents"]:
            raise HTTPException(409, "收款请求与已确认份额不一致，未入账")
        if request["status"] == "paid":
            result = public_collection(conn, group)
            conn.commit()
            return result
        if request["status"] != "pending" or group["status"] not in ("pending", "partial"):
            raise HTTPException(409, "收款请求已关闭，未入账")
        contact = conn.execute("SELECT * FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
                               (request["contact_id"], USER_ID)).fetchone()
        if contact is None or contact_fingerprint(contact) != person["contact_fingerprint"]:
            raise HTTPException(409, "付款人信息已变化，未入账")
        amount = request["amount_cents"]
        received = _received(conn, group["id"])
        target = share_cents(payload["receivable_yuan"])
        if received + amount > target:
            raise HTTPException(409, "到账将超过应收金额，未入账")
        timestamp = business_now(conn).isoformat(timespec="seconds")
        tx_id = f"aa-{request_id}"
        conn.execute("UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?", (amount, ACCOUNT_ID))
        conn.execute("INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note, action_id) "
                     "VALUES (?, ?, ?, 'in', ?, ?, 'AA回款', ?, ?)",
                     (tx_id, ACCOUNT_ID, business_date(conn), amount, person["name"], payload["note"], f"aa-payment-{request_id}"))
        conn.execute("INSERT INTO aa_payment_events (id, request_id, transaction_id, amount_cents, created_at) VALUES (?, ?, ?, ?, ?)",
                     (f"payment-{request_id}", request_id, tx_id, amount, timestamp))
        conn.execute("UPDATE aa_requests SET status = 'paid', paid_at = ?, transaction_id = ? WHERE id = ?",
                     (timestamp, tx_id, request_id))
        conn.execute("UPDATE aa_collections SET status = ? WHERE id = ?",
                     ("completed" if received + amount == target else "partial", group["id"]))
        audit(conn, session_id, "aa_payment_received", {"collection_id": group["id"], "request_id": request_id,
                                                       "transaction_id": tx_id, "amount_yuan": money(amount), "simulated": True})
        result = public_collection(conn, _owned(conn, group["id"], session_id))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_collection(collection_id: str, session_id: str) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _owned(conn, collection_id, session_id)
        _validate_authorization(conn, row)
        if row["status"] in ("pending", "partial"):
            received = _received(conn, collection_id)
            conn.execute("UPDATE aa_requests SET status = 'closed' WHERE collection_id = ? AND status = 'pending'", (collection_id,))
            conn.execute("UPDATE aa_collections SET status = 'closed', closed_at = ?, source_locked = ? WHERE id = ?",
                         (business_now(conn).isoformat(timespec="seconds"), int(received > 0), collection_id))
            audit(conn, session_id, "aa_collection_closed", {"collection_id": collection_id, "received_yuan": money(received)})
        result = public_collection(conn, _owned(conn, collection_id, session_id))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
