"""Shared SQLAlchemy bootstrap, environment config, and the clock.

No domain models and no domain logic live here (architecture RFC section 4).
Every component's ORM rows hang off the single `Base` declared below, which is
what lets a cross-component foreign key be a string reference rather than an
import.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Single metadata registry shared by every component."""


# --- Environment (architecture RFC section 11) -----------------------------
#
# Read through functions rather than captured at import so tests and the app
# factory can set the environment before anything reads it.


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def covers_dir() -> Path:
    return data_dir() / "covers"


def timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ", "UTC"))


def discogs_token() -> str | None:
    return os.environ.get("DISCOGS_TOKEN")


def discogs_username() -> str | None:
    return os.environ.get("DISCOGS_USERNAME")


def database_url() -> str:
    return f"sqlite:///{data_dir() / 'rotation33.db'}"


def now() -> datetime:
    """Naive local time in the configured zone.

    Naive by contract, not by accident: recency arithmetic and the never-played
    sentinel both assume no aware/naive mixing (architecture RFC section 2).
    """
    return datetime.now(timezone()).replace(tzinfo=None)


# --- Engine and sessions ---------------------------------------------------

SessionLocal = sessionmaker(expire_on_commit=False)

engine: Engine | None = None


def _apply_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def build_engine(url: str | None = None) -> Engine:
    """Create an engine with the SQLite pragmas applied per connection.

    WAL for concurrent readers alongside the sync thread's single writer, and
    `busy_timeout` so a collision waits instead of erroring (RFC section 12).
    """
    made = create_engine(url or database_url())
    event.listen(made, "connect", _apply_pragmas)
    return made


def init_engine(url: str | None = None) -> Engine:
    """Point the module engine and `SessionLocal` at a database."""
    global engine
    engine = build_engine(url)
    SessionLocal.configure(bind=engine)
    return engine


def session() -> Session:
    if engine is None:
        init_engine()
    return SessionLocal()
