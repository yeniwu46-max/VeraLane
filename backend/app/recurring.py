"""Finite monthly and batch transfers with scoped parent/child authorizations."""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from typing import Any

from fastapi import HTTPException

from . import db
from .clock import SHANGHAI, business_date, business_now
from .execution_controls import available_cents, require_verified, requires_red_tier


SNAPSHOT_FIELDS = ("contact_id", "recipient", "phone_masked", "contact_fingerprint", "amount_cents", "amount_yuan", "note")


def interpret(sid: str, message: str, kind: str) -> dict[str, Any]:
    """Conservative form drafts only: missing dates and identities stay missing."""
    from .agent import extract_amount_text
    from .schedule_time import instruction_text
    from .service import cents_from_yuan
    text = instruction_text(message)
    note_match = re.search(r"(?:备注|用途)\s*[:：为是]?\s*(.+)", message)
    note = note_match[1].strip()[:100] if note_match else "周期转账" if kind == "recurring" else "批量转账"
    issues = []
    with db.db_session() as conn:
        contacts = list(conn.execute("SELECT id,name,phone FROM contacts WHERE user_id=? AND verified=1", (db.USER_ID,)))
    def contact_id(token: str) -> str | None:
        matches = [row for row in contacts if row["name"] == token or row["phone"] == token]
        return matches[0]["id"] if len(matches) == 1 else None
    if kind == "recurring":
        names = list(dict.fromkeys(row["name"] for row in contacts if row["name"] in text))
        phones = re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)
        recipient = contact_id(phones[0]) if len(phones) == 1 else contact_id(names[0]) if len(names) == 1 else None
        amount = extract_amount_text(text)
        amount = _money(cents_from_yuan(amount)) if cents_from_yuan(amount) else None
        if len(re.findall(r"元|块", text)) != 1:
            amount = None
        day = re.search(r"每月\s*(\d{1,2})\s*(?:日|号)", text)
        count = re.search(r"(?:连续|一共|共)\s*([一二两三四五六七八九十\d]{1,3})\s*(?:次|期|个月|月)", text)
        numerals = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'十一':11,'十二':12}
        count_value = (int(count[1]) if count[1].isdigit() else numerals.get(count[1])) if count else None
        first = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)(?:\+08:00)?", text)
        first_at = f'{first[1]}T{first[2]}' if first else None
        cn_dates = re.findall(r'(?<!\d)(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*(\d{1,2}):(\d{2})', text)
        if len(cn_dates)==1 and first_at is None:
            year,month,date_day,hour,minute=map(int,cn_dates[0])
            try:
                first_at=datetime(year,month,date_day,hour,minute).isoformat(timespec='minutes')
            except ValueError:
                issues.append('首个日期或时刻无效，请重新填写。')
        if len(cn_dates)+len(re.findall(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}',text))>1:
            first_at=None
            issues.append('检测到多个首日时间，请明确唯一首个执行时间。')
        draft = {"contact_id": recipient, "amount_yuan": amount, "note": note,
                 "first_at": first_at,
                 "monthly_day": int(day[1]) if day and 1 <= int(day[1]) <= 31 else None,
                 "count": count_value if count_value and 1 <= count_value <= 12 else None}
        labels = {"contact_id": "唯一已验证联系人（同名请按手机号手选）", "amount_yuan": "单笔金额",
                  "first_at": "首个完整年月日与北京时间", "monthly_day": "每月日号", "count": "实际执行次数（1–12期）"}
        issues.extend(f"请补充{label}。" for key, label in labels.items() if draft[key] is None)
    elif kind == "batch":
        draft = {"items": []}
        body = re.sub(r"^(?:请|帮我)?\s*(?:批量转账\s*[:：]?)?", "", text.strip())
        pieces = [piece.strip() for piece in re.split(r"[，,；;\n]+", body) if piece.strip()]
        for piece in pieces:
            matched = re.fullmatch(r"(?:转给|给|向)?\s*([^\d\s+\-.,，]+|1[3-9]\d{9})\s*(?:转)?\s*([+\-]?\d+(?:\.\d+)?)\s*元[。]?", piece)
            if not matched or cents_from_yuan(matched[2]) is None:
                issues.append("批量草稿请逐项写为“给林悦100元，给陈晨200元”，不支持金额算式或合并条件。")
                continue
            recipient = matched[1]
            id = contact_id(recipient)
            draft["items"].append({"contact_id": id, "recipient": recipient, "amount_yuan": _money(cents_from_yuan(matched[2])), "note": note})
            if id is None:
                issues.append(f"请手动选择“{recipient}”对应的唯一已验证联系人。")
        if not 2 <= len(draft["items"]) <= 10:
            issues.append("请明确 2 至 10 行转账。")
        ids = [item["contact_id"] for item in draft["items"] if item["contact_id"]]
        if len(ids) != len(set(ids)):
            issues.append("草稿有重复联系人，请逐项核对。")
    else:
        raise HTTPException(422, "请选择 recurring 或 batch 草稿类型")
    return {"status": "needs_clarification" if issues else "draft", "mode": "offline", "draft": draft,
            "needs_review": list(dict.fromkeys(issues)), "message": "仅整理输入草稿。请逐项核对并补齐，再生成待确认计划；当前未授权或转账。"}


def init_schema(conn: sqlite3.Connection) -> None:
    for sql in (
        """CREATE TABLE IF NOT EXISTS recurring_plans (
            id TEXT PRIMARY KEY REFERENCES actions(id), session_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','completed','partially_completed','failed','cancelled')),
            created_at TEXT NOT NULL,finished_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS recurring_occurrences (
            id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES recurring_plans(id),
            occurrence_index INTEGER NOT NULL,execute_at TEXT NOT NULL,window_expires_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','expired','cancelled')),
            transaction_id TEXT UNIQUE REFERENCES transactions(id),result_json TEXT,failure_reason TEXT,finished_at TEXT,
            UNIQUE(plan_id,occurrence_index))""",
        "CREATE INDEX IF NOT EXISTS recurring_due ON recurring_occurrences(status,execute_at)",
        """CREATE TABLE IF NOT EXISTS transfer_batches (
            id TEXT PRIMARY KEY REFERENCES actions(id),session_id TEXT NOT NULL,
            account_id TEXT NOT NULL REFERENCES accounts(id),payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','completed','partially_completed','failed')),
            created_at TEXT NOT NULL,finished_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS transfer_batch_items (
            id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES transfer_batches(id),item_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
            transaction_id TEXT UNIQUE REFERENCES transactions(id),result_json TEXT,failure_reason TEXT,
            UNIQUE(batch_id,item_id))""",
    ):
        conn.execute(sql)


def _money(amount: int) -> str:
    from .service import money
    return money(amount)


def _transfer(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    from .service import cents_from_yuan, contact_fingerprint, mask_phone
    raw = data.get("amount_yuan")
    amount = cents_from_yuan(raw) if isinstance(raw, str) and re.fullmatch(r"\d+(?:\.\d{1,2})?", raw.strip()) else None
    if amount is None:
        raise HTTPException(422, "每笔金额须大于零、精确到分且不超过 100000 元")
    note = data.get("note", "转账")
    if not isinstance(note, str) or len(note) > 100:
        raise HTTPException(422, "备注最多 100 字")
    contact = conn.execute("SELECT * FROM contacts WHERE id=? AND user_id=? AND verified=1", (data.get("contact_id"), db.USER_ID)).fetchone()
    if contact is None:
        raise HTTPException(404, "未找到已验证的收款人")
    return {"contact_id": contact["id"], "recipient": contact["name"], "phone_masked": mask_phone(contact["phone"]),
            "contact_fingerprint": contact_fingerprint(contact), "amount_cents": amount, "amount_yuan": _money(amount), "note": note.strip() or "转账"}


def _occurrences(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    try:
        raw = data["first_at"]
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:\+08:00)?", raw):
            raise ValueError
        start = datetime.fromisoformat(raw)
        if start.tzinfo is None:
            start = start.replace(tzinfo=SHANGHAI)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(422, "请提供完整首日和北京时间，例如 2026-10-05T09:00:00+08:00")
    day, count = data.get("monthly_day"), data.get("count")
    if type(day) is not int or not 1 <= day <= 31 or type(count) is not int or not 1 <= count <= 12:
        raise HTTPException(422, "每月日号为 1–31，实际执行次数为 1–12")
    if start.day != day:
        raise HTTPException(422, "首日的日期必须与每月日号一致")
    if start <= business_now(conn):
        raise HTTPException(422, "首个执行时间必须晚于当前业务时钟")
    values, skipped = [], []
    year, month = start.year, start.month
    try:
        while len(values) < count:
            if monthrange(year, month)[1] < day:
                skipped.append(f"{year:04d}-{month:02d}")
            else:
                at = start.replace(year=year, month=month, day=day)
                values.append({"index": len(values) + 1, "execute_at": at.isoformat(timespec="seconds"),
                               "window_expires_at": (at + timedelta(minutes=10)).isoformat(timespec="seconds")})
            month += 1
            if month > 12:
                month, year = 1, year + 1
    except (ValueError, OverflowError):
        raise HTTPException(422, "周期日期超出支持范围")
    return start.isoformat(timespec="seconds"), values, skipped


def _recurring_payload(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    transfer = _transfer(conn, data)
    first_at, dates, skipped = _occurrences(conn, data)
    total = transfer["amount_cents"] * len(dates)
    return {**transfer, "first_at": first_at, "monthly_day": data["monthly_day"], "count": len(dates),
            "occurrences": dates, "skipped_months": skipped, "total_cents": total, "total_yuan": _money(total),
            "timezone": "Asia/Shanghai", "execution_window_minutes": 10,
            "notice": "仅执行列出的日期，不预留余额。无该日的月份跳过；失败或超窗不补扣，可取消剩余期次。"}


def _batch_payload(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(items, list) or not 2 <= len(items) <= 10:
        raise HTTPException(422, "请选择 2 至 10 位不同的已验证收款人")
    contacts = [item.get("contact_id") for item in items]
    if len(set(contacts)) != len(contacts):
        raise HTTPException(422, "同一批次请勿重复添加相同联系人")
    values = [{"item_id": str(index), **_transfer(conn, item)} for index, item in enumerate(items, 1)]
    total = sum(item["amount_cents"] for item in values)
    return {"items": values, "total_cents": total, "total_yuan": _money(total), "count": len(values),
            "notice": "按列表顺序逐笔执行。业务失败保留每笔结果，不重扣成功项，不自动撤销已完成转账。"}


def preview_recurring(data: dict[str, Any]) -> dict[str, Any]:
    with db.db_session() as conn:
        return {**_recurring_payload(conn, data), "available_now_yuan": _money(available_cents(conn))}


def preview_batch(data: dict[str, Any]) -> dict[str, Any]:
    with db.db_session() as conn:
        payload = _batch_payload(conn, data["items"])
        available = available_cents(conn)
        return {**payload, "available_now_yuan": _money(available),
                "warnings": ["当前可用余额不足以完成全部转账，确认后可能出现部分失败。"] if available < payload["total_cents"] else []}


def _sent_today(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM transactions WHERE account_id=? AND posted_on=? AND direction='out' AND category='转账'",
                        (db.ACCOUNT_ID, business_date(conn))).fetchone()[0]


def prepare_recurring(data: dict[str, Any]) -> dict[str, Any]:
    from .service import create_action, reply
    with db.db_session() as conn:
        payload = _recurring_payload(conn, data)
        action = create_action(conn, data["session_id"], "recurring_transfer", "red" if requires_red_tier(payload["total_cents"]) else "yellow", payload)
        return reply("请核对全部执行日期、每笔金额和总授权金额，再确认有限次数的周期转账。", data["session_id"], "offline", pending_action=action)


def prepare_batch(data: dict[str, Any]) -> dict[str, Any]:
    from .service import create_action, reply
    with db.db_session() as conn:
        payload = _batch_payload(conn, data["items"])
        tier = "red" if requires_red_tier(_sent_today(conn) + payload["total_cents"]) else "yellow"
        action = create_action(conn, data["session_id"], "batch_transfer", tier, payload)
        return reply("请逐行核对收款人、金额和备注，确认后按顺序执行并保留每笔结果。", data["session_id"], "offline", pending_action=action)


def _parent(conn: sqlite3.Connection, action_id: str, sid: str, kind: str, payload: dict[str, Any], status: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=? AND type=?", (action_id, sid, kind)).fetchone()
    if row is None or row["status"] != status or json.loads(row["payload_json"]) != payload:
        raise HTTPException(409, "父计划授权与当前执行范围不一致")
    if status == "pending" and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(409, "计划确认已过期，请重新发起")
    if row["tier"] == "yellow" and requires_red_tier(payload["total_cents"]):
        raise HTTPException(403, "计划总额超过普通确认范围，请完成模拟强验证")
    if status == "pending":
        require_verified(conn, row)
    return row


def execute_recurring(conn: sqlite3.Connection, action_id: str, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
    _parent(conn, action_id, sid, "recurring_transfer", payload, "pending")
    if _recurring_payload(conn, payload) != payload:
        raise HTTPException(409, "日期或收款人资料已变化，请重新生成周期计划")
    conn.execute("INSERT INTO recurring_plans(id,session_id,account_id,payload_json,status,created_at) VALUES (?,?,?,?,'active',?)",
                 (action_id, sid, db.ACCOUNT_ID, json.dumps(payload, ensure_ascii=False), db.utc_now()))
    for item in payload["occurrences"]:
        conn.execute("""INSERT INTO recurring_occurrences(id,plan_id,occurrence_index,execute_at,window_expires_at,status)
            VALUES (?,?,?,?,?,'pending')""", (f"recurring-{action_id}-{item['index']}", action_id, item["index"], item["execute_at"], item["window_expires_at"]))
    db.audit(conn, sid, "recurring_transfer_created", {"plan_id": action_id, "count": payload["count"], "total_cents": payload["total_cents"]})
    return {"status": "completed", "action_id": action_id, "plan_id": action_id, "message": "有限次数的周期转账已建立，尚未扣款或预留资金。"}


def validate_transfer_authorization(conn: sqlite3.Connection, authorization_id: str, child_action_id: str, sid: str, payload: dict[str, Any]) -> str:
    parent = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=?", (authorization_id, sid)).fetchone()
    if parent is None:
        raise HTTPException(404, "未找到该转账的父授权")
    snapshot = json.loads(parent["payload_json"])
    if parent["type"] == "batch_transfer":
        _parent(conn, authorization_id, sid, "batch_transfer", snapshot, "pending")
        group = conn.execute("SELECT * FROM transfer_batches WHERE id=? AND session_id=? AND account_id=? AND status='running'", (authorization_id, sid, db.ACCOUNT_ID)).fetchone()
        child = conn.execute("SELECT * FROM transfer_batch_items WHERE id=? AND batch_id=? AND status='pending'", (child_action_id, authorization_id)).fetchone()
        if group is None or child is None or json.loads(group["payload_json"]) != snapshot:
            raise HTTPException(409, "批次或子项已失效，未转账")
        matches = [item for item in snapshot["items"] if item["item_id"] == child["item_id"]]
        if len(matches) != 1 or child_action_id != f"batch-{authorization_id}-{child['item_id']}":
            raise HTTPException(409, "批次子操作编号与授权不一致")
        expected = {key: matches[0][key] for key in SNAPSHOT_FIELDS}
    elif parent["type"] == "recurring_transfer":
        _parent(conn, authorization_id, sid, "recurring_transfer", snapshot, "completed")
        group = conn.execute("SELECT * FROM recurring_plans WHERE id=? AND session_id=? AND account_id=? AND status='active'", (authorization_id, sid, db.ACCOUNT_ID)).fetchone()
        child = conn.execute("SELECT * FROM recurring_occurrences WHERE id=? AND plan_id=? AND status='pending'", (child_action_id, authorization_id)).fetchone()
        if group is None or child is None or json.loads(group["payload_json"]) != snapshot:
            raise HTTPException(409, "周期或当前期次已失效，未转账")
        matches = [item for item in snapshot["occurrences"] if item["index"] == child["occurrence_index"]]
        if len(matches) != 1 or child_action_id != f"recurring-{authorization_id}-{child['occurrence_index']}" or len(snapshot["occurrences"]) != snapshot["count"]:
            raise HTTPException(409, "周期次数或期次编号与授权不一致")
        date = matches[0]
        if child["execute_at"] != date["execute_at"] or child["window_expires_at"] != date["window_expires_at"]:
            raise HTTPException(409, "周期执行日期与授权不一致")
        if not datetime.fromisoformat(date["execute_at"]) <= business_now(conn) < datetime.fromisoformat(date["window_expires_at"]):
            raise HTTPException(409, "当前时间不在已授权的执行窗口")
        expected = {key: snapshot[key] for key in SNAPSHOT_FIELDS}
    else:
        raise HTTPException(403, "父授权类型不支持周期或批量转账")
    if payload != expected:
        raise HTTPException(409, "转账对象、金额、备注或联系人指纹与子项授权不一致")
    return parent["tier"]


def execute_batch(conn: sqlite3.Connection, action_id: str, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
    from .service import execute_transfer
    _parent(conn, action_id, sid, "batch_transfer", payload, "pending")
    # Structural integrity is checked against the frozen plan. Contact changes
    # are each line's business failure and must not silently discard other lines.
    if (len(payload["items"]) != payload["count"] or not 2 <= payload["count"] <= 10
            or sum(item["amount_cents"] for item in payload["items"]) != payload["total_cents"]):
        raise HTTPException(409, "批量金额或行数与授权不一致")
    conn.execute("INSERT INTO transfer_batches(id,session_id,account_id,payload_json,status,created_at) VALUES (?,?,?,?,'running',?)",
                 (action_id, sid, db.ACCOUNT_ID, json.dumps(payload, ensure_ascii=False), db.utc_now()))
    for item in payload["items"]:
        conn.execute("INSERT INTO transfer_batch_items(id,batch_id,item_id,status) VALUES (?,?,?,'pending')", (f"batch-{action_id}-{item['item_id']}", action_id, item["item_id"]))
    for item in payload["items"]:
        child_id = f"batch-{action_id}-{item['item_id']}"
        child_payload = {key: item[key] for key in SNAPSHOT_FIELDS}
        conn.execute("SAVEPOINT batch_line")
        try:
            result = execute_transfer(conn, child_id, sid, child_payload, authorization_id=action_id)
            conn.execute("UPDATE transfer_batch_items SET status='completed',transaction_id=?,result_json=? WHERE id=?", (result["transaction_id"], json.dumps(result, ensure_ascii=False), child_id))
            conn.execute("RELEASE batch_line")
        except HTTPException as exc:
            conn.execute("ROLLBACK TO batch_line")
            conn.execute("RELEASE batch_line")
            conn.execute("UPDATE transfer_batch_items SET status='failed',failure_reason=? WHERE id=?", (str(exc.detail), child_id))
    completed = conn.execute("SELECT COUNT(*) FROM transfer_batch_items WHERE batch_id=? AND status='completed'", (action_id,)).fetchone()[0]
    status = "completed" if completed == payload["count"] else "partially_completed" if completed else "failed"
    conn.execute("UPDATE transfer_batches SET status=?,finished_at=? WHERE id=?", (status, db.utc_now(), action_id))
    db.audit(conn, sid, "batch_transfer_finished", {"batch_id": action_id, "status": status, "completed": completed, "count": payload["count"]})
    return {"status": status, "action_id": action_id, "batch_id": action_id, "completed_count": completed,
            "failed_count": payload["count"] - completed, "message": f"批量转账已处理：成功 {completed} 笔，失败 {payload['count']-completed} 笔。", "batch": _public_batch(conn, action_id, sid)}


def _owned_plan(conn: sqlite3.Connection, id: str, sid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM recurring_plans WHERE id=? AND session_id=? AND account_id=?", (id, sid, db.ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该周期转账")
    return row


def _public_recurring(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"])
    occurrences = []
    for item in conn.execute("SELECT * FROM recurring_occurrences WHERE plan_id=? ORDER BY occurrence_index", (row["id"],)):
        value = dict(item)
        value["result"] = json.loads(value.pop("result_json")) if item["result_json"] else None
        occurrences.append(value)
    return {**payload, "id": row["id"], "status": row["status"], "created_at": row["created_at"], "finished_at": row["finished_at"], "occurrences": occurrences}


def list_recurring(sid: str) -> dict[str, Any]:
    with db.db_session() as conn:
        rows = conn.execute("SELECT * FROM recurring_plans WHERE session_id=? AND account_id=? ORDER BY rowid DESC", (sid, db.ACCOUNT_ID)).fetchall()
        return {"plans": [_public_recurring(conn, row) for row in rows]}


def get_recurring(id: str, sid: str) -> dict[str, Any]:
    with db.db_session() as conn:
        return _public_recurring(conn, _owned_plan(conn, id, sid))


def _public_batch(conn: sqlite3.Connection, id: str, sid: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM transfer_batches WHERE id=? AND session_id=? AND account_id=?", (id, sid, db.ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该批量转账")
    payload = json.loads(row["payload_json"])
    items = []
    for detail in payload["items"]:
        state = conn.execute("SELECT * FROM transfer_batch_items WHERE batch_id=? AND item_id=?", (id, detail["item_id"])).fetchone()
        items.append({**detail, "id": state["id"], "status": state["status"], "transaction_id": state["transaction_id"],
                      "failure_reason": state["failure_reason"], "result": json.loads(state["result_json"]) if state["result_json"] else None})
    return {**payload, "items": items, "id": id, "status": row["status"], "created_at": row["created_at"], "finished_at": row["finished_at"]}


def list_batches(sid: str) -> dict[str, Any]:
    with db.db_session() as conn:
        rows = conn.execute("SELECT id FROM transfer_batches WHERE session_id=? AND account_id=? ORDER BY rowid DESC", (sid, db.ACCOUNT_ID)).fetchall()
        return {"batches": [_public_batch(conn, row["id"], sid) for row in rows]}


def get_batch(id: str, sid: str) -> dict[str, Any]:
    with db.db_session() as conn:
        return _public_batch(conn, id, sid)


def prepare_cancel(id: str, sid: str) -> dict[str, Any]:
    from .service import create_action, reply
    with db.db_session() as conn:
        row = _owned_plan(conn, id, sid)
        remaining = [dict(item) for item in conn.execute("SELECT id,execute_at FROM recurring_occurrences WHERE plan_id=? AND status='pending' ORDER BY occurrence_index", (id,))]
        if row["status"] != "active" or not remaining:
            raise HTTPException(409, "没有可取消的未来期次")
        action = create_action(conn, sid, "recurring_cancel", "yellow", {"plan_id": id, "remaining": remaining,
                               "notice": "仅取消尚未执行的期次；已完成转账不会退款。"})
        return reply("请确认取消全部尚未执行的周期期次。", sid, "offline", pending_action=action)


def execute_cancel(conn: sqlite3.Connection, action_id: str, sid: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = _owned_plan(conn, payload["plan_id"], sid)
    if row["status"] == "cancelled":
        return {"status": "completed", "action_id": action_id, "plan_id": row["id"], "message": "剩余期次已取消。"}
    remaining = [dict(item) for item in conn.execute("SELECT id,execute_at FROM recurring_occurrences WHERE plan_id=? AND status='pending' ORDER BY occurrence_index", (row["id"],))]
    if row["status"] != "active" or remaining != payload["remaining"]:
        raise HTTPException(409, "待执行期次已变化，请重新核对取消范围")
    timestamp = business_now(conn).isoformat(timespec="seconds")
    conn.execute("UPDATE recurring_occurrences SET status='cancelled',finished_at=? WHERE plan_id=? AND status='pending'", (timestamp, row["id"]))
    conn.execute("UPDATE recurring_plans SET status='cancelled',finished_at=? WHERE id=?", (timestamp, row["id"]))
    db.audit(conn, sid, "recurring_transfer_cancelled", {"plan_id": row["id"], "cancelled_count": len(remaining)})
    return {"status": "completed", "action_id": action_id, "plan_id": row["id"], "message": "剩余期次已取消；已完成转账未退款。"}


def run_due_recurring(conn: sqlite3.Connection) -> int:
    from .service import execute_transfer
    if not conn.in_transaction:
        raise RuntimeError("Recurring execution requires caller-owned transaction")
    now = business_now(conn)
    timestamp = now.isoformat(timespec="seconds")
    rows = conn.execute("""SELECT o.*,p.session_id,p.payload_json FROM recurring_occurrences o JOIN recurring_plans p ON p.id=o.plan_id
        WHERE o.status='pending' AND p.status='active' AND o.execute_at<=? ORDER BY o.execute_at,o.id""", (timestamp,)).fetchall()
    touched = set()
    for row in rows:
        touched.add(row["plan_id"])
        parent_payload = json.loads(row["payload_json"])
        child_payload = {key: parent_payload[key] for key in SNAPSHOT_FIELDS}
        status, result, reason = "completed", None, None
        # Even an expired row must match the parent snapshot before its date is
        # trusted; invalid indexes do not expand the scope of the authorization.
        dates = [item for item in parent_payload["occurrences"] if item["index"] == row["occurrence_index"]]
        if (len(dates) != 1 or dates[0]["execute_at"] != row["execute_at"]
                or dates[0]["window_expires_at"] != row["window_expires_at"]):
            status, reason = "failed", "执行日期或期次与原授权不一致，未转账"
        elif now >= datetime.fromisoformat(row["window_expires_at"]):
            status, reason = "expired", "已超过本期 10 分钟执行窗口，不补扣"
        else:
            conn.execute("SAVEPOINT recurring_occurrence")
            try:
                result = execute_transfer(conn, row["id"], row["session_id"], child_payload, authorization_id=row["plan_id"])
                conn.execute("RELEASE recurring_occurrence")
            except HTTPException as exc:
                conn.execute("ROLLBACK TO recurring_occurrence")
                conn.execute("RELEASE recurring_occurrence")
                status, reason = "failed", str(exc.detail)
        conn.execute("UPDATE recurring_occurrences SET status=?,transaction_id=?,result_json=?,failure_reason=?,finished_at=? WHERE id=?",
                     (status, result["transaction_id"] if result else None, json.dumps(result, ensure_ascii=False) if result else None, reason, timestamp, row["id"]))
        db.audit(conn, row["session_id"], "recurring_occurrence_finished", {"plan_id": row["plan_id"], "occurrence_id": row["id"], "status": status, "reason": reason})
    for id in touched:
        statuses = [row[0] for row in conn.execute("SELECT status FROM recurring_occurrences WHERE plan_id=?", (id,))]
        if "pending" not in statuses:
            status = "completed" if all(value == "completed" for value in statuses) else "partially_completed" if "completed" in statuses else "failed"
            conn.execute("UPDATE recurring_plans SET status=?,finished_at=? WHERE id=?", (status, timestamp, id))
    return len(rows)
