"""A listening sitting and the plays logged into it; release-level recency.

A session stays current until the next one starts. There is no midnight
rollover and no calendar day anywhere in this module (architecture RFC section
6): `start` always creates a new session, and the prior one simply stops being
the latest.

`Play` carries a denormalized `release_id`. That is what lets this module answer
the recency question from its own table, and it is why `sessions` does not
import `records` (RFC sections 3 and 5.3). A test asserts the absence of that
import, because the denormalization only earns its keep if the independence it
buys is actually kept.

Conventions follow `records`: private ORM rows, a session-bound `_Mapper` as the
only code touching a table, rules in the public functions, and nothing commits.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func, select

# Aliased: this module's own domain `Session` is the sitting, not the DB handle.
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm import Session as DbSession

import infra

ReleaseId = str
InstanceId = str


class UnknownPlay(Exception):
    """Raised when a play id does not exist. A caller bug, not a user state."""


class UnknownSession(Exception):
    """Raised when a session id does not exist."""


class NotCurrentSession(Exception):
    """Raised when a play outside the current session is removed (FR-12b).

    Only the active session is editable; earlier history is not. Refusing here
    rather than in the view keeps the rule with the data it protects.
    """


# --- Domain ----------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """A sitting: a chosen mood and when it began."""

    id: str  # uuid4 hex, minted by start()
    mood: str  # chosen mood name
    started_at: datetime


@dataclass(frozen=True)
class Play:
    """One instance logged as played into a session."""

    id: str  # uuid4 hex, minted by log_play()
    session_id: str
    instance_id: InstanceId  # which copy was played
    release_id: ReleaseId  # denormalized, so recency needs no join to `records`
    played_at: datetime


def _new_id() -> str:
    return uuid.uuid4().hex


# --- Storage (RFC section 7) ----------------------------------------------


class _SessionRow(infra.Base):
    __tablename__ = "session"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    mood: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    # No relationship to plays: the aggregate is never loaded whole. `plays` is
    # a query by session id and `latest_plays` is an aggregate across all of
    # them, so a relationship would only add a lazy-load footgun.


class _PlayRow(infra.Base):
    __tablename__ = "play"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.id"), index=True)
    instance_id: Mapped[str]
    # Denormalized from the instance at log time (RFC section 5.3). It is what
    # keeps a retired instance's plays contributing to release recency (FR-2a):
    # the release id is recorded here and survives whatever later happens to the
    # copy that was played. Indexed because `latest_plays` groups on it.
    release_id: Mapped[str] = mapped_column(String, index=True)
    played_at: Mapped[datetime] = mapped_column(DateTime)


class _Mapper:
    """The only code in this module that touches the tables."""

    def __init__(self, db: DbSession):
        self._db = db

    # --- transforms --------------------------------------------------------

    @classmethod
    def _session(cls, s: _SessionRow) -> Session:
        return Session(id=s.id, mood=s.mood, started_at=s.started_at)

    @classmethod
    def _play(cls, p: _PlayRow) -> Play:
        return Play(
            id=p.id,
            session_id=p.session_id,
            instance_id=p.instance_id,
            release_id=p.release_id,
            played_at=p.played_at,
        )

    # --- reads -------------------------------------------------------------

    def current(self) -> Session | None:
        # Latest by clock. The id tiebreak only makes the ordering total; two
        # sessions sharing a started_at to the microsecond is not a real state,
        # since the caller passes infra.now() and a human cannot start two
        # sittings in the same instant.
        stmt = (
            select(_SessionRow)
            .order_by(_SessionRow.started_at.desc(), _SessionRow.id.desc())
            .limit(1)
        )
        row = self._db.scalars(stmt).first()
        return self._session(row) if row else None

    def session_exists(self, session_id: str) -> bool:
        return self._db.get(_SessionRow, session_id) is not None

    def get(self, session_id: str) -> Session | None:
        row = self._db.get(_SessionRow, session_id)
        return self._session(row) if row else None

    def plays(self, session_id: str) -> list[Play]:
        stmt = (
            select(_PlayRow)
            .where(_PlayRow.session_id == session_id)
            .order_by(_PlayRow.played_at, _PlayRow.id)
        )
        return [self._play(p) for p in self._db.scalars(stmt)]

    def get_play(self, play_id: str) -> Play | None:
        row = self._db.get(_PlayRow, play_id)
        return self._play(row) if row else None

    def latest_plays(self) -> dict[ReleaseId, datetime]:
        # One row per release ever played, so the result is bounded by the
        # collection size rather than by the length of play history.
        stmt = select(_PlayRow.release_id, func.max(_PlayRow.played_at)).group_by(
            _PlayRow.release_id
        )
        return {release_id: last for release_id, last in self._db.execute(stmt)}

    # --- writes ------------------------------------------------------------

    def add_session(self, session: Session) -> None:
        self._db.add(_SessionRow(id=session.id, mood=session.mood, started_at=session.started_at))

    def add_play(self, play: Play) -> None:
        self._db.add(
            _PlayRow(
                id=play.id,
                session_id=play.session_id,
                instance_id=play.instance_id,
                release_id=play.release_id,
                played_at=play.played_at,
            )
        )

    def delete_play(self, play_id: str) -> None:
        self._db.delete(self._db.get(_PlayRow, play_id))


# --- Public surface (architecture RFC section 5.3) -------------------------


def current(db: DbSession) -> Session | None:
    """The latest session. None only on a virgin database.

    A long-idle session staying current is intended, not a bug: a session ends
    when the next one starts (RFC section 6), and the open-app flow leads with
    starting one.
    """
    return _Mapper(db).current()


def get(db: DbSession, session_id: str) -> Session:
    """One session by id. Raises rather than returning None.

    Added for the `recommendations` facade, which is handed a `session_id` and
    needs the mood behind it (RFC section 5.4 step 1). The RFC's section 5.3
    interface list omits this, but every operation the facade performs on a
    session is keyed by id rather than by "the current one", and conflating the
    two would make `generate` fail on a valid-but-not-latest session.

    Optional-vs-raising follows `moods.get`, not `records.get`: a session id
    only ever arrives from a session this system minted, so a miss is a caller
    bug rather than a user state.
    """
    session = _Mapper(db).get(session_id)
    if session is None:
        raise UnknownSession(session_id)
    return session


def start(db: DbSession, mood: str, now: datetime) -> Session:
    """Begin a new sitting. Always creates; the prior session stops being latest.

    The mood name is not validated against `moods` here: this module does not
    import other components (RFC section 3), and the view only ever offers the
    five.
    """
    session = Session(id=_new_id(), mood=mood, started_at=now)
    _Mapper(db).add_session(session)
    return session


def log_play(
    db: DbSession,
    session_id: str,
    instance_id: InstanceId,
    release_id: ReleaseId,
    played_at: datetime,
) -> Play:
    """Log one copy as played into a session (FR-11).

    Both ids are recorded: the instance because that is what actually played,
    and the release because recency is release-level and must outlive the copy.
    """
    m = _Mapper(db)
    if not m.session_exists(session_id):
        raise UnknownSession(session_id)
    play = Play(
        id=_new_id(),
        session_id=session_id,
        instance_id=instance_id,
        release_id=release_id,
        played_at=played_at,
    )
    m.add_play(play)
    return play


def remove_play(db: DbSession, play_id: str) -> None:
    """Delete a play from the *current* session (FR-12b).

    Removal is outright; there is no edit. Because recency is derived from the
    remaining plays, deleting one immediately restores that release's
    eligibility with no special case anywhere else.

    Only the active session is editable, so a play belonging to any earlier
    session is refused.
    """
    m = _Mapper(db)
    play = m.get_play(play_id)
    if play is None:
        raise UnknownPlay(play_id)
    active = m.current()
    if active is None or play.session_id != active.id:
        raise NotCurrentSession(play_id)
    m.delete_play(play_id)


def plays(db: DbSession, session_id: str) -> list[Play]:
    """This session's plays, oldest first: the session log and FR-9's exclusion.

    Returns ids only. The view joins to `records` for artwork and titles, since
    a component enriches results only from what it already depends on, and this
    one depends on nothing (RFC section 3).
    """
    return _Mapper(db).plays(session_id)


def latest_plays(db: DbSession) -> dict[ReleaseId, datetime]:
    """The most recent play per release: one entry per release ever played.

    The single recency read. The facade derives both the recency exclusion
    (`now - dt <= window`) and the staleness ranking (`now - dt`, never-played
    ranking first) from this one dict, so there is exactly one definition of
    "when did this album last play".

    A retired instance's plays still appear here, because a play stores its own
    `release_id` and this never looks at the instance (FR-2a).
    """
    return _Mapper(db).latest_plays()
