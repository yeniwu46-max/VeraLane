from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app import db
from app.main import app


def database_snapshot(path):
    conn = sqlite3.connect(path)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        contents = {}
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            contents[table] = sorted((tuple(row) for row in conn.execute(f"SELECT * FROM {quoted}")), key=repr)
        return conn.execute("PRAGMA schema_version").fetchone()[0], contents
    finally:
        conn.close()


def test_financial_get_routes_do_not_change_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "readonly.sqlite3")
    session = "readonly-audit"
    get_requests = [
        ("/api/health", {}),
        ("/api/overview", {}),
        ("/api/contacts", {}),
        ("/api/bills", {}),
        ("/api/bills/export", {}),
        ("/api/schedules", {"session_id": session}),
        ("/api/demo/events", {"session_id": session}),
        ("/api/aa/settlements", {"session_id": session}),
        ("/api/aa/settlements/missing", {"session_id": session}),
        ("/api/aa/collections", {"session_id": session}),
        ("/api/aa/collections/missing", {"session_id": session}),
        ("/api/subscriptions/diagnostics", {}),
        ("/api/reminders", {"session_id": session}),
        ("/api/aliases", {"session_id": session}),
        ("/api/funds", {"session_id": session}),
        ("/api/insights", {}),
        ("/api/bill-preferences", {"session_id": session}),
        ("/api/cards", {"session_id": session}),
        ("/api/investments/questions", {}),
        ("/api/investments/products", {"session_id": session}),
        ("/api/investments/portfolio", {"session_id": session}),
        ("/api/life/catalog", {}),
        ("/api/life/tasks", {"session_id": session}),
        ("/api/life/tasks/missing", {"session_id": session}),
        ("/api/plans", {"session_id": session}),
        ("/api/plans/missing", {"session_id": session}),
        ("/api/transfers/recurring", {"session_id": session}),
        ("/api/transfers/recurring/missing", {"session_id": session}),
        ("/api/transfers/batch", {"session_id": session}),
        ("/api/transfers/batch/missing", {"session_id": session}),
    ]

    with TestClient(app, base_url="http://127.0.0.1") as client:
        before = database_snapshot(db.DB_PATH)
        responses = [(path, client.get(path, params=params)) for path, params in get_requests]
        after = database_snapshot(db.DB_PATH)

    unexpected = [(path, response.status_code) for path, response in responses if response.status_code >= 500]
    assert not unexpected
    assert after == before
