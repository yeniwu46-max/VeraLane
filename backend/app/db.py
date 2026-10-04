"""SQLite state for the local, fictional banking demo."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("VERALANE_DB_PATH", ROOT / "data" / "veralane.sqlite3"))
DEMO_DATE = os.environ.get("VERALANE_DEMO_DATE", "2026-09-30")
USER_ID = "demo-user"
ACCOUNT_ID = "account-main"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db_session():
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                label TEXT NOT NULL,
                balance_cents INTEGER NOT NULL CHECK (balance_cents >= 0)
            );
            CREATE TABLE IF NOT EXISTS contacts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                account_ref TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                posted_on TEXT NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                counterparty TEXT NOT NULL,
                category TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                action_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                merchant TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                renewal_on TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('active', 'cancelled'))
            );
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                tier TEXT NOT NULL CHECK (tier IN ('yellow', 'red')),
                status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'expired', 'failed')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                result_json TEXT
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                context_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                session_id TEXT NOT NULL,
                event TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS demo_clock (
                id INTEGER PRIMARY KEY CHECK (id = 1), now TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduled_transfers (
                id TEXT PRIMARY KEY REFERENCES actions(id),
                session_id TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                payload_json TEXT NOT NULL,
                execute_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed', 'cancelled', 'expired')),
                created_at TEXT NOT NULL,
                finished_at TEXT,
                result_json TEXT,
                failure_reason TEXT,
                transaction_id TEXT UNIQUE
            );
            CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_transfers(status, execute_at);
            CREATE TABLE IF NOT EXISTS aa_collections (
                id TEXT PRIMARY KEY REFERENCES actions(id),
                session_id TEXT NOT NULL,
                account_id TEXT NOT NULL REFERENCES accounts(id),
                source_transaction_id TEXT REFERENCES transactions(id),
                source_locked INTEGER NOT NULL DEFAULT 1 CHECK (source_locked IN (0, 1)),
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'partial', 'completed', 'closed')),
                created_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS aa_one_source ON aa_collections(account_id, source_transaction_id)
                WHERE source_transaction_id IS NOT NULL AND source_locked = 1;
            CREATE TABLE IF NOT EXISTS aa_requests (
                id TEXT PRIMARY KEY,
                collection_id TEXT NOT NULL REFERENCES aa_collections(id),
                contact_id TEXT NOT NULL REFERENCES contacts(id),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'closed')),
                paid_at TEXT,
                transaction_id TEXT UNIQUE REFERENCES transactions(id),
                UNIQUE(collection_id, contact_id)
            );
            CREATE TABLE IF NOT EXISTS aa_payment_events (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE REFERENCES aa_requests(id),
                transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(id),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aa_partial_payments (
                idempotency_key TEXT PRIMARY KEY,
                request_id TEXT NOT NULL REFERENCES aa_requests(id),
                transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(id),
                amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
                created_at TEXT NOT NULL
            );
            """
        )
        # executescript commits any open transaction; seed rows atomically.
        conn.execute("BEGIN")
        from .subscription_intelligence import init_schema as init_subscriptions
        init_subscriptions(conn)
        from .plans import init_schema as init_plans
        init_plans(conn)
        from .execution_controls import init_schema as init_controls
        init_controls(conn)
        from .investments import init_schema as init_investments
        init_investments(conn)
        from .cards import init_schema as init_cards
        init_cards(conn)
        from .life_tasks import init_schema as init_life_tasks
        init_life_tasks(conn)
        from .aliases import init_schema as init_aliases
        init_aliases(conn)
        from .recurring import init_schema as init_recurring
        init_recurring(conn)
        from .bill_preferences import init_schema as init_bill_preferences
        init_bill_preferences(conn)
        conn.execute("INSERT OR IGNORE INTO demo_clock (id, now) VALUES (1, ?)",
                     (f"{DEMO_DATE}T09:00:00+08:00",))
        if conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
            return
        seed_demo(conn)


def seed_demo(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO accounts VALUES (?, ?, ?, ?)",
        (ACCOUNT_ID, USER_ID, "日常账户", 888830),
    )
    conn.executemany(
        "INSERT INTO contacts VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("contact-linyue", USER_ID, "林悦", "13800001234", "DEMO-1001", 1),
            ("contact-wang1", USER_ID, "王明", "13900001111", "DEMO-1002", 1),
            ("contact-wang2", USER_ID, "王明", "13900002222", "DEMO-1003", 1),
            ("contact-chen", USER_ID, "陈晨", "13700003333", "DEMO-1004", 1),
        ],
    )
    transactions = [
        ("tx-1", "2026-09-28", "out", 4590, "城市咖啡", "餐饮", "早餐"),
        ("tx-2", "2026-09-25", "out", 16800, "云影会员", "数字服务", "月度自动续费"),
        ("tx-3", "2026-09-22", "out", 32640, "悦选超市", "日用", "购物"),
        ("tx-4", "2026-09-18", "out", 89900, "安居物业", "居住", "物业费"),
        ("tx-5", "2026-09-16", "out", 12800, "青柠音乐", "数字服务", "月度自动续费"),
        ("tx-6", "2026-09-11", "out", 5800, "城市咖啡", "餐饮", "午餐"),
        ("tx-7", "2026-09-07", "in", 980000, "模拟工资", "收入", "工资"),
        ("tx-8", "2026-09-02", "out", 23500, "地铁出行", "交通", "通勤"),
        ("tx-9", "2026-08-27", "out", 16900, "云影会员", "数字服务", "月度自动续费"),
        ("tx-10", "2026-08-20", "out", 24700, "悦选超市", "日用", "购物"),
        ("tx-11", "2026-08-12", "out", 10700, "青柠音乐", "数字服务", "月度自动续费"),
        ("tx-12", "2026-08-05", "out", 13700, "城市咖啡", "餐饮", "聚餐"),
    ]
    conn.executemany(
        """INSERT INTO transactions
        (id, account_id, posted_on, direction, amount_cents, counterparty, category, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [(id, ACCOUNT_ID, day, direction, cents, counterparty, category, note)
         for id, day, direction, cents, counterparty, category, note in transactions],
    )
    conn.executemany(
        "INSERT INTO subscriptions VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("sub-cloud", USER_ID, "云影会员", 16800, "2026-10-25", "active"),
            ("sub-music", USER_ID, "青柠音乐", 12800, "2026-10-16", "active"),
        ],
    )
    audit(conn, "system", "demo_seeded", {"demo_date": DEMO_DATE})


def audit(conn: sqlite3.Connection, session_id: str, event: str, details: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO audit (at, session_id, event, details_json) VALUES (?, ?, ?, ?)",
        (utc_now(), session_id, event, json.dumps(details, ensure_ascii=False, sort_keys=True)),
    )


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
