"""The facade that turns a mood and a collection into picks, and remembers them.

The one component that imports downward. The rule is "no import cycles", not
"no imports": a facade whose whole job is coordination has to reach the things
it coordinates, and none of `records`, `sessions`, `moods` or `picker` depends
on it or on each other. That dependency is also what lets this module return
rendered `Release` objects instead of bare ids.

Conventions otherwise as `records`: private ORM rows, a session-bound `_Mapper`
as the only code touching a table, rules in the public functions, no commits.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

import infra
import moods
import picker
import records
import sessions

ReleaseId = str

COUNT = 5
"""How many picks `generate` asks for; `picker` returns up to this many.

Not a floor. A thin pool yielding one or two picks is shown as-is; only an empty
draw gets an explanation.
"""

DEFAULT_WINDOW_DAYS = 3
"""Recency window default. Not an env var: user-editable and persisted here."""


class InvalidWindow(Exception):
    """Raised when a recency window is set to something nonsensical."""


class EmptyReason(Enum):
    """Why a generate came back with nothing. An enum rather than a message, so
    the view owns the wording and the reason stays testable."""

    NOTHING_AVAILABLE = "nothing_available"  # no playable, non-retired records at all
    NO_FIT = "no_fit"  # records exist, none fit this mood
    ALL_RECENT = "all_recent"  # fit exists, all of it played inside the recency window
    SESSION_EXHAUSTED = "session_exhausted"  # fit exists and is not recent; this session saw it


@dataclass(frozen=True)
class RecommendationResult:
    """A batch of picks, or an empty batch that knows why it is empty.

    `reason` rides in the same object as `releases` so a caller cannot render the
    empty list and drop the explanation. Set only when `releases` is empty.
    """

    releases: list[records.Release]  # in draw order; empty when nothing was drawn
    reason: EmptyReason | None = None


# --- Storage ---------------------------------------------------------------


class _RecommendationRow(infra.Base):
    """One drawn release. A batch is the rows sharing a `generated_at`.

    Rows accumulate and are never pruned, which is fine at single-user scale and
    turns "already shown earlier in this session" into a plain query.
    """

    __tablename__ = "recommendation"

    # A surrogate key rather than the natural (session_id, generated_at,
    # position). Two generates can share a `generated_at` when a caller passes
    # the same `now` twice, which is not a real production state but is routine
    # in tests, and a natural key would turn it into an integrity error instead
    # of the harmless batch merge it is.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("session.id"), index=True)
    # Null on a marker row, which is how a batch that drew nothing is recorded.
    # Without one, an empty generate writes no rows, so the next read finds the
    # *previous* batch and resurrects picks the user already rejected.
    release_id: Mapped[str | None] = mapped_column(String, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Draw order is the product: the weighted draw ranks the picks, and losing
    # that ordering on the round trip would silently reduce the batch to a set.
    position: Mapped[int]
    # Set only on a marker row: why this batch came back empty.
    empty_reason: Mapped[str | None] = mapped_column(String, default=None)


class _WindowRow(infra.Base):
    """The recency window, as one row. "No row" means "use the code default", so
    a fresh database needs no seeding and a later change to
    `DEFAULT_WINDOW_DAYS` reaches every install that never touched it."""

    __tablename__ = "recommendation_window"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    days: Mapped[int]


_SINGLETON = 1


class _Mapper:
    """The only code in this module that touches the tables."""

    def __init__(self, db: Session):
        self._db = db

    # --- reads -------------------------------------------------------------

    def shown_release_ids(self, session_id: str) -> set[ReleaseId]:
        """Every release this session has been shown. Marker rows are skipped: an
        empty batch showed nothing, so it used nothing up."""
        stmt = select(_RecommendationRow.release_id).where(
            _RecommendationRow.session_id == session_id,
            _RecommendationRow.release_id.is_not(None),
        )
        return set(self._db.scalars(stmt))

    def latest_batch(self, session_id: str) -> tuple[list[ReleaseId], EmptyReason | None]:
        """The active batch: its release ids in draw order, and why it is empty.

        The batch is the greatest `generated_at` for the session, read as a
        scalar subquery so the two statements cannot disagree. A batch is either
        picks or a single marker row, never both.
        """
        newest = (
            select(func.max(_RecommendationRow.generated_at))
            .where(_RecommendationRow.session_id == session_id)
            .scalar_subquery()
        )
        stmt = (
            select(_RecommendationRow)
            .where(
                _RecommendationRow.session_id == session_id,
                _RecommendationRow.generated_at == newest,
            )
            .order_by(_RecommendationRow.position)
        )
        rows = list(self._db.scalars(stmt))
        marker = rows[0].empty_reason if rows else None
        return [r.release_id for r in rows if r.release_id is not None], (
            EmptyReason(marker) if marker else None
        )

    def window_days(self) -> int | None:
        """The persisted override, or None when nothing has been set."""
        row = self._db.get(_WindowRow, _SINGLETON)
        return row.days if row else None

    # --- writes ------------------------------------------------------------

    def add_batch(
        self,
        session_id: str,
        release_ids: list[ReleaseId],
        generated_at: datetime,
        reason: EmptyReason | None = None,
    ) -> None:
        """Persist a batch: a row per pick, or one marker row carrying `reason`.

        A drew-nothing batch is still a batch. The marker stops the next read
        finding the previous one, and carries the explanation across a reload.
        """
        for position, rid in enumerate(release_ids or [None]):
            self._db.add(
                _RecommendationRow(
                    session_id=session_id,
                    release_id=rid,
                    generated_at=generated_at,
                    position=position,
                    empty_reason=None if rid else reason.value,
                )
            )

    def set_window_days(self, days: int) -> None:
        row = self._db.get(_WindowRow, _SINGLETON) or _WindowRow(id=_SINGLETON)
        row.days = days
        self._db.add(row)


# --- Public surface --------------------------------------------------------


def generate(
    db: Session,
    session_id: str,
    now: datetime,
    rng: random.Random | None = None,
    keep: Sequence[ReleaseId] = (),
) -> RecommendationResult:
    """Draw a new batch of picks for this session's mood and persist it.

    First-generate and regenerate are one act: a regenerate is a generate with
    more rows already on the table to exclude.

    `keep` carries pinned picks into the new batch, so a regenerate reshuffles
    only the unpinned slots. Kept releases lead, in the order passed; the draw
    fills the rest and excludes them so it cannot duplicate one. A kept release
    that has since left the pool is dropped, as `active` drops a vanished pick.

    The empty outcome is a value, not an exception: "no picks, and why" is an
    expected state. Genuine faults still raise, including an unknown mood --
    `sessions.start` imports no components and so cannot validate the name, and
    swallowing it into an `EmptyReason` would render a typo as "nothing fits this
    mood" and hide the bug.
    """
    session = sessions.get(db, session_id)
    # Identical types across the engine swap boundary, restated rather than
    # shared so `picker` imports no component.
    mood_affinity = moods.affinity(db, session.mood)
    affinity = picker.Affinity(
        weights=mood_affinity.weights,
        mapped_styles=mood_affinity.mapped_styles,
    )

    pool = records.recommendable(db)
    recency = sessions.latest_plays(db)
    pool_ids = {r.id for r in pool}

    # Kept picks, filtered to those still recommendable and de-duplicated while
    # preserving the order they were passed.
    kept: list[ReleaseId] = []
    for rid in keep:
        if rid in pool_ids and rid not in kept:
            kept.append(rid)

    candidates = [
        picker.Candidate(
            release_id=r.id,
            styles=r.styles,
            staleness=_staleness(now, recency.get(r.id)),
        )
        for r in pool
    ]

    # Fit is computed before exclusion and kept: it separates "nothing fits this
    # mood" from "things fit, but you have played them all".
    fit = picker.matching(candidates, affinity)

    # The kept ids join the exclusion set so the draw cannot redraw one.
    recent, session_seen = _exclusions(db, session_id, now, recency)
    excluded = recent | session_seen | set(kept)
    surviving = [c for c in fit if c.release_id not in excluded]

    drawn = picker.draw(surviving, COUNT - len(kept), rng)
    batch = kept + drawn  # kept lead in display order, fresh draws follow

    reason = _reason(pool, fit, recent) if not batch else None
    _Mapper(db).add_batch(session_id, batch, now, reason)
    if not batch:
        return RecommendationResult(releases=[], reason=reason)

    # Built by filtering the pool already in hand, not by re-reading each id:
    # the rows were loaded a few lines ago and nothing has changed since.
    order = {rid: i for i, rid in enumerate(batch)}
    releases = sorted((r for r in pool if r.id in order), key=lambda r: order[r.id])
    return RecommendationResult(releases=releases, reason=None)


def _staleness(now: datetime, last_play: datetime | None) -> timedelta:
    """How long since this release last played; `timedelta.max` if it never has.

    An explicit branch, deliberately. The sentinel is only ever sorted on, never
    added to a datetime, so it cannot overflow; deriving the same ranking by
    subtracting a sentinel *date* would break on the never-played path alone,
    which is the common path on a fresh collection.
    """
    if last_play is None:
        return timedelta.max
    return now - last_play


def _exclusions(
    db: Session, session_id: str, now: datetime, recency: dict[ReleaseId, datetime]
) -> tuple[set[ReleaseId], set[ReleaseId]]:
    """Every release this batch may not contain, split by *why*.

    Three sources, all exclusions rather than penalties, so "why wasn't X picked"
    stays answerable: played inside the recency window by any pressing, already
    logged into this session, or already shown and passed over in it.

    Two sets rather than one union because the split is what `_reason` needs.
    Folded together, a session that had merely *seen* everything reported
    "played recently", which for a user who has played nothing is false.

    The window comparison is `<=`: 2d23h ago is out at a 3-day window, 3d1h ago
    is eligible.
    """
    recent_window = window(db)
    recent = {rid for rid, last in recency.items() if now - last <= recent_window}
    session_seen = {p.release_id for p in sessions.plays(db, session_id)}
    session_seen |= _Mapper(db).shown_release_ids(session_id)
    return recent, session_seen


def _reason(
    pool: list[records.Release],
    fit: list[picker.Candidate],
    recent: set[ReleaseId],
) -> EmptyReason:
    """Why an empty draw was empty. Order matters: narrowest true statement
    first, since testing fit before the pool would report NO_FIT for an empty
    collection, which is true and useless.

    The last two differ in remedy. Everything inside the recency window means
    waiting or widening it helps; otherwise this session has already seen it all,
    and only a new session helps.
    """
    if not pool:
        return EmptyReason.NOTHING_AVAILABLE
    if not fit:
        return EmptyReason.NO_FIT
    if all(c.release_id in recent for c in fit):
        return EmptyReason.ALL_RECENT
    return EmptyReason.SESSION_EXHAUSTED


def active(db: Session, session_id: str) -> RecommendationResult:
    """The batch currently showing, re-hydrated. Empty when none has been drawn.

    A release removed, retired or marked unplayable since the batch was drawn
    drops out and the batch shows fewer, which is the only place a persisted pick
    can vanish. The alternative is leaving a record you have just sold sitting in
    the picks.

    Rehydrating from `recommendable` rather than by id avoids a second definition
    of "may this be recommended": the batch is the ids, the pool is the rule, and
    the answer is their intersection.

    `reason` is the one `generate` persisted, so an empty outcome survives a
    reload with its explanation. `None` means nothing has been generated yet,
    which is a different state and renders no message.
    """
    ids, reason = _Mapper(db).latest_batch(session_id)
    eligible = {r.id: r for r in records.recommendable(db)}
    return RecommendationResult(
        releases=[eligible[rid] for rid in ids if rid in eligible], reason=reason
    )


def window(db: Session) -> timedelta:
    """The recency window. `DEFAULT_WINDOW_DAYS` until someone sets one."""
    days = _Mapper(db).window_days()
    return timedelta(days=DEFAULT_WINDOW_DAYS if days is None else days)


def set_window(db: Session, days: int) -> None:
    """Persist the user-adjusted recency window.

    Zero is allowed and meaningful: it permits cross-session immediate repeats,
    which is what zero means. Intra-session repeats stay blocked by session
    scope. Negative is rejected, since it would exclude nothing while reading as
    though it excluded something.
    """
    if not isinstance(days, int) or isinstance(days, bool):
        raise InvalidWindow(f"window must be a whole number of days, got {days!r}")
    if days < 0:
        raise InvalidWindow(f"window cannot be negative, got {days}")
    _Mapper(db).set_window_days(days)
