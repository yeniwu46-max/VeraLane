from __future__ import annotations

import sqlite3

import pytest

from app import db
from app.demo_history import load_explicit_history


def test_explicit_history_import_requires_confirmation(tmp_path, monkeypatch):
    database = tmp_path / "demo.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database)
    db.init_db()

    with pytest.raises(ValueError, match="显式确认"):
        load_explicit_history(database, confirmed=False)


def test_explicit_history_import_is_idempotent_and_does_not_change_balance(tmp_path, monkeypatch):
    database = tmp_path / "demo.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database)
    db.init_db()

    with sqlite3.connect(database) as conn:
        original_balance = conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()[0]
        original_count = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (db.ACCOUNT_ID,)).fetchone()[0]

    assert load_explicit_history(database, confirmed=True) == 32
    assert load_explicit_history(database, confirmed=True) == 0

    with sqlite3.connect(database) as conn:
        balance = conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (db.ACCOUNT_ID,)).fetchone()[0]
        count = conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (db.ACCOUNT_ID,)).fetchone()[0]
        history = conn.execute("SELECT COUNT(*) FROM transactions WHERE id LIKE 'fixture-%'").fetchone()[0]

    assert balance == original_balance
    assert count == original_count + 32
    assert history == 32


def test_explicit_history_import_rejects_uninitialized_database(tmp_path):
    database = tmp_path / "empty.sqlite3"
    database.touch()

    with pytest.raises(ValueError, match="演示账户"):
        load_explicit_history(database, confirmed=True)
