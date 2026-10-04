"""AA collection plans and atomic, explicitly simulated incoming payments."""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import sqlite3
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from .clock import business_date, business_now
from .db import ACCOUNT_ID, USER_ID, audit, connect, db_session, utc_now
from .execution_controls import available_cents, debit
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
    if slots.get("share_weights") is not None:
        state["share_weights"] = slots["share_weights"]
    for field in ("include_self", "participant_count", "note", "payer_is_self"):
        if slots[field] is not None:
            state[field] = slots[field]
    if slots["aa_non_equal"]:
        state["aa_non_equal"] = True
    elif re.search(r"均分|平摊|平均分", text):
        state["aa_non_equal"] = False
        state.pop("share_weights", None)
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
        if state.get("share_weights"):
            issues.append("检测到明确份数或非均分信息；请先核对总额和预填权重，再检查服务端计算的每人金额。")
        else:
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
    suggested_share_ratios = None
    if state.get("share_weights"):
        ratios = {}
        ambiguous = False
        for entry in state["share_weights"]:
            token = entry["name"]
            if token in {"我", "本人", "自己"}:
                person_id = "self"
            else:
                candidates = [row for row in contacts if token in (row["name"], row["phone"])]
                person_id = candidates[0]["id"] if len(candidates) == 1 else None
            if person_id is None or person_id in ratios:
                ambiguous = True
                break
            ratios[person_id] = entry["weight"]
        expected_ids = {"self", *(row["contact_id"] for row in members if row["contact_id"])}
        if ambiguous or set(ratios) != expected_ids or not any(ratios.values()):
            issues.append("已识别到按人分配的份数，但名单存在歧义或份数没有覆盖本人及全部参与人；请核对联系人，并为每人明确填写份数。")
        else:
            suggested_share_ratios = ratios
    draft = {"total_yuan": money(amount) if amount is not None else None, "note": state.get("note") or (source["counterparty"] if source else "AA 分摊"),
             "include_self": state.get("include_self"), "payer_is_self": state.get("payer_is_self"),
             "participant_count": state.get("participant_count"), "source_transaction": source,
             "participants": members, "needs_review": issues, "requires_custom_shares": bool(state.get("aa_non_equal")),
             "suggested_share_ratios": suggested_share_ratios}
    set_context(conn, session_id, {"pending_aa": state})
    ratio_message = "已识别明确的份数并预填比例权重；服务端会重新计算金额，请核对每个人的份数和分摊结果。" if suggested_share_ratios else None
    return reply(" ".join(issues) if issues else ratio_message or "已提取分摊信息。请核对名单并计算预览，确认后才建立收款单。",
                 session_id, mode, aa_draft=draft)


async def interpret(session_id: str, message: str, source_id: str | None = None, reset: bool = False) -> dict:
    with db_session() as conn:
        names = [row[0] for row in conn.execute("SELECT DISTINCT name FROM contacts WHERE user_id = ?", (USER_ID,))]
    parsed = await parse_intent(message, names, [])
    with db_session() as conn:
        audit(conn, session_id, "intent_parsed", {"action": "aa_split", "mode": parsed.mode})
        if parsed.usage:
            conn.execute("INSERT INTO model_usage (at, model, prompt_tokens, completion_tokens) VALUES (?, ?, ?, ?)",
                         (utc_now(), os.getenv('DEEPSEEK_MODEL', 'deepseek-flash'), parsed.usage["prompt_tokens"], parsed.usage["completion_tokens"]))
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
    ratios = data.get("shares_ratio")
    itemized = data.get("itemized_items")
    if sum(value is not None for value in (shares, ratios, itemized)) > 1:
        raise HTTPException(422, "均分、比例、手动份额与按菜品分摊不能混合提交")
    share_ratios = None
    normalized_items = None
    if itemized is not None:
        amounts, normalized_items = _allocate_itemized(itemized, participants, total)
        extras = [False] * len(participants)
        allocation_method = "itemized"
    elif ratios is not None:
        if set(ratios) != {row["id"] for row in participants}:
            raise HTTPException(422, "按比例分摊时必须为本人及全部参与人提供比例值")
        weights = [ratios[row["id"]] for row in participants]
        if any(type(weight) is not int or weight < 0 or weight > 1_000_000 for weight in weights):
            raise HTTPException(422, "比例值须为 0 到 1000000 的整数")
        weight_total = sum(weights)
        if weight_total == 0:
            raise HTTPException(422, "至少一位参与人的比例值须大于 0")
        divided = [divmod(total * weight, weight_total) for weight in weights]
        amounts = [whole for whole, _ in divided]
        remainder_order = sorted(range(len(participants)), key=lambda index: (-divided[index][1], index))
        leftover = total - sum(amounts)
        extras = [False] * len(participants)
        for index in remainder_order[:leftover]:
            amounts[index] += 1
            extras[index] = True
        allocation_method = "proportional"
        share_ratios = {row["id"]: ratios[row["id"]] for row in participants}
    elif shares is not None:
        if set(shares) != {row["id"] for row in participants}:
            raise HTTPException(422, "调整份额时必须提交本人及全部参与人的金额")
        amounts = [share_cents(shares[row["id"]]) for row in participants]
        if sum(amounts) != total:
            raise HTTPException(422, f"份额合计与总额相差 ¥{money(total - sum(amounts))}，请调整后再生成计划")
        extras = [False] * len(participants)
        allocation_method = "manual"
    else:
        base, remainder = divmod(total, len(participants))
        amounts = [base + (index < remainder) for index in range(len(participants))]
        extras = [index < remainder for index in range(len(participants))]
        allocation_method = "equal"
    for index, participant in enumerate(participants):
        participant.update(amount_yuan=money(amounts[index]), amount_cents=amounts[index], rounding_extra=extras[index],
                            share_ratio=share_ratios[participant["id"]] if share_ratios is not None else None)
    return {"total_yuan": money(total), "total_cents": total,
            "self_yuan": money(amounts[0]), "receivable_yuan": money(total - amounts[0]),
            "note": (data.get("note") or "AA 分摊")[:100], "source_transaction": source,
            "source_type": "ledger" if source else "user", "participants": participants,
            "allocation_method": allocation_method, "share_ratios": share_ratios,
            "itemized_items": normalized_items,
            "reminder_on": (date.fromisoformat(business_date(conn)) + timedelta(days=3)).isoformat()}


def _allocate_itemized(items: list[dict], participants: list[dict], total_cents: int) -> tuple[list[int], list[dict]]:
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise HTTPException(422, "按菜品分摊须提供 1–100 条明细")
    participant_ids = [person["id"] for person in participants]
    participant_index = {participant_id: index for index, participant_id in enumerate(participant_ids)}
    amounts = [0] * len(participants)
    normalized = []
    item_total = 0
    for item in items:
        description = (item.get("description") or "").strip()
        if not description or len(description) > 80:
            raise HTTPException(422, "每道菜名须为 1–80 个字符")
        try:
            cents = share_cents(item.get("amount_yuan"))
        except HTTPException:
            raise HTTPException(422, "每道菜金额须为正数且精确到分") from None
        if cents <= 0:
            raise HTTPException(422, "每道菜金额须为正数且精确到分")
        selected = item.get("participant_ids")
        if (not isinstance(selected, list) or not 1 <= len(selected) <= len(participants)
                or len(set(selected)) != len(selected)
                or any(participant_id not in participant_index for participant_id in selected)):
            raise HTTPException(422, "每道菜须选择至少一位、不重复的本次参与人")
        selected = sorted(selected, key=participant_index.__getitem__)
        base, remainder = divmod(cents, len(selected))
        allocations = []
        for position, participant_id in enumerate(selected):
            share = base + (position < remainder)
            amounts[participant_index[participant_id]] += share
            allocations.append({"participant_id": participant_id,
                                "name": participants[participant_index[participant_id]]["name"],
                                "amount_cents": share, "amount_yuan": money(share)})
        item_total += cents
        normalized.append({"description": description, "amount_cents": cents, "amount_yuan": money(cents),
                           "participant_ids": selected, "allocations": allocations})
    if item_total != total_cents:
        raise HTTPException(422, f"菜品明细合计须等于垫付总额，当前相差 ¥{money(total_cents - item_total)}")
    return amounts, normalized


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
            "source_transaction_id": (payload["source_transaction"] or {}).get("id")}
    if payload["allocation_method"] == "itemized":
        data["itemized_items"] = payload.get("itemized_items")
    elif payload["allocation_method"] == "proportional":
        data["shares_ratio"] = payload["share_ratios"]
    elif payload["allocation_method"] == "manual":
        data["shares_yuan"] = {person["id"]: person["amount_yuan"] for person in people}
    elif payload["allocation_method"] != "equal":
        raise HTTPException(409, "分摊方式已变化，请重新生成计划")
    fresh = build_preview(conn, data)
    if any(a.get("contact_fingerprint") != b.get("contact_fingerprint") for a, b in zip(people, fresh["participants"])):
        raise HTTPException(409, "参与人信息已变化，请重新核对分摊计划")
    if payload["source_transaction"] != fresh["source_transaction"]:
        raise HTTPException(409, "原始支出信息已变化，请重新生成分摊计划")
    if payload["reminder_on"] != fresh["reminder_on"]:
        raise HTTPException(409, "演示日期已变化，请重新核对站内提醒日期")
    if payload["allocation_method"] != fresh["allocation_method"] or payload["share_ratios"] != fresh["share_ratios"]:
        raise HTTPException(409, "分摊方式或比例已变化，请重新核对计划")
    if payload.get("itemized_items") != fresh.get("itemized_items"):
        raise HTTPException(409, "菜品明细或承担人已变化，请重新核对计划")
    if (payload["total_cents"] != fresh["total_cents"] or payload["self_yuan"] != fresh["self_yuan"]
            or payload["receivable_yuan"] != fresh["receivable_yuan"]
            or any(a["amount_cents"] != b["amount_cents"] for a, b in zip(people, fresh["participants"]))):
        raise HTTPException(409, "分摊金额与授权不一致")
    timestamp = utc_now()
    state = "completed" if share_cents(payload["receivable_yuan"]) == 0 else "pending"
    conn.execute("INSERT INTO aa_collections (id, session_id, account_id, source_transaction_id, payload_json, status, created_at, reminder_on) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (action_id, session_id, ACCOUNT_ID, data["source_transaction_id"],
                  json.dumps(payload, ensure_ascii=False), state, timestamp, payload["reminder_on"]))
    for person in people:
        if person["id"] != "self" and person["amount_cents"] > 0:
            conn.execute("INSERT INTO aa_requests (id, collection_id, contact_id, amount_cents, status) VALUES (?, ?, ?, ?, 'pending')",
                         (secrets.token_urlsafe(18), action_id, person["id"], person["amount_cents"]))
    audit(conn, session_id, "aa_collection_created", {"collection_id": action_id, "receivable_yuan": payload["receivable_yuan"]})
    return {"status": "completed", "collection_id": action_id, "action_id": action_id,
            "message": f"AA 收款单已建立，应收 ¥{payload['receivable_yuan']}。建单未改变账户余额；可在 AA 收款中查看进度。"}


def refresh_aa_reminders(conn: sqlite3.Connection) -> int:
    today = business_date(conn)
    due = conn.execute("SELECT * FROM aa_collections WHERE status IN ('pending', 'partial') "
                       "AND reminder_on IS NOT NULL AND reminder_on <= ? ORDER BY reminder_on, id", (today,)).fetchall()
    created = 0
    for row in due:
        reminder_id = f"aa:{row['id']}:{row['reminder_on']}"
        payload = json.loads(row["payload_json"])
        note = payload.get("note") or "AA 收款"
        cursor = conn.execute("INSERT OR IGNORE INTO reminders(id,account_id,kind,title,body,source_id,due_on,created_at) "
                              "VALUES(?,?,'aa_collection',?,?,?, ?,?)",
                              (reminder_id, row["account_id"], f"{note}仍有未收款项",
                               "收款单仍有未收金额。请打开 AA 查看实时余额；这是站内提醒，不发送消息。",
                               row["id"], row["reminder_on"], business_now(conn).isoformat(timespec="seconds")))
        created += cursor.rowcount
    return created


def _owned(conn: sqlite3.Connection, collection_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM aa_collections WHERE id = ? AND session_id = ? AND account_id = ?",
                       (collection_id, session_id, ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该收款单")
    return row


def _received(conn: sqlite3.Connection, collection_id: str) -> int:
    legacy = conn.execute("SELECT COALESCE(SUM(e.amount_cents), 0) FROM aa_payment_events e "
                          "JOIN aa_requests r ON r.id = e.request_id WHERE r.collection_id = ?", (collection_id,)).fetchone()[0]
    installments = conn.execute("SELECT COALESCE(SUM(p.amount_cents), 0) FROM aa_partial_payments p "
                                "JOIN aa_requests r ON r.id = p.request_id WHERE r.collection_id = ?", (collection_id,)).fetchone()[0]
    return legacy + installments


def _request_received(conn: sqlite3.Connection, request_id: str) -> int:
    legacy = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM aa_payment_events WHERE request_id = ?", (request_id,)).fetchone()[0]
    installments = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM aa_partial_payments WHERE request_id = ?", (request_id,)).fetchone()[0]
    return legacy + installments


def _refunded(conn: sqlite3.Connection, collection_id: str) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM aa_refunds WHERE collection_id=?", (collection_id,)).fetchone()[0]


def public_collection(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"])
    requests = {item["contact_id"]: item for item in conn.execute("SELECT * FROM aa_requests WHERE collection_id = ?", (row["id"],))}
    received = _received(conn, row["id"])
    target = share_cents(payload["receivable_yuan"])
    participants = []
    for person in payload["participants"]:
        request = requests.get(person["id"])
        request_received = _request_received(conn, request["id"]) if request else 0
        request_payments = []
        request_refunded = 0
        if request:
            payment_rows = conn.execute(
                "SELECT amount_cents, transaction_id, created_at FROM aa_payment_events WHERE request_id = ? "
                "UNION ALL SELECT amount_cents, transaction_id, created_at FROM aa_partial_payments WHERE request_id = ? "
                "ORDER BY created_at, transaction_id", (request["id"], request["id"]),
            ).fetchall()
            for item in payment_rows:
                refund_rows = conn.execute("SELECT amount_cents,transaction_id,created_at FROM aa_refunds "
                                           "WHERE source_transaction_id=? ORDER BY created_at,transaction_id",
                                           (item["transaction_id"],)).fetchall()
                refunded = sum(refund["amount_cents"] for refund in refund_rows)
                request_refunded += refunded
                request_payments.append({"amount_yuan": money(item["amount_cents"]), "transaction_id": item["transaction_id"],
                                         "paid_at": item["created_at"], "refunded_yuan": money(refunded),
                                         "net_received_yuan": money(item["amount_cents"] - refunded),
                                         "refundable_yuan": money(item["amount_cents"] - refunded),
                                         "refunds": [{"amount_yuan": money(refund["amount_cents"]),
                                                      "transaction_id": refund["transaction_id"], "refunded_at": refund["created_at"]}
                                                     for refund in refund_rows]})
        status = "self" if person["id"] == "self" else "not_required"
        if request:
            status = request["status"] if request["status"] in ("paid", "closed") else "partial" if request_received else "pending"
        participants.append({"id": person["id"], "name": person["name"], "phone_masked": person["phone_masked"],
                             "amount_yuan": person["amount_yuan"], "request_id": request["id"] if request else None,
                             "share_ratio": person.get("share_ratio"),
                             "status": status, "received_yuan": money(request_received),
                             "refunded_yuan": money(request_refunded),
                             "net_received_yuan": money(request_received - request_refunded),
                             "outstanding_yuan": money((request["amount_cents"] - request_received) if request else 0),
                             "payments": request_payments,
                             "paid_at": request["paid_at"] if request else None,
                             "transaction_id": request["transaction_id"] if request else None})
    refunded = _refunded(conn, row["id"])
    return {"id": row["id"], "status": row["status"], "created_at": row["created_at"], "closed_at": row["closed_at"],
            **{key: payload[key] for key in ("note", "total_yuan", "self_yuan", "receivable_yuan", "source_transaction", "source_type")},
            "allocation_method": payload.get("allocation_method"), "share_ratios": payload.get("share_ratios"),
            "itemized_items": payload.get("itemized_items"),
            "received_yuan": money(received), "refunded_yuan": money(refunded),
            "net_received_yuan": money(received - refunded), "outstanding_yuan": money(target - received),
            "net_advance_yuan": money(payload["total_cents"] - received + refunded), "participants": participants}


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


def _refund_snapshot(conn: sqlite3.Connection, request_id: str, source_transaction_id: str,
                     amount_cents: int, session_id: str) -> dict:
    request = conn.execute("SELECT * FROM aa_requests WHERE id=?", (request_id,)).fetchone()
    if request is None:
        raise HTTPException(404, "未找到 AA 收款请求")
    group = _owned(conn, request["collection_id"], session_id)
    collection_payload = _validate_authorization(conn, group)
    if request["status"] != "paid":
        raise HTTPException(409, "仅已付清份额的回款可以退款；未付清的回款不能用退款代替关闭请求")
    person = next((row for row in collection_payload["participants"]
                   if row["id"] == request["contact_id"] and row["id"] != "self"), None)
    contact = conn.execute("SELECT * FROM contacts WHERE id=? AND user_id=? AND verified=1",
                           (request["contact_id"], USER_ID)).fetchone()
    if person is None or person["amount_cents"] != request["amount_cents"] or contact is None:
        raise HTTPException(409, "收款对象与已确认记录不一致，未退款")
    if contact_fingerprint(contact) != person["contact_fingerprint"]:
        raise HTTPException(409, "付款人信息已变化，请重新核对收款单")
    source = conn.execute("""SELECT amount_cents,created_at FROM aa_payment_events
        WHERE request_id=? AND transaction_id=?
        UNION ALL
        SELECT amount_cents,created_at FROM aa_partial_payments
        WHERE request_id=? AND transaction_id=?""",
        (request_id, source_transaction_id, request_id, source_transaction_id)).fetchone()
    if source is None:
        raise HTTPException(409, "原回款流水与该收款请求不匹配，未退款")
    refunded = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM aa_refunds WHERE source_transaction_id=?",
                            (source_transaction_id,)).fetchone()[0]
    remaining = source["amount_cents"] - refunded
    if remaining <= 0:
        raise HTTPException(409, "该笔回款已全部退回")
    if amount_cents <= 0 or amount_cents > remaining:
        raise HTTPException(409, f"退款不能超过该笔回款的剩余可退金额 ¥{money(remaining)}")
    return {"collection_id": group["id"], "request_id": request_id,
            "source_transaction_id": source_transaction_id, "source_amount_cents": source["amount_cents"],
            "source_amount_yuan": money(source["amount_cents"]), "source_paid_at": source["created_at"],
            "remaining_refundable_cents": remaining, "remaining_refundable_yuan": money(remaining),
            "amount_cents": amount_cents, "amount_yuan": money(amount_cents),
            "recipient_contact_id": contact["id"], "recipient_name": person["name"],
            "recipient_phone_masked": mask_phone(contact["phone"]), "collection_note": collection_payload["note"]}


def prepare_refund(request_id: str, session_id: str, source_transaction_id: str, amount_yuan: str) -> dict:
    amount = share_cents(amount_yuan)
    if amount <= 0:
        raise HTTPException(422, "退款金额须大于 0")
    with db_session() as conn:
        details = _refund_snapshot(conn, request_id, source_transaction_id, amount, session_id)
        available = available_cents(conn)
        if amount > available:
            raise HTTPException(409, f"可用余额不足，当前可退金额上限为 ¥{money(max(available, 0))}")
        tier = "red" if amount > 100_000 else "yellow"
        action = create_action(conn, session_id, "aa_refund", tier, details)
        return reply("请确认将这部分已到账 AA 回款退回原付款人。退款会从可用余额扣除并保留原回款记录。",
                     session_id, "offline", pending_action=action)


def execute_refund(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    action = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    if (action is None or action["type"] != "aa_refund" or action["session_id"] != session_id
            or action["tier"] != ("red" if payload["amount_cents"] > 100_000 else "yellow")):
        raise HTTPException(409, "退款授权记录不一致，未执行")
    fresh = _refund_snapshot(conn, payload["request_id"], payload["source_transaction_id"],
                             payload["amount_cents"], session_id)
    if payload != fresh:
        raise HTTPException(409, "回款、收款人或可退金额已变化，请重新核对退款计划")
    debit(conn, payload["amount_cents"])
    transaction_id = f"aa-refund-{action_id}"
    timestamp = business_now(conn).isoformat(timespec="seconds")
    conn.execute("""INSERT INTO transactions(id,account_id,posted_on,direction,amount_cents,counterparty,category,note,action_id)
        VALUES(?,?,?,'out',?,?,'AA退款',?,?)""",
        (transaction_id, ACCOUNT_ID, business_date(conn), payload["amount_cents"], payload["recipient_name"],
         f"退回 AA 回款：{payload['collection_note']}", action_id))
    conn.execute("INSERT INTO aa_refunds(id,collection_id,request_id,source_transaction_id,transaction_id,amount_cents,created_at) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (action_id, payload["collection_id"], payload["request_id"], payload["source_transaction_id"],
                  transaction_id, payload["amount_cents"], timestamp))
    audit(conn, session_id, "aa_receipt_refunded", {"collection_id": payload["collection_id"],
          "request_id": payload["request_id"], "source_transaction_id": payload["source_transaction_id"],
          "transaction_id": transaction_id, "amount_yuan": payload["amount_yuan"], "simulated": True})
    return {"status": "completed", "action_id": action_id, "collection_id": payload["collection_id"],
            "request_id": payload["request_id"], "source_transaction_id": payload["source_transaction_id"],
            "transaction_id": transaction_id, "amount_yuan": payload["amount_yuan"],
            "message": f"已模拟退回 {payload['recipient_name']} ¥{payload['amount_yuan']}；原回款保留，具体业务关系请另行核对。"}


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
        amount = request["amount_cents"] - _request_received(conn, request_id)
        if amount <= 0:
            raise HTTPException(409, "收款请求已收齐，请查看逐笔回执")
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


def simulate_installment(request_id: str, session_id: str, amount_yuan: str, idempotency_key: str) -> dict:
    if os.environ.get("VERALANE_DEMO_CONTROLS", "1") != "1":
        raise HTTPException(403, "模拟付款控制已关闭")
    amount = share_cents(amount_yuan)
    if amount <= 0:
        raise HTTPException(422, "分次回款金额须大于 0")
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
        contact = conn.execute("SELECT * FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
                               (request["contact_id"], USER_ID)).fetchone()
        if contact is None or contact_fingerprint(contact) != person["contact_fingerprint"]:
            raise HTTPException(409, "付款人信息已变化，未入账")

        existing = conn.execute("SELECT * FROM aa_partial_payments WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        if existing:
            if existing["request_id"] != request_id or existing["amount_cents"] != amount:
                raise HTTPException(409, "分次回款编号已用于其他金额或收款人")
            result = public_collection(conn, group)
            conn.commit()
            return result
        if request["status"] != "pending" or group["status"] not in ("pending", "partial"):
            raise HTTPException(409, "收款请求已关闭或收齐，未入账")
        outstanding = request["amount_cents"] - _request_received(conn, request_id)
        if amount > outstanding:
            raise HTTPException(409, f"本次金额超过待收余额 ¥{money(outstanding)}，未入账")
        received = _received(conn, group["id"])
        target = share_cents(payload["receivable_yuan"])
        if received + amount > target:
            raise HTTPException(409, "到账将超过收款单应收金额，未入账")

        timestamp = business_now(conn).isoformat(timespec="seconds")
        key_hash = hashlib.sha256(f"{request_id}:{idempotency_key}".encode()).hexdigest()[:32]
        tx_id = f"aa-part-{key_hash}"
        conn.execute("UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?", (amount, ACCOUNT_ID))
        conn.execute("INSERT INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note, action_id) "
                     "VALUES (?, ?, ?, 'in', ?, ?, 'AA分次回款', ?, ?)",
                     (tx_id, ACCOUNT_ID, business_date(conn), amount, person["name"], payload["note"], f"aa-part-action-{key_hash}"))
        conn.execute("INSERT INTO aa_partial_payments (idempotency_key, request_id, transaction_id, amount_cents, created_at) "
                     "VALUES (?, ?, ?, ?, ?)", (idempotency_key, request_id, tx_id, amount, timestamp))
        request_total = _request_received(conn, request_id)
        is_paid = request_total == request["amount_cents"]
        if request_total > request["amount_cents"]:
            raise HTTPException(409, "累计回款超过该联系人应付金额，已回滚")
        conn.execute("UPDATE aa_requests SET status = ?, paid_at = ?, transaction_id = ? WHERE id = ?",
                     ("paid" if is_paid else "pending", timestamp if is_paid else None, tx_id if is_paid else None, request_id))
        conn.execute("UPDATE aa_collections SET status = ? WHERE id = ?",
                     ("completed" if received + amount == target else "partial", group["id"]))
        audit(conn, session_id, "aa_installment_received", {"collection_id": group["id"], "request_id": request_id,
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
