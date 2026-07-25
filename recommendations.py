"""The facade that turns a mood and a collection into picks, and remembers them.

This is the one component that imports downward. The rule in architecture RFC
section 3 is "no import cycles", not "no imports": a facade whose whole job is
coordination has to reach the things it coordinates. So `recommendations`
depends on `records`, `sessions`, `moods` and `picker`, and none of those four
depends on it or on each other.

That dependency is also what lets this module return fully rendered `Release`
objects rather than bare ids. `sessions` returns ids and makes the view join,
because it cannot see `records`; this module already can, so making the caller
join again would be gratuitous.

Everything else follows the conventions `records` set down: private ORM rows, a
session-bound `_Mapper` as the only code touching a table, rules and validation
in the public functions, and nothing commits. The caller owns the transaction.
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
"""How many picks `generate` asks for. `picker` returns up to this many.

FR-4 says "3 to 5", but the 3 is not a floor (RFC section 8): a thin pool
yielding one or two picks is a valid result shown as-is. Only an empty draw is
FR-10's explained-empty case.
"""

DEFAULT_WINDOW_DAYS = 3
"""Code default for the recency window (FR-14, RFC section 11).

Not an env var: it is user-editable at runtime and persisted here.
"""


class InvalidWindow(Exception):
    """Raised when a recency window is set to something nonsensical."""


class EmptyReason(Enum):
    """Why a generate came back with nothing (FR-10).

    An enum rather than a message so the view owns the wording and the reason
    stays testable.
    """

    NOTHING_AVAILABLE = "nothing_available"  # no playable, non-retired records at all
    NO_FIT = "no_fit"  # records exist, none fit this mood
    ALL_RECENT = "all_recent"  # fit exists, all of it played inside the recency window
    SESSION_EXHAUSTED = "session_exhausted"  # fit exists and is not recent; this session saw it


@dataclass(frozen=True)
class RecommendationResult:
    """A batch of picks, or an empty batch that knows why it is empty.

    `reason` travels in the same object as `releases` precisely so a caller
    cannot render the empty list and drop the explanation on the floor (RFC
    section 5.4). It is set only when `releases` is empty.
    """

    releases: list[records.Release]  # in draw order; empty when nothing was drawn
    reason: EmptyReason | None = None


# --- Storage (RFC section 7) ----------------------------------------------


class _RecommendationRow(infra.Base):
    """One drawn release. A batch is the rows sharing a `generated_at`.

    Rows accumulate and are never pruned, which is fine at single-user scale and
    is what makes the FR-9 "already shown earlier in this session" exclusion a
    plain query rather than something the caller has to carry.
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
    # Without one, an empty generate wrote no rows at all, so the next read found
    # the *previous* batch and silently resurrected picks the user had already
    # rejected, taking the FR-10 explanation with it.
    release_id: Mapped[str | None] = mapped_column(String, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    # Draw order is the product: the weighted draw ranks the picks, and losing
    # that ordering on the round trip would silently reduce the batch to a set.
    position: Mapped[int]
    # Set only on a marker row: why this batch came back empty (FR-10).
    empty_reason: Mapped[str | None] = mapped_column(String, default=None)


class _WindowRow(infra.Base):
    """The recency window, as one row (RFC section 7).

    "No row" means "use the code default", so a fresh database needs no seeding
    step and a later change to `DEFAULT_WINDOW_DAYS` reaches every install that
    never touched the setting.
    """

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
        """Every release this session has ever been shown (FR-9).

        Marker rows carry no release and are skipped: an empty batch showed the
        user nothing, so it cannot have used anything up.
        """
        stmt = select(_RecommendationRow.release_id).where(
            _RecommendationRow.session_id == session_id,
            _RecommendationRow.release_id.is_not(None),
        )
        return set(self._db.scalars(stmt))

    def latest_batch(self, session_id: str) -> tuple[list[ReleaseId], EmptyReason | None]:
        """The active batch: its release ids in draw order, and why it is empty.

        The batch is the greatest `generated_at` for the session (RFC section
        7), read as a scalar subquery so the two statements cannot disagree. A
        batch is either some picks or a single marker row carrying the reason;
        never both.
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

        A drew-nothing batch is still a batch. Writing the marker is what stops
        the next read finding the *previous* batch and resurrecting picks the
        user already rejected, and it is what carries the FR-10 explanation
        across a page reload.
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


# --- Public surface (architecture RFC section 5.4) -------------------------


def generate(
    db: Session,
    session_id: str,
    now: datetime,
    rng: random.Random | None = None,
    keep: Sequence[ReleaseId] = (),
) -> RecommendationResult:
    """Draw a new batch of picks for this session's mood and persist it.

    First-generate and regenerate are one act (FR-4, FR-9): a regenerate is just
    a generate with more rows already on the table to exclude.

    `keep` carries pinned picks from the current batch into the new one, so a
    regenerate reshuffles only the unpinned slots (the session workspace). A kept
    release still in the recommendable pool leads the new batch, in the order
    passed, which is its display order; the draw fills the remaining slots and
    excludes the kept ids so it never duplicates one. A kept release that has
    since left the pool (retired, marked not-playable) is dropped, exactly as
    `active` drops a vanished pick.

    The empty outcome is a value, not an exception, because "no picks, and why"
    is an expected state (FR-10). Genuine faults still raise: an unknown
    `session_id` raises `sessions.UnknownSession`, and an unknown mood raises
    `moods.UnknownMood`. The latter is deliberate. `sessions.start` cannot
    validate the mood name (it imports no components), so a bad one survives
    until it reaches here, and swallowing it into an `EmptyReason` would render
    a typo as "nothing fits this mood" and hide the bug. Validating at the POST
    boundary is the view layer's job.
    """
    session = sessions.get(db, session_id)
    # Field-for-field identical types across the engine swap boundary, restated
    # rather than shared so `picker` imports no component (RFC section 5.5).
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

    # Fit is computed before exclusion and kept: it is what separates "nothing
    # fits this mood" from "things fit, but you have played them all" (FR-10).
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

    An explicit branch on the never-played case, and deliberately so (RFC
    section 5.4 step 3). The sentinel is only ever sorted on, never added to a
    datetime, so it cannot overflow. Deriving the same ranking by subtracting a
    sentinel *date* would break on the never-played path alone, which is the
    common path on a freshly synced collection and so the one least likely to be
    caught late.
    """
    if last_play is None:
        return timedelta.max
    return now - last_play


def _exclusions(
    db: Session, session_id: str, now: datetime, recency: dict[ReleaseId, datetime]
) -> tuple[set[ReleaseId], set[ReleaseId]]:
    """Every release this batch may not contain, split by *why*.

    Three sources, all of them exclusions rather than penalties, so "why wasn't
    X picked" stays answerable:

    - played inside the recency window, by any pressing (FR-4);
    - already logged into this session (FR-5, and FR-9's "already played");
    - already shown earlier in this session and passed over (FR-9).

    Returned as two sets rather than one union because the split is exactly what
    `_reason` needs. Folding them together made a session that had merely *seen*
    everything report "played recently", which is a different sentence and, for
    a user who has played nothing, a false one.

    The window comparison is `<=`, so a play exactly at the boundary is still
    excluded: 2d23h ago is out at a 3-day window, 3d1h ago is eligible (RFC
    section 6).
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
    """Why an empty draw was empty (FR-10). Order matters.

    Narrowest true statement first: an empty collection is not "nothing fits
    this mood", and a fully-recent collection is not "nothing fits" either.
    Testing fit before the pool would report NO_FIT for an empty collection,
    which is technically true and useless.

    The last two are the ones worth telling apart, because the remedy differs.
    If everything that fits is inside the recency window, waiting or widening the
    window helps. If it is not, and the draw still came back empty, then this
    session has already been shown or played all of it: a new session helps and
    the recency window is irrelevant.
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
    drops out and the batch shows fewer. That silent shrink is intended and is
    the only place a persisted pick can vanish: the alternative is leaving a
    record you have just sold sitting in the picks, and the user's next
    regenerate resolves it anyway.

    Rehydrating from `recommendable` rather than by id is what makes that work
    without a second definition of "may this be recommended": the batch is the
    ids, the pool is the rule, and the answer is their intersection. One query
    also replaces the five `records.get` calls this used to make.

    The `EmptyReason` is the one `generate` persisted with the batch, so an empty
    outcome survives a page reload with its explanation attached. `None` here
    means no batch has been generated for this session yet, which is a different
    state from FR-10's "nothing qualifies" and reads as such: no message.
    """
    ids, reason = _Mapper(db).latest_batch(session_id)
    eligible = {r.id: r for r in records.recommendable(db)}
    return RecommendationResult(
        releases=[eligible[rid] for rid in ids if rid in eligible], reason=reason
    )


def window(db: Session) -> timedelta:
    """The recency window (FR-14). `DEFAULT_WINDOW_DAYS` until someone sets one."""
    days = _Mapper(db).window_days()
    return timedelta(days=DEFAULT_WINDOW_DAYS if days is None else days)


def set_window(db: Session, days: int) -> None:
    """Persist the user-adjusted recency window (FR-14).

    Zero is allowed and meaningful: it makes cross-session immediate repeats
    possible, which is what zero means (RFC section 6). Intra-session repeats
    are still blocked by session scope (FR-9). Negative is not, since it would
    exclude nothing while reading as though it excluded something.
    """
    if not isinstance(days, int) or isinstance(days, bool):
        raise InvalidWindow(f"window must be a whole number of days, got {days!r}")
    if days < 0:
        raise InvalidWindow(f"window cannot be negative, got {days}")
    _Mapper(db).set_window_days(days)
