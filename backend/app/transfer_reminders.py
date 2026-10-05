"""Date-based, in-app notes about transfers; these never create money actions."""
from __future__ import annotations

import re
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .clock import business_date, business_now
from .db import audit, db_session
from .schedule_time import DATE_PATTERN, RELATIVES, instruction_text

router = APIRouter(prefix="/api")


def init_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS transfer_reminders (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        due_on TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','due','completed','cancelled')),
        completed_at TEXT,
        cancelled_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS transfer_reminders_due ON transfer_reminders(status,due_on)")


def _due_date(message: str, today: date) -> tuple[date | None, str | None]:
    text = instruction_text(message)
    # This v1 handles one calendar date only. Relative conditions and recurrence
    # must never be silently collapsed into a single date.
    if re.search(r"每[天日周月年]|每个|工作日|周[一二三四五六日天末]|星期|礼拜|下周|下星期|月底|月末|以后|之前|之后|稍后|等到|到账后|完成后|取消后|转账后|晚点|过几天", text):
        return None, "我目前只支持按一个具体日期创建转账站内提醒；周期或条件提醒未创建。请改用“提醒我10月6日核对给林悦的转账”。"
    if re.search(r"\d{1,2}[:：]\d{2}|\d+(?:点|时)|上午|下午|早上|晚上|中午|凌晨", text):
        return None, "提醒目前按日期触发，不支持指定钟点；未创建提醒。请只提供一个日期，例如“明天”或“10月6日”。"
    relative_pattern = "|".join(sorted(RELATIVES, key=len, reverse=True))
    relatives = list(re.finditer(relative_pattern, text))
    dates = list(DATE_PATTERN.finditer(text))
    if len(relatives) + len(dates) != 1:
        return None, "请提供一个明确的未来日期，例如“明天”或“10月6日”；未创建提醒。"
    try:
        if relatives:
            due = today + timedelta(days=RELATIVES[relatives[0].group()])
        else:
            match = dates[0]
            due = date(int(match["iso_year"] or match["cn_year"] or today.year),
                       int(match["iso_month"] or match["cn_month"]),
                       int(match["iso_day"] or match["cn_day"]))
    except (ValueError, TypeError):
        return None, "日期无效，未创建提醒。请重新提供一个实际存在的日历日期。"
    if due < today:
        return None, "提醒日期已过去，未创建提醒。请提供今天或未来的日期。"
    return due, None


def create_from_message(session_id: str, message: str) -> dict:
    with db_session(immediate=True) as conn:
        today = date.fromisoformat(business_date(conn))
        due, error = _due_date(message, today)
        if error:
            return {"created": False, "message": f"转账提醒：{error}"}
        body = re.sub(r"^(?:请)?(?:提醒|备忘)(?:我)?", "", message.strip()).strip(" ，,。:：")
        body = body or "核对转账事项"
        reminder_id = f"transfer-reminder-{uuid.uuid4().hex}"
        now = business_now(conn).isoformat(timespec="seconds")
        conn.execute("INSERT INTO transfer_reminders(id,session_id,title,body,due_on,created_at,status) VALUES(?,?,?,?,?,?,'pending')",
                     (reminder_id, session_id, "转账提醒", body, due.isoformat(), now))
        audit(conn, session_id, "transfer_reminder_created", {"reminder_id": reminder_id, "due_on": due.isoformat()})
        return {"created": True, "id": reminder_id, "due_on": due.isoformat(), "body": body}


def mark_due(conn) -> int:
    today = business_date(conn)
    cursor = conn.execute("UPDATE transfer_reminders SET status='due' WHERE status='pending' AND due_on<=?", (today,))
    return cursor.rowcount


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=100)


@router.get("/transfer-reminders")
def list_transfer_reminders(session_id: str = Query(min_length=1, max_length=100)):
    with db_session() as conn:
        today = business_date(conn)
        rows = conn.execute("SELECT * FROM transfer_reminders WHERE session_id=? ORDER BY due_on,id", (session_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            if item["status"] == "pending" and item["due_on"] <= today:
                item["status"] = "due"
            items.append(item)
        return {"items": items, "as_of": today}


def _set_status(reminder_id: str, session_id: str, status: str) -> dict:
    with db_session(immediate=True) as conn:
        row = conn.execute("SELECT * FROM transfer_reminders WHERE id=? AND session_id=?", (reminder_id, session_id)).fetchone()
        if row is None:
            raise HTTPException(404, "未找到此会话的转账提醒")
        if row["status"] not in ("pending", "due"):
            return {"status": row["status"], "id": reminder_id}
        now = business_now(conn).isoformat(timespec="seconds")
        if status == "completed":
            conn.execute("UPDATE transfer_reminders SET status='completed',completed_at=? WHERE id=?", (now, reminder_id))
            event = "transfer_reminder_completed"
        else:
            conn.execute("UPDATE transfer_reminders SET status='cancelled',cancelled_at=? WHERE id=?", (now, reminder_id))
            event = "transfer_reminder_cancelled"
        audit(conn, session_id, event, {"reminder_id": reminder_id})
        return {"status": status, "id": reminder_id}


@router.post("/transfer-reminders/{reminder_id}/complete")
def complete_transfer_reminder(reminder_id: str, request: SessionRequest):
    return _set_status(reminder_id, request.session_id, "completed")


@router.post("/transfer-reminders/{reminder_id}/cancel")
def cancel_transfer_reminder(reminder_id: str, request: SessionRequest):
    return _set_status(reminder_id, request.session_id, "cancelled")
