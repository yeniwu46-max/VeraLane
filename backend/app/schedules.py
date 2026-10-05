"""Durable one-off authorizations and atomic execution against the local ledger."""

import json
import os
import sqlite3
from datetime import datetime

from fastapi import HTTPException

from .clock import business_now, clock_info
from .db import ACCOUNT_ID, USER_ID, audit, connect, db_session, utc_now
from .service import contact_fingerprint, execute_transfer


def create_schedule(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict) -> dict:
    if datetime.fromisoformat(payload["execute_at"]) <= business_now(conn):
        raise HTTPException(409, "预约时间已过去，请重新生成计划")
    contact = conn.execute("SELECT * FROM contacts WHERE id = ? AND user_id = ? AND verified = 1",
                           (payload["contact_id"], USER_ID)).fetchone()
    if contact is None or contact_fingerprint(contact) != payload["contact_fingerprint"]:
        raise HTTPException(409, "收款人信息已变化，请重新核对")
    if not 0 < payload["amount_cents"] <= 100_000:
        raise HTTPException(403, "金额超出当前原型可执行范围")
    conn.execute(
        "INSERT INTO scheduled_transfers (id, session_id, account_id, payload_json, execute_at, expires_at, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (action_id, session_id, ACCOUNT_ID, json.dumps(payload, ensure_ascii=False),
         payload["execute_at"], payload["window_expires_at"], utc_now()),
    )
    audit(conn, session_id, "schedule_authorized", {"schedule_id": action_id, "execute_at": payload["execute_at"]})
    return {"status": "completed", "schedule_id": action_id, "action_id": action_id,
            "message": "预约已建立，尚未扣款。到期通过检查后自动执行，可在“我的预约”中查看或取消。"}


def public_task(row: sqlite3.Row) -> dict:
    payload = json.loads(row["payload_json"])
    return {"id": row["id"], "status": row["status"],
            **{key: payload[key] for key in ("recipient", "phone_masked", "amount_yuan", "note")},
            "execute_at": row["execute_at"], "expires_at": row["expires_at"],
            "created_at": row["created_at"], "authorized_at": row["created_at"],
            "finished_at": row["finished_at"], "failure_reason": row["failure_reason"],
            "transaction_id": row["transaction_id"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None}


def list_schedules(session_id: str) -> dict:
    with db_session() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_transfers WHERE session_id = ? AND account_id = ? "
            "ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, execute_at, id",
            (session_id, ACCOUNT_ID),
        ).fetchall()
        info = clock_info(conn)
        info["controls_enabled"] = os.environ.get("VERALANE_DEMO_CONTROLS", "1") == "1"
        return {"clock": info, "tasks": [public_task(row) for row in rows]}


def cancel_schedule(task_id: str, session_id: str) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM scheduled_transfers WHERE id = ? AND session_id = ? AND account_id = ?",
                           (task_id, session_id, ACCOUNT_ID)).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该预约")
        if row["status"] == "pending":
            conn.execute("UPDATE scheduled_transfers SET status = 'cancelled', finished_at = ? WHERE id = ?",
                         (business_now(conn).isoformat(timespec="seconds"), task_id))
            audit(conn, session_id, "schedule_cancelled", {"schedule_id": task_id})
            row = conn.execute("SELECT * FROM scheduled_transfers WHERE id = ?", (task_id,)).fetchone()
        # Return the actual winner of the cancellation/execution race.
        result = public_task(row)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _execute_due(conn: sqlite3.Connection) -> int:
    now = business_now(conn)
    timestamp = now.isoformat(timespec="seconds")
    due = conn.execute("SELECT * FROM scheduled_transfers WHERE status = 'pending' AND execute_at <= ? "
                       "ORDER BY execute_at, created_at, id", (timestamp,)).fetchall()
    for row in due:
        payload = json.loads(row["payload_json"])
        status, reason, result = "completed", None, None
        # Execution indexes are data too: they must match the authorized snapshot.
        if row["execute_at"] != payload.get("execute_at") or row["expires_at"] != payload.get("window_expires_at"):
            status, reason = "failed", "预约执行时间与授权不一致，未转账。"
        elif now >= datetime.fromisoformat(row["expires_at"]):
            status, reason = "expired", "已错过预约时间后的 10 分钟执行窗口，未补扣；请重新预约。"
        else:
            auth = conn.execute("SELECT * FROM actions WHERE id = ?", (row["id"],)).fetchone()
            authorized = (auth and auth["status"] == "completed" and auth["tier"] == "yellow"
                          and auth["type"] == "scheduled_transfer" and auth["session_id"] == row["session_id"]
                          and row["account_id"] == ACCOUNT_ID and json.loads(auth["payload_json"]) == payload)
            if not authorized:
                status, reason = "failed", "预约授权无效，未转账。"
            else:
                conn.execute("SAVEPOINT scheduled_execution")
                try:
                    result = execute_transfer(conn, row["id"], row["session_id"], payload)
                    conn.execute("RELEASE scheduled_execution")
                except HTTPException as exc:
                    conn.execute("ROLLBACK TO scheduled_execution")
                    conn.execute("RELEASE scheduled_execution")
                    status, reason = "failed", str(exc.detail)
        conn.execute(
            "UPDATE scheduled_transfers SET status = ?, finished_at = ?, result_json = ?, "
            "failure_reason = ?, transaction_id = ? WHERE id = ?",
            (status, timestamp, json.dumps(result, ensure_ascii=False) if result else None,
             reason, result["transaction_id"] if result else None, row["id"]),
        )
        audit(conn, row["session_id"], f"schedule_{status}",
              {"schedule_id": row["id"], "business_time": timestamp, "reason": reason,
               "transaction_id": result["transaction_id"] if result else None})
    return len(due)


def run_due_transfers() -> int:
    # The background scanner runs frequently. Avoid reserving SQLite's single
    # writer slot on idle polls, which can make unrelated request transactions
    # fail while upgrading from a read to a write transaction.
    from .jobs import pending_events, run_due

    with db_session() as conn:
        now = business_now(conn)
        has_due_event = any(datetime.fromisoformat(event["at"]) <= now
                            for event in pending_events(conn))
    if not has_due_event:
        return 0

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        count = run_due(conn)
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def advance_to_next(session_id: str) -> dict:
    from .jobs import advance_events
    advance_events(session_id)
    return list_schedules(session_id)
