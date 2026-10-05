"""Shared fund reservations and explicitly simulated step-up authorization.

Money helpers run inside the caller's transaction (BEGIN IMMEDIATE recommended).
Demo codes are displayed to the caller: they demonstrate a confirmation boundary,
not identity verification, SMS authentication, or production bank authorization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import HTTPException

from . import db
from .db import ACCOUNT_ID


DEMO_NOTICE = "模拟强验证：验证码直接展示，仅演示操作绑定与有效期，不构成真实身份验证。"
HIGH_RISK_THRESHOLD_CENTS = 100_000


def requires_red_tier(amount_cents: int) -> bool:
    """Use one strict boundary for amount-based high-risk demo actions."""
    return amount_cents > HIGH_RISK_THRESHOLD_CENTS


def init_schema(conn: sqlite3.Connection) -> None:
    # Do not use executescript: it implicitly commits the caller's transaction.
    conn.execute("""CREATE TABLE IF NOT EXISTS fund_reservations (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
        account_id TEXT NOT NULL REFERENCES accounts(id), purpose TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
        remaining_cents INTEGER NOT NULL CHECK(remaining_cents >= 0),
        consumed_cents INTEGER NOT NULL DEFAULT 0 CHECK(consumed_cents >= 0),
        released_cents INTEGER NOT NULL DEFAULT 0 CHECK(released_cents >= 0),
        status TEXT NOT NULL CHECK(status IN ('active','released','consumed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(amount_cents = remaining_cents + consumed_cents + released_cents)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS fund_reservations_account ON fund_reservations(account_id,status)")
    conn.execute("""CREATE TABLE IF NOT EXISTS action_challenges (
        id TEXT PRIMARY KEY, action_id TEXT NOT NULL REFERENCES actions(id),
        session_id TEXT NOT NULL, payload_hash TEXT NOT NULL, code_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','verified','revoked','locked','expired')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, verified_at TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS action_challenges_action ON action_challenges(action_id)")


def _money(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def _amount(value: int) -> None:
    if type(value) is not int or not 0 < value <= 10_000_000:
        raise HTTPException(422, "金额必须为大于零的整数分，且不超过 100000 元")


def _transaction(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        raise RuntimeError("Fund changes require a caller-owned transaction")


def available_cents(conn: sqlite3.Connection, account_id: str = ACCOUNT_ID) -> int:
    row = conn.execute("""SELECT balance_cents - COALESCE((SELECT SUM(remaining_cents)
        FROM fund_reservations WHERE account_id=? AND status='active'),0) AS available
        FROM accounts WHERE id=?""", (account_id, account_id)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到模拟账户")
    return row["available"]


def debit(conn: sqlite3.Connection, amount_cents: int, exclude_reservation_id: str | None = None) -> None:
    _transaction(conn)
    _amount(amount_cents)
    changed = conn.execute("""UPDATE accounts SET balance_cents=balance_cents-?
        WHERE id=? AND balance_cents>=? AND balance_cents-COALESCE((
            SELECT SUM(remaining_cents) FROM fund_reservations
            WHERE account_id=? AND status='active' AND (? IS NULL OR id!=?)
        ),0)>=?""", (amount_cents, ACCOUNT_ID, amount_cents, ACCOUNT_ID,
                      exclude_reservation_id, exclude_reservation_id, amount_cents)).rowcount
    if changed != 1:
        raise HTTPException(409, "可用余额不足；已预留资金不能用于其他支出")


def credit(conn: sqlite3.Connection, amount_cents: int) -> None:
    """Credit the fixed demo account; caller owns transaction and idempotency."""
    _transaction(conn)
    _amount(amount_cents)
    changed = conn.execute("UPDATE accounts SET balance_cents=balance_cents+? WHERE id=?",
                           (amount_cents, ACCOUNT_ID)).rowcount
    if changed != 1:
        raise HTTPException(409, "未找到可入账的模拟账户")


def _owned(conn: sqlite3.Connection, reservation_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM fund_reservations WHERE id=? AND session_id=? AND account_id=?",
                       (reservation_id, session_id, ACCOUNT_ID)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该资金预留")
    return row


def _public(row: sqlite3.Row) -> dict[str, Any]:
    fields = ("id", "purpose", "amount_cents", "remaining_cents", "consumed_cents", "released_cents",
              "status", "created_at", "updated_at")
    result = {key: row[key] for key in fields}
    for key in ("amount", "remaining", "consumed", "released"):
        result[f"{key}_yuan"] = _money(row[f"{key}_cents"])
    return result


def reserve(conn: sqlite3.Connection, id: str, session_id: str, amount: int, purpose: str) -> dict[str, Any]:
    _transaction(conn)
    _amount(amount)
    if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 100:
        raise HTTPException(422, "请填写 1 至 100 字的资金用途")
    existing = conn.execute("SELECT * FROM fund_reservations WHERE id=?", (id,)).fetchone()
    if existing is not None:
        row = _owned(conn, id, session_id)
        if row["amount_cents"] != amount or row["purpose"] != purpose.strip():
            raise HTTPException(409, "该预留编号已绑定其他金额或用途")
        return _public(row)
    if amount > available_cents(conn):
        raise HTTPException(409, "可用余额不足，未创建资金预留")
    now = db.utc_now()
    conn.execute("""INSERT INTO fund_reservations
        (id,session_id,account_id,purpose,amount_cents,remaining_cents,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,'active',?,?)""", (id, session_id, ACCOUNT_ID, purpose.strip(), amount, amount, now, now))
    db.audit(conn, session_id, "funds_reserved", {"reservation_id": id, "amount_cents": amount})
    return _public(_owned(conn, id, session_id))


def release(conn: sqlite3.Connection, id: str, session_id: str) -> dict[str, Any]:
    _transaction(conn)
    row = _owned(conn, id, session_id)
    if row["status"] == "active":
        conn.execute("""UPDATE fund_reservations SET status='released',released_cents=released_cents+remaining_cents,
            remaining_cents=0,updated_at=? WHERE id=?""", (db.utc_now(), id))
        db.audit(conn, session_id, "funds_released", {"reservation_id": id, "amount_cents": row["remaining_cents"]})
    return _public(_owned(conn, id, session_id))


def consume(conn: sqlite3.Connection, id: str, session_id: str, amount: int) -> dict[str, Any]:
    _transaction(conn)
    _amount(amount)
    row = _owned(conn, id, session_id)
    if row["status"] != "active" or amount > row["remaining_cents"]:
        raise HTTPException(409, "预留资金已释放、已用完或不足，未扣款")
    debit(conn, amount, exclude_reservation_id=id)
    remaining = row["remaining_cents"] - amount
    conn.execute("""UPDATE fund_reservations SET remaining_cents=?,consumed_cents=consumed_cents+?,
        status=?,updated_at=? WHERE id=?""", (remaining, amount, "active" if remaining else "consumed", db.utc_now(), id))
    db.audit(conn, session_id, "funds_consumed", {"reservation_id": id, "amount_cents": amount})
    return _public(_owned(conn, id, session_id))


def funds_snapshot(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    account = conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (ACCOUNT_ID,)).fetchone()
    if account is None:
        raise HTTPException(404, "未找到模拟账户")
    available = available_cents(conn)
    reservations = conn.execute("SELECT * FROM fund_reservations WHERE session_id=? AND account_id=? ORDER BY rowid DESC",
                                (session_id, ACCOUNT_ID)).fetchall()
    return {"balance_yuan": _money(account["balance_cents"]), "available_yuan": _money(available),
            "reserved_yuan": _money(account["balance_cents"] - available),
            "reservations": [_public(row) for row in reservations]}


def prepare_reserve(session_id: str, amount_yuan: str, purpose: str) -> dict[str, Any]:
    from .service import cents_from_yuan, create_action, reply
    amount = cents_from_yuan(amount_yuan)
    if amount is None:
        raise HTTPException(422, "请输入大于零且精确到分的金额")
    if not purpose.strip() or len(purpose) > 100:
        raise HTTPException(422, "请填写 1 至 100 字的资金用途")
    with db.db_session() as conn:
        if amount > available_cents(conn):
            raise HTTPException(409, "可用余额不足，未创建预留计划")
        action = create_action(conn, session_id, "fund_reserve", "yellow",
                               {"amount_cents": amount, "amount_yuan": _money(amount), "purpose": purpose.strip()})
        return reply("请确认预留金额与用途。确认只减少可用余额，不扣减账面余额。", session_id, "offline", pending_action=action)


def prepare_release(session_id: str, reservation_id: str) -> dict[str, Any]:
    from .service import create_action, reply
    with db.db_session() as conn:
        row = _owned(conn, reservation_id, session_id)
        if row["status"] != "active":
            raise HTTPException(409, "该预留已释放或用完")
        action = create_action(conn, session_id, "fund_release", "yellow",
                               {"reservation_id": reservation_id, "remaining_cents": row["remaining_cents"],
                                "remaining_yuan": _money(row["remaining_cents"]), "purpose": row["purpose"]})
        return reply("请确认释放剩余预留资金。已发生的支出不会退款。", session_id, "offline", pending_action=action)


def execute_reserve(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    reservation = reserve(conn, f"reserve-{action_id}", session_id, payload["amount_cents"], payload["purpose"])
    return {"status": "completed", "action_id": action_id, "reservation_id": reservation["id"],
            "reservation": reservation, "message": f"已预留 ¥{reservation['amount_yuan']}；账面余额未变。"}


def execute_release(conn: sqlite3.Connection, action_id: str, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = _owned(conn, payload["reservation_id"], session_id)
    if row["status"] != "active" or row["remaining_cents"] != payload["remaining_cents"] or row["purpose"] != payload["purpose"]:
        raise HTTPException(409, "预留资金已变化，请重新核对剩余金额")
    reservation = release(conn, row["id"], session_id)
    return {"status": "completed", "action_id": action_id, "reservation_id": row["id"],
            "reservation": reservation, "message": "剩余预留已释放；账面余额未增加，已发生支出未退款。"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _demo_enabled() -> None:
    if os.environ.get("VERALANE_DEMO_CONTROLS", "1") != "1":
        raise HTTPException(403, "模拟强验证已关闭")


def _action(conn: sqlite3.Connection, action_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM actions WHERE id=? AND session_id=?", (action_id, session_id)).fetchone()
    if row is None:
        raise HTTPException(404, "未找到该待确认操作")
    if row["status"] != "pending":
        raise HTTPException(409, "操作已完成或失效，请重新发起")
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        raise HTTPException(409, "操作确认已过期，请重新发起")
    if row["tier"] != "red":
        raise HTTPException(409, "该操作无需模拟强验证")
    return row


def _payload_hash(row: sqlite3.Row) -> str:
    value = {key: row[key] for key in ("id", "session_id", "type", "tier")}
    value["payload"] = json.loads(row["payload_json"])
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _code_hash(challenge_id: str, code: str) -> str:
    return hashlib.sha256(f"{challenge_id}:{code}".encode()).hexdigest()


def issue_challenge(action_id: str, session_id: str) -> dict[str, Any]:
    _demo_enabled()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _action(conn, action_id, session_id)
        conn.execute("UPDATE action_challenges SET status='revoked' WHERE action_id=? AND status IN ('active','verified')", (action_id,))
        challenge_id = secrets.token_urlsafe(24)
        code = f"{secrets.randbelow(1_000_000):06d}"
        expires = min(_now() + timedelta(minutes=3), datetime.fromisoformat(row["expires_at"]))
        conn.execute("""INSERT INTO action_challenges
            (id,action_id,session_id,payload_hash,code_hash,status,created_at,expires_at)
            VALUES (?,?,?,?,?,'active',?,?)""", (challenge_id, action_id, session_id, _payload_hash(row),
                                               _code_hash(challenge_id, code), db.utc_now(), expires.isoformat(timespec="seconds")))
        db.audit(conn, session_id, "demo_challenge_created", {"action_id": action_id, "challenge_id": challenge_id})
        conn.commit()
        return {"action_id": action_id, "challenge_id": challenge_id, "demo_code": code,
                "expires_at": expires.isoformat(timespec="seconds"), "attempts_remaining": 3,
                "mode": "simulated", "notice": DEMO_NOTICE}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_challenge(action_id: str, session_id: str, challenge_id: str, code: str) -> dict[str, Any]:
    _demo_enabled()
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        action = _action(conn, action_id, session_id)
        row = conn.execute("SELECT * FROM action_challenges WHERE id=? AND action_id=? AND session_id=?",
                           (challenge_id, action_id, session_id)).fetchone()
        if row is None:
            raise HTTPException(404, "未找到该操作的验证请求")
        if row["status"] not in ("active", "verified"):
            raise HTTPException(409, "验证请求已作废或尝试次数用完，请重新生成")
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            conn.execute("UPDATE action_challenges SET status='expired' WHERE id=?", (challenge_id,))
            conn.commit()
            raise HTTPException(409, "模拟验证码已过期，请重新生成")
        if not hmac.compare_digest(row["payload_hash"], _payload_hash(action)):
            conn.execute("UPDATE action_challenges SET status='revoked' WHERE id=?", (challenge_id,))
            conn.commit()
            raise HTTPException(409, "操作内容已变化，请重新核对并验证")
        valid_code = isinstance(code, str) and re.fullmatch(r"\d{6}", code) and hmac.compare_digest(row["code_hash"], _code_hash(challenge_id, code))
        if not valid_code:
            if row["status"] == "verified":
                raise HTTPException(409, "该验证已完成，不能修改其授权状态")
            attempts = row["attempts"] + 1
            conn.execute("UPDATE action_challenges SET attempts=?,status=? WHERE id=?",
                         (attempts, "locked" if attempts >= 3 else "active", challenge_id))
            db.audit(conn, session_id, "demo_challenge_failed", {"action_id": action_id, "attempts_remaining": 3 - attempts})
            conn.commit()  # Persist failed guesses even though the HTTP request fails.
            raise HTTPException(403, f"模拟验证码错误，剩余 {3 - attempts} 次机会")
        if row["status"] != "verified":
            conn.execute("UPDATE action_challenges SET status='verified',verified_at=? WHERE id=?", (db.utc_now(), challenge_id))
            db.audit(conn, session_id, "demo_challenge_verified", {"action_id": action_id, "challenge_id": challenge_id})
        conn.commit()
        return {"action_id": action_id, "challenge_id": challenge_id, "verified": True,
                "expires_at": row["expires_at"], "mode": "simulated", "notice": DEMO_NOTICE}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def require_verified(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    if row["tier"] != "red":
        return
    _demo_enabled()
    current = _action(conn, row["id"], row["session_id"])
    challenge = conn.execute("SELECT * FROM action_challenges WHERE action_id=? AND session_id=? ORDER BY rowid DESC LIMIT 1",
                             (row["id"], row["session_id"])).fetchone()
    if (challenge is None or challenge["status"] != "verified"
            or datetime.fromisoformat(challenge["expires_at"]) <= _now()
            or not hmac.compare_digest(challenge["payload_hash"], _payload_hash(current))):
        raise HTTPException(403, "请先完成与当前操作绑定的模拟强验证")
