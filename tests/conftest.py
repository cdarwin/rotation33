"""Test harness shared by every phase.

Each test gets its own SQLite file under `tmp_path` and a fresh schema, so
there is no shared state and no ordering dependency between tests. The database
is real; the ORM is never mocked (execution plan section 6).
"""

from __future__ import annotations

import pytest

import infra
import schema


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """An isolated DATA_DIR, so nothing under test writes to the real volume."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "covers").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def engine(data_dir):
    made = infra.init_engine(f"sqlite:///{data_dir / 'test.db'}")
    schema.metadata.create_all(made)
    yield made
    made.dispose()


@pytest.fixture
def db(engine):
    """A session on a fresh, empty database.

    For arrange-act-assert inside a single uncommitted transaction, which is
    most tests: the work is visible to the same session via flush-on-query, and
    the fixture rolls back on close. When a test needs to observe state across a
    real commit boundary, use `begin` instead (or several `begin` blocks).
    """
    session = infra.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def begin(engine):
    """A framed unit of work, the SQLAlchemy begin/commit/rollback pattern.

        with begin() as db:
            records.upsert(db, release)
        # committed here; rolled back instead if the block raised; closed either way

    This is the same convention the view layer uses (RFC section 10), so a test
    exercises components through the exact transaction framing production does.
    Reach for it wherever a commit boundary is part of what is under test.
    """
    return infra.SessionLocal.begin
