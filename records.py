"""Collection catalog: albums, the pressings owned of them, condition, retirement.

The first component with storage, so it sets down the `_Mapper` pattern the
others copy:

- ORM rows are private and never leave the module; public functions accept and
  return frozen dataclasses only.
- `_Mapper` is session-bound and the only code that touches a table.
- Rules and validation live in the public functions, never in the mapper; SQL
  lives in the mapper, never outside it.
- Nothing here commits. The caller owns the transaction.
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
    is_playable: bool
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
    cover_url: str | None  # local static URL the template renders
    year: int | None
    instances: list[Instance] = field(default_factory=list)
    cover_source_url: str | None = None  # Discogs origin; sync diffs it to spot a new image

    @property
    def owned_instances(self) -> list[Instance]:
        """The copies still owned. Anything offering a choice of copy uses this;
        anything reporting history uses `instances`, which keeps retirements
        because a sold copy's plays still count.

        Not-playable copies stay in: the flag suppresses suggestions, it does not
        deny that you put the record on.
        """
        return [i for i in self.instances if i.retirement_status is not RetirementStatus.RETIRED]


# --- Identity --------------------------------------------------------------


def release_id(master_id: int | None, pressing_release_id: int) -> ReleaseId:
    """The Discogs ids for one listing, resolved to our release id.

    `m<master_id>`, falling back to `r<release_id>` when there is no master. The
    namespace prefix stops a master id and a release id aliasing in the one
    `release.id` column and silently merging two albums.

    Public because `sync` groups by this rule and must not reimplement it.
    """
    if master_id:  # 0 and None both mean "no master"
        return f"m{master_id}"
    return f"r{pressing_release_id}"


def cover_file(rid: ReleaseId) -> Path:
    """Where this release's cover art lives. Named by release id so a retried
    sync overwrites rather than orphaning. `sync` writes here and the mapper
    renders from here, so one function owns the naming."""
    return infra.covers_dir() / f"{rid}.jpg"


def _served_url(rid: ReleaseId) -> str | None:
    """The static URL for a cached cover, or None if it is not on disk.

    Consults the file, not the `cover_path` column: sync records the path but
    fetches the bytes afterwards and skips failures, so a missing file must read
    as "no artwork" rather than a broken image. The column also holds an absolute
    path built from `DATA_DIR` as it stood at the last sync, so trusting it would
    lose every cover the day the volume moves.
    """
    return f"/covers/{cover_file(rid).name}" if cover_file(rid).exists() else None


# --- Storage ---------------------------------------------------------------


class _ReleaseRow(infra.Base):
    __tablename__ = "release"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    artist: Mapped[str]
    title: Mapped[str]
    year: Mapped[int | None]
    # A JSON list, not a join table. Styles are only ever read
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
    # The only place the instance-to-release reference exists; absent from the
    # Instance dataclass, which is always reached through its Release. Indexed
    # because every aggregate load and every browse query joins on it.
    release_id: Mapped[str] = mapped_column(ForeignKey("release.id"), index=True)
    # Required, not optional. Discogs master assignments change over time, and a
    # no-master-to-has-master transition re-keys an album on a later sync,
    # splitting its instances and recency history. This id is then the only
    # evidence of which instances used to belong together. Do not make it
    # nullable.
    pressing_release_id: Mapped[int]
    is_playable: Mapped[bool] = mapped_column(default=True)
    retirement_status: Mapped[RetirementStatus] = mapped_column(
        # Store the lowercase values ("active"), not the member names, so the
        # column reads the way the URLs do.
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
        # Lowercased so "the Beatles" and "The Beatles" do not sort into
        # different neighbourhoods.
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
        # PENDING survives on purpose: an instance is excluded only once the
        # user confirms its retirement.
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
            # Narrowed to the pending copies: the confirmation screen asks about
            # those, not about every copy of the album.
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
            # Metadata only. is_playable and retirement_status are absent by
            # design: they are local state and a sync must never overwrite them.
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


# --- Public surface --------------------------------------------------------
#
# Validate or enforce the rule here, then delegate persistence to the mapper.


def browse(db: Session) -> list[Release]:
    """Every owned album, artist then title.

    Does not filter on playability: a damaged copy is still browsable and still
    loggable. The flag suppresses suggestions only.
    """
    return _Mapper(db).browse()


def search(db: Session, text: str) -> list[Release]:
    """Albums whose artist or title matches, case-insensitive. Like `browse`,
    does not filter on playability."""
    return _Mapper(db).search(text)


def get(db: Session, rid: ReleaseId) -> Release | None:
    return _Mapper(db).get(rid)


def recommendable(db: Session) -> list[Release]:
    """Albums with at least one playable instance whose status is not RETIRED."""
    return _Mapper(db).recommendable()


def pending_retirements(db: Session) -> list[Release]:
    """Albums narrowed to their PENDING copies, for the confirmation list."""
    return _Mapper(db).pending_retirements()


def styles(db: Session) -> set[str]:
    """Distinct styles present. The view diffs these against the affinity map to
    surface styles nobody has classified yet."""
    return _Mapper(db).styles()


def upsert(db: Session, release: Release) -> None:
    """Write album and pressing metadata from a sync.

    Never touches condition or retirement on an existing row. Retirement is a
    whole-collection set difference, so it lives in `reconcile_retirements`
    rather than in this per-album write.
    """
    _Mapper(db).upsert(release)


def reconcile_retirements(db: Session, present_instance_ids: set[InstanceId]) -> list[Instance]:
    """Diff the whole collection against what the fetch found.

    An instance held locally but absent from Discogs becomes PENDING; one that
    reappears flips back to ACTIVE and keeps its play history. Returns the newly
    pending instances, which is what a sync reports.

    Only safe on a *complete* fetch, since a partial one looks like a mass
    disappearance and would flag the whole collection. That is why this is
    separate from `upsert`: the caller decides when the fetch is complete.
    """
    m = _Mapper(db)
    newly_pending: list[_InstanceRow] = []
    reappeared: list[_InstanceRow] = []
    for row in m.all_instances():
        if row.id in present_instance_ids:
            # Covers RETIRED as well as PENDING: a copy that comes back un-retires.
            if row.retirement_status is not RetirementStatus.ACTIVE:
                reappeared.append(row)
        elif row.retirement_status is RetirementStatus.ACTIVE:
            newly_pending.append(row)
        # Absent and already PENDING or RETIRED: nothing to say, leave it.
    m.set_status(reappeared, RetirementStatus.ACTIVE)
    m.set_status(newly_pending, RetirementStatus.PENDING)
    return [_Mapper._instance(row) for row in newly_pending]


def confirm_retirement(db: Session, instance_ids: list[InstanceId]) -> None:
    """The user confirms the pending retirements. Only now are they excluded."""
    m = _Mapper(db)
    for iid in instance_ids:
        if m.get_instance(iid) is None:
            raise UnknownInstance(iid)
    m.confirm_retirement(instance_ids)


def set_playable(db: Session, instance_id: InstanceId, playable: bool) -> None:
    """Mark one copy playable or not."""
    m = _Mapper(db)
    if m.get_instance(instance_id) is None:
        raise UnknownInstance(instance_id)
    m.set_playable(instance_id, playable)
