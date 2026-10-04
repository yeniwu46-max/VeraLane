"""Opt-in fictional history. Never loaded during application startup.

Call ``load_history_fixture(conn)`` from a test or an explicitly requested demo
setup script. These historical samples do not recalculate the demo balance.
"""

import sqlite3

from .db import ACCOUNT_ID


def load_history_fixture(conn: sqlite3.Connection) -> int:
    rows = []
    for index, month in enumerate(("2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07")):
        for suffix, day, amount, merchant, category in (
            ("cloud", "25", 12800 if index < 7 else 16800, "云影会员", "数字服务"),
            ("music", "16", 9800 if index < 6 else 10700, "青柠音乐", "数字服务"),
            ("coffee", "11", 4500 + index * 100, "城市咖啡", "餐饮"),
            ("grocery", "22", 24000 + index * 500, "悦选超市", "日用"),
        ):
            rows.append((f"fixture-{month}-{suffix}", ACCOUNT_ID, f"{month}-{day}", "out", amount, merchant, category, "可重复生成的虚构历史样本"))
    before = conn.total_changes
    conn.executemany("INSERT OR IGNORE INTO transactions (id, account_id, posted_on, direction, amount_cents, counterparty, category, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return conn.total_changes - before
