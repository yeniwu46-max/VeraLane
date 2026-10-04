"""Explicitly append fictional historical transactions to a prepared demo DB."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from .db import ACCOUNT_ID, audit
from .history_fixture import load_history_fixture


def load_explicit_history(database: Path, *, confirmed: bool) -> int:
    if not confirmed:
        raise ValueError("导入虚构历史样本前须显式确认；余额不会变化，但账单会新增历史流水。")
    path = Path(database).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("目标路径不是现有数据库文件；请先启动并初始化本地演示系统。")

    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
            raise ValueError("未找到 VeraLane 演示账户；请确认传入的是已初始化的 VeraLane 数据库。")
        if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (ACCOUNT_ID,)).fetchone():
            raise ValueError("未找到 VeraLane 演示账户；请确认传入的是已初始化的 VeraLane 数据库。")
        added = load_history_fixture(conn)
        if added:
            audit(conn, "demo-setup", "fictional_history_loaded", {"added_transactions": added})
        conn.commit()
        return added
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="向现有 VeraLane 演示数据库追加可重复的虚构历史流水。")
    parser.add_argument("--database", type=Path, required=True, help="已初始化的 SQLite 数据库文件路径")
    parser.add_argument("--confirm-fictional-history", action="store_true", help="确认向账单追加 32 笔历史样本；不改变余额")
    args = parser.parse_args(argv)
    try:
        added = load_explicit_history(args.database, confirmed=args.confirm_fictional_history)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    if added:
        print(f"已追加 {added} 笔虚构历史流水；账户余额未改变。")
    else:
        print("历史样本已经存在，无需重复导入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
