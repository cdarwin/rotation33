"""Proves the Phase 0 harness itself works, since every later phase leans on it."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa

import infra


def test_db_fixture_gives_a_working_session(db):
    assert db.execute(sa.text("select 1")).scalar_one() == 1


def test_sqlite_pragmas_are_applied_per_connection(db):
    assert db.execute(sa.text("pragma journal_mode")).scalar_one().lower() == "wal"
    assert db.execute(sa.text("pragma busy_timeout")).scalar_one() == 5000
    assert db.execute(sa.text("pragma foreign_keys")).scalar_one() == 1


def test_each_test_gets_its_own_database(db, data_dir):
    assert str(db.get_bind().url).endswith(str(data_dir / "test.db"))


def test_now_is_naive(monkeypatch):
    """Naive by contract: aware datetimes would break the recency arithmetic."""
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    assert infra.now().tzinfo is None
    assert isinstance(infra.now(), datetime)


def test_now_respects_the_configured_zone(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    utc = infra.now()
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    tokyo = infra.now()
    assert 8 <= (tokyo - utc).total_seconds() / 3600 <= 10
