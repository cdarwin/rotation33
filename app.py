"""Flask app factory, request session lifecycle, and routes.

Thin views: a view may join across components for display but never implements
a rule, which lives in a facade. Reads use the request-scoped `db()` session;
writes frame a unit of work with `with write() as db:`, so commit and rollback
are structural.

Two htmx interactions, the regenerate swap and the sync progress poll.
Everything else is a plain form post with a redirect, which works without
JavaScript and keeps the surface small.
"""

from __future__ import annotations

import json
import os
from contextlib import AbstractContextManager

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import infra
import moods
import recommendations
import records
import sessions
import sync


def create_app() -> Flask:
    infra.init_engine()

    # A process killed mid-sync leaves a sync_run stuck at `running`, which the
    # progress bar would poll forever. The in-process lock cannot cover a
    # restart, so the factory reconciles orphans once at startup.
    with write() as startup:
        sync.reconcile_orphaned_runs(startup)

    app = Flask(__name__)
    # Signs the flash-message cookie. A per-process key is fine: one worker
    # single user, and flashes only need to survive a redirect.
    app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)

    @app.errorhandler(OperationalError)
    def busy(exc: OperationalError):
        """SQLite refused a write because something else held the lock.

        WAL gives concurrent readers but one writer, so a request write can lose
        to the sync thread's commit and exhaust `busy_timeout`.
        The transaction has already rolled back by the time this runs, so nothing
        is half-written; what is left is telling the user rather than showing
        them a traceback. Retrying is safe and usually works, because the sync
        commits once and briefly.
        """
        app.logger.warning("database busy, asking the user to retry: %s", exc)
        flash("The database was busy, most likely a sync finishing up. Try that again.")
        return redirect(request.referrer or url_for("home")), 503

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

    # --- Home -------------------------------------------------------------

    @app.route("/")
    def home() -> str | Response:
        # A live session sends you to its workspace; ?new=1 forces the start form
        # anyway, which is what the workspace's "Start a new session" links to.
        if sessions.current(db()) is not None and not request.args.get("new"):
            return redirect(url_for("session_view"))
        return render_template(
            "home.html",
            active_nav="home",
            collection_empty=not records.browse(db()),
            moods=moods.choices(db()),
            suggested_mood=moods.for_time(infra.now()),
        )

    @app.post("/session/start")
    def session_start():
        mood = request.form.get("mood", "")
        if mood not in moods.NAMES:
            flash("Pick a mood to start a session.")
            return redirect(url_for("home", new=1))
        with write() as tx:
            session = sessions.start(tx, mood, infra.now())
            recommendations.generate(tx, session.id, infra.now())
        return redirect(url_for("session_view"))

    # --- Session workspace ------------------------------------------------

    @app.route("/session")
    def session_view() -> str | Response:
        session = sessions.current(db())
        if session is None:
            return redirect(url_for("home"))
        query = request.args.get("q", "").strip()
        return render_template(
            "session.html",
            active_nav="home",
            session=session,
            picks=_current_picks(db(), session),
            empty_message=_empty_message(recommendations.active(db(), session.id).reason),
            query=query,
            # Search shows results only on submit; no default full-collection list.
            results=records.search(db(), query) if query else None,
            log=_session_log(db(), session),
        )

    @app.post("/session/regenerate")
    def session_regenerate() -> str:
        session = sessions.current(db())
        if session is None:
            abort(409, "No current session.")
        keep = request.form.getlist("keep")  # release ids the user pinned
        with write() as tx:
            recommendations.generate(tx, session.id, infra.now(), keep=keep)
        # htmx swaps just the recommendations panel back in.
        picks = _current_picks(db(), session)
        return render_template(
            "_picks.html",
            picks=picks,
            empty_message=_empty_message(recommendations.active(db(), session.id).reason),
        )

    @app.post("/session/log")
    def session_log():
        session = sessions.current(db())
        if session is None:
            flash("Start a session before logging a play.")
            return redirect(url_for("home"))
        with write() as tx:
            sessions.log_play(
                tx,
                session.id,
                request.form.get("instance_id", ""),
                request.form.get("release_id", ""),
                infra.now(),
            )
        # Preserve the search so several records can be logged from one query.
        return redirect(url_for("session_view", q=request.form.get("q", "")))

    @app.post("/session/remove/<play_id>")
    def session_remove(play_id: str):
        with write() as tx:
            sessions.remove_play(tx, play_id)  # enforces current-session-only
        return redirect(url_for("session_view", q=request.form.get("q", "")))

    # --- Condition --------------------------------------------------------

    @app.route("/condition")
    def condition() -> str:
        return render_template(
            "condition.html", active_nav="condition", albums=records.browse(db())
        )

    @app.post("/condition/toggle")
    def condition_toggle():
        instance_id = request.form.get("instance_id", "")
        playable = request.form.get("playable") == "on"
        with write() as tx:
            records.set_playable(tx, instance_id, playable)
        return redirect(url_for("condition"))

    # --- Settings ---------------------------------------------------------

    @app.route("/settings")
    def settings() -> str:
        present = records.styles(db())
        mapped = set(moods.affinity_map(db()))
        return render_template(
            "settings.html",
            active_nav="settings",
            window_days=int(recommendations.window(db()).days),
            moods=moods.choices(db()),
            affinity_json=json.dumps(moods.affinity_map(db()), indent=2, sort_keys=True),
            unmapped=sorted(present - mapped),  # styles nobody has classified
        )

    @app.post("/settings/window")
    def settings_window():
        try:
            days = int(request.form.get("days", ""))
            with write() as tx:
                recommendations.set_window(tx, days)
        except (ValueError, recommendations.InvalidWindow):
            flash("The recency window must be a whole number of days, zero or more.")
        return redirect(url_for("settings"))

    @app.post("/settings/mood/<name>")
    def settings_description(name: str):
        try:
            with write() as tx:
                moods.set_description(tx, name, request.form.get("description", ""))
        except moods.UnknownMood:
            abort(404)
        return redirect(url_for("settings"))

    @app.post("/settings/affinity")
    def settings_affinity():
        try:
            mapping = json.loads(request.form.get("affinity", ""))
            with write() as tx:
                moods.set_affinity_map(tx, mapping)
            flash("Affinity map saved.")
        except json.JSONDecodeError:
            flash("That is not valid JSON.")
        except (moods.InvalidAffinity, moods.UnknownMood, TypeError, AttributeError) as exc:
            flash(f"The affinity map was rejected: {exc}")
        return redirect(url_for("settings"))

    # --- Sync -------------------------------------------------------------

    @app.route("/sync")
    def sync_page() -> str:
        return render_template(
            "sync.html",
            active_nav="sync",
            run=sync.latest(db()),
            pending=records.pending_retirements(db()),
        )

    @app.post("/sync/trigger")
    def sync_trigger():
        if not sync.trigger():
            flash("A sync is already running.")
        return redirect(url_for("sync_page"))

    @app.route("/sync/progress")
    def sync_progress() -> str:
        # htmx polls this while a sync runs.
        return render_template("_sync_status.html", run=sync.latest(db()))

    @app.post("/sync/retire")
    def sync_retire():
        instance_ids = request.form.getlist("instance_id")
        with write() as tx:
            records.confirm_retirement(tx, instance_ids)
        flash(f"Retired {len(instance_ids)}.")
        return redirect(url_for("sync_page"))

    # --- Cover art --------------------------------------------------------

    @app.route("/covers/<path:filename>")
    def cover(filename: str):
        # Served from the data volume, not the static dir. The URL scheme
        # matches records._served_url: /covers/<release-id>.jpg.
        return send_from_directory(infra.covers_dir(), filename)

    return app


# --- Request-scoped session helpers ----------------------------------------


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


# --- Display helpers (join across components; decide nothing) --------------

_EMPTY_MESSAGES = {
    recommendations.EmptyReason.NOTHING_AVAILABLE: (
        "Nothing to play yet. Run a sync, or check that some records are marked playable."
    ),
    recommendations.EmptyReason.NO_FIT: (
        "Nothing in the collection fits this mood. Widen the mood's affinity map in Settings."
    ),
    recommendations.EmptyReason.ALL_RECENT: (
        "Everything that fits was played recently. Come back later, or shorten the recency "
        "window in Settings."
    ),
    recommendations.EmptyReason.SESSION_EXHAUSTED: (
        "You have already seen everything that fits this mood in this session. Start a new "
        "session, or try a different mood."
    ),
}


def _empty_message(reason: recommendations.EmptyReason | None) -> str | None:
    return _EMPTY_MESSAGES.get(reason) if reason else None


def _current_picks(session_db: Session, session: sessions.Session) -> list:
    """The active batch minus anything already played this session.

    A logged pick slides from Recommendations to the session log without a
    regenerate, by being filtered out here. The stored batch is untouched, and
    the facade's exclusion keeps it out of the next regenerate anyway.
    """
    played = {p.release_id for p in sessions.plays(session_db, session.id)}
    active = recommendations.active(session_db, session.id)
    return [r for r in active.releases if r.id not in played]


def _session_log(session_db: Session, session: sessions.Session | None) -> list[dict]:
    """The current session's plays, joined to records for artwork and titles.

    This is display composition, which the view owns: `sessions.plays` returns
    ids, and the join to `records` for a title lives here, not in a facade.
    """
    if session is None:
        return []
    entries = []
    for play in sessions.plays(session_db, session.id):
        release = records.get(session_db, play.release_id)
        entries.append({"play": play, "release": release})
    return entries
