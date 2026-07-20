"""Flask app factory, request session lifecycle, and routes.

Phase 0 stands up the factory and the session lifecycle only; the screens
arrive in Phase 6. Keeping the shape here from the start means later phases add
routes rather than discover the wiring.

Transactions follow the SQLAlchemy begin/commit/rollback framing convention
(RFC section 10). Components never commit; the caller frames the unit of work.
A view that only reads uses the request-scoped `db()` session, which teardown
closes. A view that writes frames its work explicitly:

    with write() as db:
        sessions.log_play(db, ...)
    # committed on success, rolled back on exception, closed either way

Commit and rollback are structural, never hand-written on a success/except path,
which is what keeps a half-finished request from committing its finished half.
"""

from __future__ import annotations

from contextlib import AbstractContextManager

from flask import Flask, g, render_template
from sqlalchemy.orm import Session

import infra


def create_app() -> Flask:
    infra.init_engine()

    app = Flask(__name__)

    @app.teardown_appcontext
    def close_session(exception: BaseException | None) -> None:
        """Close the request's read session.

        Nothing is committed here. Reads leave nothing to commit, and writes are
        framed by `write()` and already committed or rolled back by the time the
        request ends. A dirty read session at teardown means a view mutated the
        read session instead of framing a write, so it is rolled back on close.
        """
        session = g.pop("db", None)
        if session is None:
            return
        if session.in_transaction():
            session.rollback()
        session.close()

    @app.route("/")
    def home() -> str:
        return render_template("index.html")

    return app


def db() -> Session:
    """The request-scoped read session, opened on first use, closed at teardown."""
    if "db" not in g:
        g.db = infra.session()
    return g.db


def write() -> AbstractContextManager[Session]:
    """A framed write transaction: commit on success, rollback on exception, close.

    This is the SQLAlchemy `sessionmaker.begin()` pattern. A writing view opens
    one of these around its unit of work rather than mutating the read session,
    so commit and rollback are structural and a write is never entangled with
    the request's reads.
    """
    return infra.SessionLocal.begin()
