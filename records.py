"""Collection catalog: albums, the pressings owned of them, condition, retirement.

The first component with storage, so it sets down the `_Mapper` pattern that
every other component copies (architecture RFC section 3). The conventions:

- ORM rows are private (`_ReleaseRow`, `_InstanceRow`) and never leave the
  module. Public functions accept and return frozen dataclasses only.
- `_Mapper` is session-bound, constructed per call, and is the *only* code that
  touches a table. It holds the queries and both transforms: row-to-domain
  (`_release`, `_instance`, classmethods, since they need no session) and
  domain-to-row (`upsert`).
- Public functions carry the validation and the business rules, then delegate
  persistence. A rule never lives in the mapper and SQL never lives outside it.
- Nothing here commits. The caller owns the transaction: the view commits on the
  success path, and the sync thread commits its whole fetch once at the end
  (RFC sections 9 and 10). A component that committed on its own would break
  that isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, String, func, select
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

import infra

ReleaseId = str
InstanceId = str


class UnknownInstance(Exception):
    """Raised when an instance id does not exist. A caller bug, not a user state."""


class RetirementStatus(Enum):
    ACTIVE = "active"
    PENDING = "pending"
    RETIRED = "retired"


@dataclass(frozen=True)
class Instance:
    """One owned physical copy: a Discogs collection instance."""

    id: InstanceId  # Discogs collection instance_id, the per-copy holding id
    is_playable: bool  # FR-13
    retirement_status: RetirementStatus
    pressing_release_id: int  # Discogs release id of the pressing; see _InstanceRow
    description: str | None = None  # from the format text; only useful with >1 copy


@dataclass(frozen=True)
class Release:
    """The album, across pressings. Aggregate root; an Instance is reached through it."""

    id: ReleaseId  # m<master_id>, or r<release_id> when there is no master
    artist: str
    title: str
    styles: list[str]  # drive mood fit
    cover_url: str | None  # local static URL the template renders (FR-6)
    year: int | None
    instances: list[Instance] = field(default_factory=list)
    cover_source_url: str | None = None  # Discogs origin; sync diffs it to spot a new image

    @property
    def owned_instances(self) -> list[Instance]:
        """The copies still owned: everything but the confirmed retirements.

        `instances` is the whole aggregate, retirements included, because the
        history of a sold copy still matters. What a *screen* may offer is
        narrower: a retired copy is gone, so it cannot be logged as played and
        its condition cannot be changed. Anything that renders a choice of copy
        uses this; anything reporting history uses `instances`.

        Not-playable copies stay in here on purpose. FR-13a keeps a damaged copy
        loggable; the flag suppresses suggestions, it does not deny that you put
        the record on.
        """
        return [i for i in self.instances if i.retirement_status is not RetirementStatus.RETIRED]


# --- Identity (architecture RFC section 5) ---------------------------------


def release_id(master_id: int | None, pressing_release_id: int) -> ReleaseId:
    """Resolve the Discogs ids for one listing to this system's release id.

    `m<master_id>`, falling back to `r<release_id>` for a release with no master.
    The namespace prefix is the point: without it a master id and a release id
    could alias in the one `release.id` column and silently merge two albums.

    Public because `sync` groups a collection page by this exact rule (RFC
    section 9 step 3); it must call this rather than reimplement it, or the two
    definitions drift and re-key the collection.
    """
    if master_id:  # 0 and None both mean "no master"
        return f"m{master_id}"
    return f"r{pressing_release_id}"


def cover_file(rid: ReleaseId) -> Path:
    """Where this release's cover art lives on the data volume.

    Named by release id so a retried sync overwrites rather than orphaning (RFC
    section 7). Public for the same reason as `release_id`: `sync` downloads to
    this path and the mapper renders from it, so one function owns the naming.
    """
    return infra.covers_dir() / f"{rid}.jpg"


def _served_url(rid: ReleaseId) -> str | None:
    """The static URL for a cached cover, or None if it is not on disk yet.

    Checking the file rather than trusting the column keeps a release whose art
    download failed rendering as "no artwork" instead of a broken image: sync
    records the path at upsert but fetches the bytes afterwards, and a single
    art failure is skipped rather than fatal (RFC section 9).

    The location comes from the release id, not from the `cover_path` column,
    which holds an absolute path built from `DATA_DIR` as it stood at the last
    sync: trusting it means every cover silently disappears the day the volume
    moves. The column is still written as a record of where the bytes went; the
    file on disk is the only thing consulted here.
    """
    return f"/covers/{cover_file(rid).name}" if cover_file(rid).exists() else None


# --- Storage ---------------------------------------------------------------


class _ReleaseRow(infra.Base):
    __tablename__ = "release"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    artist: Mapped[str]
    title: Mapped[str]
    year: Mapped[int | None]
    # A JSON list, not a join table (RFC section 7). Styles are only ever read
    # as a whole set per release; a join table would buy nothing.
    styles: Mapped[list[str]] = mapped_column(JSON, default=list)
    cover_path: Mapped[str | None]  # local file; the dataclass cover_url derives from it
    cover_source_url: Mapped[str | None]  # Discogs origin, for change detection

    instances: Mapped[list[_InstanceRow]] = relationship(
        back_populates="release",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="_InstanceRow.id",
    )


class _InstanceRow(infra.Base):
    __tablename__ = "instance"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # The only place the instance-to-release reference exists. It assembles the
    # aggregate and is deliberately absent from the Instance dataclass, which is
    # always reached through its Release (RFC section 5).
    # Indexed: every aggregate load and every recommendable/browse query joins
    # on it, so it is the module's hottest column after the primary keys.
    release_id: Mapped[str] = mapped_column(ForeignKey("release.id"), index=True)
    # Required, not optional (execution plan D6). Discogs master assignments
    # change over time, and a no-master-to-has-master transition re-keys an
    # album on a later sync and can split its instances and recency history.
    # That is the one accepted data-loss risk in this design, and this id is the
    # only surviving evidence of which instances used to belong together. It is
    # one integer, free from the listing payload. Do not make it nullable.
    pressing_release_id: Mapped[int]
    is_playable: Mapped[bool] = mapped_column(default=True)
    retirement_status: Mapped[RetirementStatus] = mapped_column(
        # Store the lowercase values ("active"), not the member names, so the
        # column reads the way the PRD and the URLs talk about it.
        SAEnum(RetirementStatus, values_callable=lambda e: [m.value for m in e]),
        default=RetirementStatus.ACTIVE,
    )
    description: Mapped[str | None]

    release: Mapped[_ReleaseRow] = relationship(back_populates="instances")


class _Mapper:
    """The only code in this module that touches the tables."""

    def __init__(self, db: Session):
        self._db = db

    # --- transforms --------------------------------------------------------

    @classmethod
    def _instance(cls, i: _InstanceRow) -> Instance:
        return Instance(
            id=i.id,
            is_playable=i.is_playable,
            retirement_status=i.retirement_status,
            pressing_release_id=i.pressing_release_id,
            description=i.description,
        )

    @classmethod
    def _release(cls, r: _ReleaseRow, instances: list[_InstanceRow] | None = None) -> Release:
        """Row to domain. `instances` narrows the aggregate (pending_retirements)."""
        rows = r.instances if instances is None else instances
        return Release(
            id=r.id,
            artist=r.artist,
            title=r.title,
            styles=list(r.styles or []),
            cover_url=_served_url(r.id),
            year=r.year,
            instances=[cls._instance(i) for i in rows],
            cover_source_url=r.cover_source_url,
        )

    # --- reads -------------------------------------------------------------

    def _ordered(self, stmt):
        # Artist then title (FR-12a). Lowercased so "the Beatles" and "The
        # Beatles" do not sort into different neighbourhoods.
        return stmt.order_by(func.lower(_ReleaseRow.artist), func.lower(_ReleaseRow.title))

    def browse(self) -> list[Release]:
        stmt = self._ordered(select(_ReleaseRow))
        return [self._release(r) for r in self._db.scalars(stmt)]

    def search(self, text: str) -> list[Release]:
        pattern = f"%{text}%"
        # `ilike` renders as lower(col) LIKE lower(?), so the match is
        # case-insensitive regardless of the column collation.
        stmt = self._ordered(
            select(_ReleaseRow).where(
                _ReleaseRow.artist.ilike(pattern) | _ReleaseRow.title.ilike(pattern)
            )
        )
        return [self._release(r) for r in self._db.scalars(stmt)]

    def get(self, rid: ReleaseId) -> Release | None:
        row = self._db.get(_ReleaseRow, rid)
        return self._release(row) if row else None

    def recommendable(self) -> list[Release]:
        # PENDING survives this filter on purpose: FR-2a excludes an instance
        # only once its retirement is confirmed.
        stmt = self._ordered(
            select(_ReleaseRow)
            .join(_InstanceRow)
            .where(
                _InstanceRow.is_playable,
                _InstanceRow.retirement_status != RetirementStatus.RETIRED,
            )
            .distinct()
        )
        return [self._release(r) for r in self._db.scalars(stmt)]

    def pending_retirements(self) -> list[Release]:
        stmt = self._ordered(
            select(_ReleaseRow)
            .join(_InstanceRow)
            .where(_InstanceRow.retirement_status == RetirementStatus.PENDING)
            .distinct()
        )
        return [
            # Narrowed to just the pending instances: the confirmation screen
            # asks about those copies, not about every copy of the album.
            self._release(
                r, [i for i in r.instances if i.retirement_status is RetirementStatus.PENDING]
            )
            for r in self._db.scalars(stmt)
        ]

    def styles(self) -> set[str]:
        # Flattened in Python because `styles` is a JSON list; SQLite has no
        # portable DISTINCT over the elements of one.
        rows = self._db.scalars(select(_ReleaseRow.styles))
        return {s for row in rows for s in (row or [])}

    def get_instance(self, iid: InstanceId) -> Instance | None:
        row = self._db.get(_InstanceRow, iid)
        return self._instance(row) if row else None

    # --- writes ------------------------------------------------------------

    def upsert(self, release: Release) -> None:
        """Domain to rows. Metadata only."""
        row = self._db.get(_ReleaseRow, release.id) or _ReleaseRow(id=release.id)
        row.artist = release.artist
        row.title = release.title
        row.year = release.year
        row.styles = list(release.styles)
        row.cover_source_url = release.cover_source_url
        # The path is derived, never supplied: one function owns cover naming.
        row.cover_path = str(cover_file(release.id)) if release.cover_source_url else None
        self._db.add(row)

        for inst in release.instances:
            irow = self._db.get(_InstanceRow, inst.id)
            if irow is None:
                # Only a brand-new row gets its condition and retirement set,
                # and then from the dataclass defaults, not from sync.
                irow = _InstanceRow(
                    id=inst.id,
                    is_playable=inst.is_playable,
                    retirement_status=inst.retirement_status,
                )
            # Metadata only. is_playable and retirement_status are deliberately
            # absent from this block: they are local behavioral state and a sync
            # must never write them on an existing row (FR-2).
            irow.release_id = release.id
            irow.pressing_release_id = inst.pressing_release_id
            irow.description = inst.description
            self._db.add(irow)

    def all_instances(self) -> list[_InstanceRow]:
        return list(self._db.scalars(select(_InstanceRow)))

    def set_status(self, rows: list[_InstanceRow], status: RetirementStatus) -> None:
        for row in rows:
            row.retirement_status = status

    def set_playable(self, iid: InstanceId, playable: bool) -> None:
        self._db.get(_InstanceRow, iid).is_playable = playable

    def confirm_retirement(self, iids: list[InstanceId]) -> None:
        for iid in iids:
            self._db.get(_InstanceRow, iid).retirement_status = RetirementStatus.RETIRED


# --- Public surface (architecture RFC section 5.1) -------------------------
#
# Validate or enforce the rule here, then delegate persistence to the mapper.


def browse(db: Session) -> list[Release]:
    """Every owned album, artist then title (FR-12a).

    Does not filter on playability: a not-playable copy is still browsable and
    still loggable (FR-13a). The flag suppresses suggestions only.
    """
    return _Mapper(db).browse()


def search(db: Session, text: str) -> list[Release]:
    """Albums whose artist or title matches, case-insensitive (FR-12a).

    Like `browse`, does not filter on playability (FR-13a).
    """
    return _Mapper(db).search(text)


def get(db: Session, rid: ReleaseId) -> Release | None:
    return _Mapper(db).get(rid)


def recommendable(db: Session) -> list[Release]:
    """Albums with at least one playable instance whose status is not RETIRED."""
    return _Mapper(db).recommendable()


def pending_retirements(db: Session) -> list[Release]:
    """Albums narrowed to their PENDING instances, for the confirmation list (FR-2a)."""
    return _Mapper(db).pending_retirements()


def styles(db: Session) -> set[str]:
    """Distinct styles present; the view diffs these against the affinity map (FR-18)."""
    return _Mapper(db).styles()


def upsert(db: Session, release: Release) -> None:
    """Write album and pressing metadata from a sync (FR-1).

    Never touches condition or retirement on an existing row (FR-2). Retirement
    is a whole-collection set difference, so it is `reconcile_retirements`, not
    part of this per-album write.
    """
    _Mapper(db).upsert(release)


def reconcile_retirements(db: Session, present_instance_ids: set[InstanceId]) -> list[Instance]:
    """Diff the whole collection against what the fetch found (FR-2a).

    An instance held locally but absent from Discogs becomes PENDING; one that
    reappears flips back to ACTIVE and reconnects to its existing play history.
    Returns the instances newly flagged PENDING, which is what a sync reports.

    Only safe on a *complete* fetch: a partial one would look like a mass
    disappearance and flag the whole collection. The caller enforces that (RFC
    section 9), which is why this is a separate function from `upsert`.
    """
    m = _Mapper(db)
    newly_pending: list[_InstanceRow] = []
    reappeared: list[_InstanceRow] = []
    for row in m.all_instances():
        if row.id in present_instance_ids:
            # Covers RETIRED as well as PENDING: FR-2a un-retires on reappearance.
            if row.retirement_status is not RetirementStatus.ACTIVE:
                reappeared.append(row)
        elif row.retirement_status is RetirementStatus.ACTIVE:
            newly_pending.append(row)
        # Absent and already PENDING or RETIRED: nothing to say, leave it.
    m.set_status(reappeared, RetirementStatus.ACTIVE)
    m.set_status(newly_pending, RetirementStatus.PENDING)
    return [_Mapper._instance(row) for row in newly_pending]


def confirm_retirement(db: Session, instance_ids: list[InstanceId]) -> None:
    """The user confirms the pending retirements; only now are they excluded (FR-2a)."""
    m = _Mapper(db)
    for iid in instance_ids:
        if m.get_instance(iid) is None:
            raise UnknownInstance(iid)
    m.confirm_retirement(instance_ids)


def set_playable(db: Session, instance_id: InstanceId, playable: bool) -> None:
    """Mark one copy playable or not (FR-13)."""
    m = _Mapper(db)
    if m.get_instance(instance_id) is None:
        raise UnknownInstance(instance_id)
    m.set_playable(instance_id, playable)
