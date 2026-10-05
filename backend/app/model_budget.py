"""Local, conservative model-call accounting; never a provider billing balance.

Reservations and attempts survive restarts. Unknown usage retains its reservation
instead of inventing a zero-token response. No prompts, responses or secrets are
stored here, and the pre-existing model_usage report is not written by this module.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any

from . import db


NOTICE = "仅统计本地账本启用后的调用；费用为配置单价下的估算，不等于 DeepSeek 实际账单或账户余额，原有 20 元预算已使用多少未知。"


@dataclass(frozen=True)
class ModelConfig:
    model: str
    max_output_tokens: int
    timeout_seconds: float
    max_calls: int
    max_total_tokens: int
    max_inflight: int
    budget_yuan: Decimal
    input_price: Decimal | None
    output_price: Decimal | None


@dataclass(frozen=True)
class Reservation:
    attempt_id: str | None
    reason: str | None


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
        if not minimum <= value <= maximum:
            raise ValueError
        return value
    except ValueError as exc:
        raise ValueError("invalid_model_config") from exc


def _decimal(name: str, default: str | None = None) -> Decimal | None:
    raw = os.environ.get(name, default)
    if raw is None or raw.strip() == "":
        return None
    try:
        value = Decimal(raw)
        if not value.is_finite() or value < 0 or value > 1_000_000:
            raise InvalidOperation
        return value
    except InvalidOperation as exc:
        raise ValueError("invalid_model_config") from exc


def load_config() -> ModelConfig:
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash").strip()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}", model):
        raise ValueError("invalid_model_config")
    timeout = _decimal("DEEPSEEK_TIMEOUT_SECONDS", "20")
    budget = _decimal("DEEPSEEK_BUDGET_YUAN", "20")
    incoming = _decimal("DEEPSEEK_INPUT_PRICE_PER_MILLION_YUAN")
    outgoing = _decimal("DEEPSEEK_OUTPUT_PRICE_PER_MILLION_YUAN")
    if timeout is None or not 1 <= timeout <= 120 or budget is None:
        raise ValueError("invalid_model_config")
    if (incoming is None) != (outgoing is None):
        raise ValueError("invalid_model_config")
    if incoming is not None and (incoming <= 0 or outgoing is None or outgoing <= 0):
        raise ValueError("invalid_model_config")
    return ModelConfig(
        model=model,
        max_output_tokens=_integer("DEEPSEEK_MAX_OUTPUT_TOKENS", 768, 64, 4096),
        timeout_seconds=float(timeout),
        max_calls=_integer("DEEPSEEK_MAX_CALLS", 200, 0, 1_000_000),
        max_total_tokens=_integer("DEEPSEEK_MAX_TOTAL_TOKENS", 200_000, 0, 1_000_000_000),
        max_inflight=_integer("DEEPSEEK_MAX_INFLIGHT", 2, 1, 32),
        budget_yuan=budget,
        input_price=incoming,
        output_price=outgoing,
    )


def _schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS model_attempts (
        id TEXT PRIMARY KEY, started_at TEXT NOT NULL, lease_until REAL NOT NULL,
        finished_at TEXT, model TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('reserved','succeeded','failed','unknown','blocked')),
        reason TEXT, reserved_prompt_tokens INTEGER NOT NULL,
        reserved_completion_tokens INTEGER NOT NULL,
        prompt_tokens INTEGER, completion_tokens INTEGER,
        accounted_tokens INTEGER NOT NULL,
        input_price TEXT, output_price TEXT, estimated_cost_microyuan INTEGER
    )""")


def _expire(conn: sqlite3.Connection) -> None:
    # A dead process must not hold a concurrency slot forever. Its unknown token
    # and monetary reservation remain charged to this local budget.
    conn.execute(
        "UPDATE model_attempts SET status='unknown', reason='interrupted_or_timeout', finished_at=? "
        "WHERE status='reserved' AND lease_until < ?", (db.utc_now(), time.time()),
    )


def _summary(conn: sqlite3.Connection, now: float | None = None) -> dict[str, int]:
    now = time.time() if now is None else now
    row = conn.execute("""WITH attempts AS (
        SELECT *, CASE WHEN status='reserved' AND lease_until < ? THEN 'unknown' ELSE status END AS effective_status
        FROM model_attempts
    ) SELECT
        COUNT(*) AS attempts,
        COALESCE(SUM(effective_status='reserved'),0) AS active_requests,
        COALESCE(SUM(effective_status IN ('failed','unknown')),0) AS failed_attempts,
        COALESCE(SUM(prompt_tokens IS NULL),0) AS uncertain_attempts,
        COALESCE(SUM(prompt_tokens + completion_tokens),0) AS reported_tokens,
        COALESCE(SUM(accounted_tokens),0) AS accounted_tokens,
        COALESCE(SUM(estimated_cost_microyuan),0) AS estimated_cost_microyuan,
        COALESCE(SUM(estimated_cost_microyuan IS NULL),0) AS unpriced_attempts
        FROM attempts WHERE effective_status != 'blocked'""", (now,)).fetchone()
    return dict(row)


def _cost(prompt_tokens: int, completion_tokens: int, incoming: Decimal | None, outgoing: Decimal | None) -> int | None:
    if incoming is None or outgoing is None:
        return None
    # Yuan / million tokens × tokens = micro-yuan, rounded upward.
    return int((incoming * prompt_tokens + outgoing * completion_tokens).to_integral_value(rounding=ROUND_CEILING))


def prompt_reservation(messages: list[dict[str, str]]) -> int:
    # UTF-8 byte count plus protocol allowance deliberately overestimates ordinary
    # text tokenization. This is a local guard, not a provider tokenizer guarantee.
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 256


def reserve_call(config: ModelConfig, messages: list[dict[str, str]]) -> Reservation:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _schema(conn)
        _expire(conn)
        used = _summary(conn)
        prompt = prompt_reservation(messages)
        tokens = prompt + config.max_output_tokens
        cost = _cost(prompt, config.max_output_tokens, config.input_price, config.output_price)
        reason = None
        if used["attempts"] >= config.max_calls:
            reason = "call_limit"
        elif config.budget_yuan == 0:
            reason = "budget_limit"
        elif used["accounted_tokens"] + tokens > config.max_total_tokens:
            reason = "token_limit"
        elif used["active_requests"] >= config.max_inflight:
            reason = "concurrency_limit"
        elif cost is not None and used["unpriced_attempts"]:
            reason = "pricing_history_unknown"
        elif cost is not None and Decimal(used["estimated_cost_microyuan"] + cost) > config.budget_yuan * 1_000_000:
            reason = "budget_limit"
        attempt_id = uuid.uuid4().hex
        conn.execute("""INSERT INTO model_attempts
            (id, started_at, lease_until, finished_at, model, status, reason,
             reserved_prompt_tokens, reserved_completion_tokens, accounted_tokens,
             input_price, output_price, estimated_cost_microyuan)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            attempt_id, db.utc_now(), time.time() + config.timeout_seconds + 5,
            db.utc_now() if reason else None, config.model, "blocked" if reason else "reserved", reason,
            0 if reason else prompt, 0 if reason else config.max_output_tokens, 0 if reason else tokens,
            str(config.input_price) if config.input_price is not None else None,
            str(config.output_price) if config.output_price is not None else None,
            0 if reason else cost,
        ))
        conn.commit()
        return Reservation(None if reason else attempt_id, reason)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def validated_usage(payload: Any) -> dict[str, int] | None:
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    values = {name: usage.get(name) for name in ("prompt_tokens", "completion_tokens")}
    if any(type(value) is not int or value < 0 or value > 1_000_000_000 for value in values.values()):
        return None
    return values


def finish_call(attempt_id: str, *, succeeded: bool, usage: dict[str, int] | None, reason: str | None = None) -> None:
    conn = db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM model_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None or row["status"] not in ("reserved", "unknown"):
            conn.rollback()
            return
        prompt = usage["prompt_tokens"] if usage else None
        completion = usage["completion_tokens"] if usage else None
        cost = row["estimated_cost_microyuan"]
        tokens = row["accounted_tokens"]
        if usage:
            tokens = prompt + completion
            cost = _cost(prompt, completion,
                         Decimal(row["input_price"]) if row["input_price"] is not None else None,
                         Decimal(row["output_price"]) if row["output_price"] is not None else None)
        conn.execute("""UPDATE model_attempts SET status=?,reason=?,finished_at=?,
            prompt_tokens=?,completion_tokens=?,accounted_tokens=?,estimated_cost_microyuan=? WHERE id=?""", (
            "succeeded" if succeeded else "failed", reason, db.utc_now(), prompt, completion, tokens, cost, attempt_id,
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_model_status() -> dict[str, Any]:
    configured = bool(os.environ.get("DEEPSEEK_API_KEY", "").strip())
    try:
        config = load_config()
    except ValueError:
        return {"mode": "offline", "reason": "invalid_model_config", "configured": configured, "notice": NOTICE}
    try:
        if db.DB_PATH.is_file():
            conn = sqlite3.connect(
                f"{db.DB_PATH.resolve().as_uri()}?mode=ro",
                uri=True, timeout=5, isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                has_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_attempts'"
                ).fetchone()
                now = time.time()
                if has_table:
                    used = _summary(conn, now)
                    last = conn.execute("SELECT status,reason,lease_until FROM model_attempts ORDER BY rowid DESC LIMIT 1").fetchone()
                    if last and last["status"] == "reserved" and last["lease_until"] < now:
                        last_status, last_reason = "unknown", "interrupted_or_timeout"
                    else:
                        last_status, last_reason = (last["status"], last["reason"]) if last else (None, None)
                else:
                    used = {"attempts": 0, "active_requests": 0, "failed_attempts": 0,
                            "uncertain_attempts": 0, "reported_tokens": 0, "accounted_tokens": 0,
                            "estimated_cost_microyuan": 0, "unpriced_attempts": 0}
                    last_status = last_reason = None
                conn.commit()
            finally:
                conn.close()
        else:
            used = {"attempts": 0, "active_requests": 0, "failed_attempts": 0,
                    "uncertain_attempts": 0, "reported_tokens": 0, "accounted_tokens": 0,
                    "estimated_cost_microyuan": 0, "unpriced_attempts": 0}
            last_status = last_reason = None
    except (sqlite3.Error, OSError):
        return {"mode": "offline", "reason": "accounting_unavailable", "configured": configured, "notice": NOTICE}
    pricing = config.input_price is not None
    reason = "missing_api_key" if not configured else None
    model_mode = os.environ.get("VERALANE_MODEL_MODE", "offline").strip().lower()
    if model_mode != "auto":
        reason = "manual_offline" if model_mode == "offline" else "invalid_model_mode"
    if reason is None:
        if used["attempts"] >= config.max_calls:
            reason = "call_limit"
        elif config.budget_yuan == 0:
            reason = "budget_limit"
        elif used["accounted_tokens"] >= config.max_total_tokens:
            reason = "token_limit"
        elif used["active_requests"] >= config.max_inflight:
            reason = "concurrency_limit"
        elif pricing and used["unpriced_attempts"]:
            reason = "pricing_history_unknown"
        elif pricing and Decimal(used["estimated_cost_microyuan"]) >= config.budget_yuan * 1_000_000:
            reason = "budget_limit"
    cost = used.pop("estimated_cost_microyuan")
    return {
        "mode": "offline" if reason else "deepseek", "reason": reason,
        "configured": configured, "model": config.model, "pricing_configured": pricing,
        "last_attempt_status": last_status,
        "last_fallback_reason": last_reason,
        "usage": {**used, "estimated_cost_yuan": f"{Decimal(cost) / 1_000_000:.6f}" if pricing and not used["unpriced_attempts"] else None},
        "limits": {"max_calls": config.max_calls, "max_total_tokens": config.max_total_tokens,
                   "max_inflight": config.max_inflight, "max_output_tokens": config.max_output_tokens,
                   "timeout_seconds": config.timeout_seconds, "budget_yuan": str(config.budget_yuan)},
        "notice": NOTICE,
    }
