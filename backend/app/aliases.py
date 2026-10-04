"""Explicit, session-owned contact aliases and evidence-only duplicate hints."""

from __future__ import annotations

from datetime import date, timedelta
import json
import re
import secrets
import sqlite3
from typing import Any

from fastapi import HTTPException

from .clock import business_date
from .db import ACCOUNT_ID, USER_ID, audit, db_session, utc_now
from .schedule_time import instruction_text
from .service import contact_fingerprint, create_action, mask_phone, money


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS contact_aliases (
        id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),session_id TEXT NOT NULL,
        alias TEXT NOT NULL,contact_id TEXT NOT NULL REFERENCES contacts(id),contact_fingerprint TEXT NOT NULL,
        version INTEGER NOT NULL CHECK(version > 0),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(account_id,session_id,alias))""")


def _contact(conn: sqlite3.Connection, contact_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM contacts WHERE id=? AND user_id=? AND verified=1", (contact_id, USER_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到已验证联系人")
    return row


def _owned(conn: sqlite3.Connection, alias_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM contact_aliases WHERE id=? AND account_id=? AND session_id=?", (alias_id, ACCOUNT_ID, session_id)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该会话的联系人别名")
    return row


def _public(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    contact = conn.execute("SELECT * FROM contacts WHERE id=? AND user_id=?", (row["contact_id"], USER_ID)).fetchone()
    return {"id": row["id"], "alias": row["alias"], "version": row["version"], "contact_id": row["contact_id"],
            "recipient": contact["name"] if contact else "联系人不可用", "phone_masked": mask_phone(contact["phone"]) if contact else "",
            "available": bool(contact and contact["verified"] and contact_fingerprint(contact) == row["contact_fingerprint"])}


def _name(conn: sqlite3.Connection, alias: str) -> str:
    alias = alias.strip()
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_-]{0,19}", alias):
        raise HTTPException(422, "别名须为 1–20 个汉字、字母、数字、下划线或短横线，并以汉字或字母开头")
    if conn.execute("SELECT 1 FROM contacts WHERE user_id=? AND name=?", (USER_ID, alias)).fetchone():
        raise HTTPException(422, "别名不能与已保存的联系人姓名相同，以免覆盖真实姓名的同名澄清")
    if any(word in alias for word in ("转账", "转给", "元", "备注", "取消", "明天", "今天", "后天", "每月", "确认")):
        raise HTTPException(422, "请使用简短称呼作为别名，不使用金额、时间或操作指令")
    return alias


def list_aliases(session_id: str) -> dict:
    with db_session() as conn:
        rows = conn.execute("SELECT * FROM contact_aliases WHERE account_id=? AND session_id=? ORDER BY alias,id", (ACCOUNT_ID, session_id))
        return {"aliases": [_public(conn, row) for row in rows]}


def discard_action(session_id: str, action_id: str) -> dict:
    with db_session() as conn:
        action = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=? AND type IN ('alias_upsert','alias_delete')", (action_id, session_id)).fetchone()
        if action is None:
            raise HTTPException(404, "未找到本会话的别名编辑")
        if action["status"] == "completed":
            raise HTTPException(409, "别名编辑已经执行，请刷新后重新编辑")
        if action["status"] == "pending":
            conn.execute("UPDATE actions SET status='failed' WHERE id=?", (action_id,))
            audit(conn, session_id, "contact_alias_draft_discarded", {"action_id": action_id})
        return {"status": "discarded", "message": "这次别名编辑已放弃，旧确认不可再使用。"}


def prepare_upsert(session_id: str, alias: str, contact_id: str, alias_id: str | None = None) -> dict:
    with db_session() as conn:
        alias = _name(conn, alias)
        contact = _contact(conn, contact_id)
        previous = _owned(conn, alias_id, session_id) if alias_id else None
        duplicate = conn.execute("SELECT id FROM contact_aliases WHERE account_id=? AND session_id=? AND alias=?", (ACCOUNT_ID, session_id, alias)).fetchone()
        if duplicate and (previous is None or duplicate["id"] != previous["id"]):
            raise HTTPException(409, "此别名已保存，请在原别名上选择编辑")
        payload = {"alias_id": alias_id or "alias-" + secrets.token_urlsafe(15), "alias": alias, "contact_id": contact["id"],
                   "recipient": contact["name"], "phone_masked": mask_phone(contact["phone"]), "contact_fingerprint": contact_fingerprint(contact),
                   "expected_version": previous["version"] if previous else 0,
                   "previous": dict(previous) if previous else None}
        action = create_action(conn, session_id, "alias_upsert", "yellow", payload)
        return {"session_id": session_id, "mode": "offline", "message": "请确认这个称呼与联系人的对应关系。仅在本会话保存，不会从交易备注推断关系。", "pending_action": action}


def execute_upsert(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    alias = _name(conn, payload["alias"])
    contact = _contact(conn, payload["contact_id"])
    if contact_fingerprint(contact) != payload["contact_fingerprint"]:
        raise HTTPException(409, "联系人信息已变化，请重新核对")
    if payload["expected_version"]:
        previous = _owned(conn, payload["alias_id"], session_id)
        if dict(previous) != payload["previous"] or previous["version"] != payload["expected_version"]:
            raise HTTPException(409, "别名已被修改，请重新核对")
    elif conn.execute("SELECT 1 FROM contact_aliases WHERE id=?", (payload["alias_id"],)).fetchone():
        raise HTTPException(409, "此别名编号已存在，请重新核对")
    duplicate = conn.execute("SELECT id FROM contact_aliases WHERE account_id=? AND session_id=? AND alias=?", (ACCOUNT_ID, session_id, alias)).fetchone()
    if duplicate and duplicate["id"] != payload["alias_id"]:
        raise HTTPException(409, "此别名已被其他操作保存，请重新核对")
    now = utc_now()
    if payload["expected_version"]:
        conn.execute("UPDATE contact_aliases SET alias=?,contact_id=?,contact_fingerprint=?,version=version+1,updated_at=? WHERE id=?", (alias, contact["id"], payload["contact_fingerprint"], now, payload["alias_id"]))
    else:
        conn.execute("INSERT INTO contact_aliases VALUES(?,?,?,?,?,?,1,?,?)", (payload["alias_id"], ACCOUNT_ID, session_id, alias, contact["id"], payload["contact_fingerprint"], now, now))
    audit(conn, session_id, "contact_alias_saved", {"action_id": action_id, "alias_id": payload["alias_id"], "contact_id": contact["id"]})
    return {"status": "completed", "action_id": action_id, "alias": _public(conn, _owned(conn, payload["alias_id"], session_id)), "message": f"已保存别名：{alias} → {contact['name']}（{mask_phone(contact['phone'])}）。"}


def prepare_delete(session_id: str, alias_id: str) -> dict:
    with db_session() as conn:
        row = _owned(conn, alias_id, session_id)
        payload = {"alias_id": alias_id, "alias": row["alias"], "expected_version": row["version"], "previous": dict(row)}
        action = create_action(conn, session_id, "alias_delete", "yellow", payload)
        return {"session_id": session_id, "mode": "offline", "message": "请确认删除这个别名。已执行的转账和已确认的具体收款人不会被删除。", "pending_action": action}


def execute_delete(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    row = _owned(conn, payload["alias_id"], session_id)
    if row["version"] != payload["expected_version"] or dict(row) != payload["previous"]:
        raise HTTPException(409, "别名已变化，请重新核对后删除")
    conn.execute("DELETE FROM contact_aliases WHERE id=? AND account_id=? AND session_id=?", (row["id"], ACCOUNT_ID, session_id))
    audit(conn, session_id, "contact_alias_deleted", {"action_id": action_id, "alias_id": row["id"]})
    return {"status": "completed", "action_id": action_id, "message": f"已删除别名“{row['alias']}”，原联系人和交易记录保留。"}


def resolve_transfer_alias(conn: sqlite3.Connection, session_id: str, message: str) -> dict | None:
    text = instruction_text(message).strip(" ，。！？?!")
    aliases = list(conn.execute("SELECT * FROM contact_aliases WHERE account_id=? AND session_id=?", (ACCOUNT_ID, session_id)))
    matched = [row for row in aliases if text == row["alias"] or re.search(r"(?:给|向|和|与|、)\s*(?:我的)?" + re.escape(row["alias"]) + r"(?=$|[\s\d，,、和与]|转|打|付)", text)]
    if not matched:
        return None
    if len(matched) != 1 or any(re.search(re.escape(row["alias"]) + r"\s*(?:和|与|、)", text) for row in matched):
        return {"status": "ambiguous", "message": "检测到多个转账对象，请一次明确一个收款人。", "choices": [_public(conn, row) for row in matched]}
    row = matched[0]
    contact = conn.execute("SELECT * FROM contacts WHERE id=? AND user_id=? AND verified=1", (row["contact_id"], USER_ID)).fetchone()
    if contact is None or contact_fingerprint(contact) != row["contact_fingerprint"]:
        return {"status": "stale", "message": "这个别名对应的联系人信息已变化，请在别名设置中重新核对后保存。", "alias_id": row["id"], "alias": row["alias"]}
    return {"status": "resolved", "alias_id": row["id"], "alias": row["alias"], "version": row["version"], "contact_id": contact["id"], "recipient": contact["name"], "phone": contact["phone"]}


def similar_transfers(conn: sqlite3.Connection, contact_id: str, amount_cents: int, days: int = 3) -> list[dict]:
    today = date.fromisoformat(business_date(conn))
    start = today - timedelta(days=max(1, min(days, 30)) - 1)
    rows = conn.execute("SELECT t.id,t.posted_on,t.direction,t.amount_cents,t.counterparty,t.category,t.note,a.payload_json FROM transactions t JOIN actions a ON t.action_id=a.id WHERE t.account_id=? AND t.direction='out' AND t.category='转账' AND t.amount_cents=? AND t.posted_on>=? AND t.posted_on<=? AND a.type IN ('transfer','scheduled_transfer') ORDER BY t.posted_on DESC,t.rowid DESC", (ACCOUNT_ID, amount_cents, str(start), str(today))).fetchall()
    result = []
    for row in rows:
        if json.loads(row["payload_json"]).get("contact_id") == contact_id:
            result.append({key: row[key] for key in ("id", "posted_on", "direction", "amount_cents", "counterparty", "category", "note")} | {"amount_yuan": money(row["amount_cents"])})
    return result[:5]


def interpret(session_id: str, message: str) -> dict[str, Any]:
    match = re.fullmatch(r"\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_-]{0,19})\s*是\s*([\u4e00-\u9fffA-Za-z0-9]+)\s*[。.!！]?", message)
    if match is None:
        return {"status": "needs_clarification", "mode": "offline", "message": "请用“房东是林悦”明确别名关系，或在表单选择已验证联系人。"}
    alias, name = match.groups()
    with db_session() as conn:
        contacts = list(conn.execute("SELECT * FROM contacts WHERE user_id=? AND verified=1 AND (name=? OR phone=?) ORDER BY id", (USER_ID, name, name)))
    if len(contacts) != 1:
        return {"status": "needs_clarification", "mode": "offline", "message": "请在表单选择具体联系人后保存；同名联系人需要核对手机号。", "alias": alias,
                "choices": [{"id": row["id"], "name": row["name"], "phone_masked": mask_phone(row["phone"])} for row in contacts]}
    return {"status": "ok", **prepare_upsert(session_id, alias, contacts[0]["id"])}
