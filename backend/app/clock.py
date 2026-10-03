"""Persistent simulated business time; consent TTLs use real UTC."""

from datetime import datetime, timedelta, timezone
import sqlite3

SHANGHAI = timezone(timedelta(hours=8))


def business_now(conn: sqlite3.Connection) -> datetime:
    row = conn.execute("SELECT now FROM demo_clock WHERE id = 1").fetchone()
    return datetime.fromisoformat(row["now"]).astimezone(SHANGHAI)


def business_date(conn: sqlite3.Connection) -> str:
    return business_now(conn).date().isoformat()


def clock_info(conn: sqlite3.Connection) -> dict:
    return {"now": business_now(conn).isoformat(timespec="seconds"),
            "timezone": "Asia/Shanghai", "mode": "simulated", "execution_window_minutes": 10}
