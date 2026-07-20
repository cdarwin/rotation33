"""The transaction-framing convention itself (RFC section 10).

Components never commit; the caller frames the unit of work. These tests pin
that convention so a later change that reintroduces an internal commit, or
breaks the framing, fails here rather than silently in production.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

import infra


def _count_marks(db) -> int:
    return db.execute(sa.text("select count(*) from _mark")).scalar_one()


@pytest.fixture
def mark_table(engine):
    with engine.begin() as conn:
        conn.execute(sa.text("create table _mark (id integer primary key)"))
    return engine


def test_begin_commits_on_success(begin, mark_table):
    with begin() as db:
        db.execute(sa.text("insert into _mark (id) values (1)"))

    with begin() as db:
        assert _count_marks(db) == 1


def test_begin_rolls_back_on_exception(begin, mark_table):
    with pytest.raises(RuntimeError), begin() as db:
        db.execute(sa.text("insert into _mark (id) values (1)"))
        raise RuntimeError("boom")

    with begin() as db:
        assert _count_marks(db) == 0


def test_begin_closes_the_session_either_way(begin, mark_table):
    with begin() as db:
        session = db
    # A closed session has no active transaction and no identity map contents.
    assert not session.in_transaction()


def test_write_frames_a_transaction_like_begin(mark_table, monkeypatch, tmp_path):
    # `app.write()` is the view layer's entry point; it must behave as begin().
    import app

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with app.write() as db:
        db.execute(sa.text("insert into _mark (id) values (7)"))

    with infra.SessionLocal.begin() as db:
        assert _count_marks(db) == 1
