"""Background Discogs sync: fetch the collection, upsert via records, cache art,
flag retirements, report progress.

The design lives in architecture RFC section 9; the parts that are easy to get
wrong, and why they are the way they are:

- **One run at a time, guarded by a module lock, released in a `finally`.** A
  non-blocking `acquire()` means a second trigger no-ops rather than starting a
  concurrent sync. The lock is released in a `finally` covering the whole thread
  body, because releasing only on success would strand it for the life of the
  process if the thread raised, leaving sync dead with nothing on screen (D2).

- **Two sessions, and the split is the point (D8).** A long-lived *data* session
  stays open across the whole fetch and commits once at the end, so a partial or
  failed fetch rolls back and writes no half-collection. A short-lived
  *progress* session commits `sync_run` per page. They cannot be one session:
  progress written inside the data transaction is invisible to the polling
  request (a different connection), so the bar would sit at zero and jump to
  done; committing progress from the data session would break the failure
  isolation. `sync_run` is metadata about the run, not collection data, so
  committing it early leaves nothing partial behind.

- **Retirement is reconciled once, inside the data transaction, only on a
  complete fetch.** A crash mid-fetch must not retire anything.

Sync never touches plays, condition flags, or session history: `records.upsert`
writes metadata only, and nothing here calls into `sessions`.
"""

from __future__ import annotations

import logging
import threading
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from sqlalchemy import Enum as SAEnum
from sqlalchemy import select
from sqlalchemy.orm import Mapped, Session, mapped_column

import infra
import records

log = logging.getLogger(__name__)

USER_AGENT = "Rotation33/0.1 +https://github.com/benpencodes/rotation33"
PER_PAGE = 50


class SyncStatus(Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


@dataclass(frozen=True)
class SyncRun:
    """The latest sync's state, for progress polling and 'last synced' (FR-1)."""

    id: int
    status: SyncStatus
    total: int
    processed: int
    started_at: datetime
    finished_at: datetime | None
    error: str | None


# --- Storage (architecture RFC section 7) ----------------------------------


class _SyncRunRow(infra.Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[SyncStatus] = mapped_column(
        SAEnum(SyncStatus, values_callable=lambda e: [m.value for m in e]),
    )
    total: Mapped[int] = mapped_column(default=0)
    processed: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None]


# --- The collection source -------------------------------------------------
#
# `_run` consumes anything with a `total` and a `pages()` that yields pages of
# raw Discogs listing dicts (the fixture shape). The real adapter wraps the
# client; a test passes the captured fixture. No Protocol: the contract is these
# two members, and one real implementation plus one fake do not need ceremony.


class _DiscogsCollection:
    def __init__(self, folder):
        self._folder = folder

    @property
    def total(self) -> int:
        return self._folder.count

    def pages(self) -> Iterator[list[dict]]:
        releases = self._folder.releases
        releases.per_page = PER_PAGE
        for page_no in range(1, releases.pages + 1):
            yield [item.data for item in releases.page(page_no)]


def _make_collection(client=None):
    if client is None:
        import discogs_client

        client = discogs_client.Client(USER_AGENT, user_token=infra.discogs_token())
    folder = client.user(infra.discogs_username()).collection_folders[0]  # 0 is "All"
    return _DiscogsCollection(folder)


# --- Listing to domain (the same mapping tools/e2e_demo.py prototyped) ------


def _is_vinyl(item: dict) -> bool:
    """Kept if *any* format is Vinyl (RFC section 9 step 2), so an LP-plus-CD
    edition survives rather than being dropped for the CD it also ships."""
    formats = item["basic_information"].get("formats") or []
    return any(f.get("name") == "Vinyl" for f in formats)


def _artist(basic: dict) -> str:
    parts: list[str] = []
    for artist in basic.get("artists") or []:
        parts.append(artist.get("anv") or artist.get("name") or "")
        if artist.get("join"):
            parts.append(artist["join"])
    return " ".join(p for p in parts if p).strip() or "Unknown Artist"


def _description(basic: dict) -> str | None:
    """A label that tells copies of one album apart (only useful with >1 copy).

    The distinguishing detail — colour, edition — lives in `formats[].text`. The
    format `descriptions` ("LP", "Album") are the same across pressings and do
    not tell two copies apart (research doc section 4). So prefer the free text,
    and fall back to the descriptions only when a pressing carries none.
    """
    texts = [f["text"].strip() for f in basic.get("formats") or [] if (f.get("text") or "").strip()]
    if texts:
        return ", ".join(texts)
    quals = ", ".join(d for f in basic.get("formats") or [] for d in (f.get("descriptions") or []))
    return quals or None


def _accumulate(grouped: dict[str, records.Release], item: dict) -> None:
    """Fold one vinyl instance into its release, creating the release on first
    sight. Grouping calls `records.release_id` rather than restating the rule,
    so the two definitions of album identity cannot drift and re-key the
    collection (RFC section 9 step 3)."""
    basic = item["basic_information"]
    rid = records.release_id(basic.get("master_id"), basic["id"])
    release = grouped.get(rid)
    if release is None:
        release = grouped[rid] = records.Release(
            id=rid,
            artist=_artist(basic),
            title=basic.get("title") or "Untitled",
            styles=list(basic.get("styles") or []),
            cover_url=None,
            year=basic.get("year") or None,
            instances=[],
            cover_source_url=basic.get("cover_image"),
        )
    release.instances.append(
        records.Instance(
            id=str(item["instance_id"]),
            is_playable=True,  # a default for a new row; upsert ignores it on an existing one
            retirement_status=records.RetirementStatus.ACTIVE,
            pressing_release_id=basic["id"],
            description=_description(basic),
        )
    )


# --- Cover art -------------------------------------------------------------


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (https only, our URL)
        return response.read()


def _cache_cover(release: records.Release, fetch) -> None:
    """Download the cover to its release-id-named file. A single failure is
    logged and skipped, never fatal, and the old file is kept (RFC section 9)."""
    path = records.cover_file(release.id)
    try:
        data = fetch(release.cover_source_url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except Exception:
        log.warning("cover download failed for %s, keeping any existing file", release.id)


def _cache_covers(releases: list[records.Release], fetch) -> None:
    """Download every wanted cover, *after* the data transaction has committed.

    This runs outside the transaction on purpose, and the ordering is load
    bearing. SQLAlchemy autoflushes before a query, so on a first sync the
    `db.get` inside `records.upsert` flushes the previous release and the data
    session takes SQLite's database-wide write lock on the very first album.
    Downloading inside that loop therefore held the write lock for the entire
    run: measured at 7.86s of a 7.87s sync against the 72-release fixture with
    40ms stub downloads, and over real network latency a concurrent `log_play`
    blew through the 5s `busy_timeout` and returned `database is locked`. A
    first sync is exactly when every cover is missing, so it was the first-run
    path that broke.

    Deferring is safe because the covers are not collection data: `upsert`
    records `cover_path` during the transaction and `records._served_url` renders
    from the file's existence, so a release whose art has not landed yet (or ever)
    shows the placeholder rather than a broken image.
    """
    for release in releases:
        _cache_cover(release, fetch)


# --- Progress (the short-lived committing session, D8) ---------------------


def _open_run(total: int) -> int:
    with infra.SessionLocal.begin() as db:
        row = _SyncRunRow(status=SyncStatus.RUNNING, total=total, started_at=infra.now())
        db.add(row)
        db.flush()
        return row.id


def _set_total(run_id: int, total: int) -> None:
    with infra.SessionLocal.begin() as db:
        db.get(_SyncRunRow, run_id).total = total


def _advance(run_id: int, processed: int) -> None:
    with infra.SessionLocal.begin() as db:
        db.get(_SyncRunRow, run_id).processed = processed


def _close_run(run_id: int, status: SyncStatus, error: str | None = None) -> None:
    with infra.SessionLocal.begin() as db:
        row = db.get(_SyncRunRow, run_id)
        row.status = status
        row.finished_at = infra.now()
        row.error = error


# --- The sync itself -------------------------------------------------------


def _run(collection, run_id: int | None = None, fetch=_http_get) -> None:
    """Walk the collection once. Commits collection data exactly once, at the end.

    `run_id` is supplied by `trigger`, which opens the run row synchronously (so
    the sync page can start polling immediately) before the total is known, then
    lets this fill the total in. Called directly in tests with no `run_id`, it
    opens its own run, so the total is set from the start.

    Exposed (underscored) for direct testing against a fixture-backed collection,
    without threads. `trigger` is the production entry point.
    """
    if run_id is None:
        run_id = _open_run(collection.total)
    else:
        _set_total(run_id, collection.total)
    grouped: dict[str, records.Release] = {}
    present_ids: set[str] = set()
    processed = 0
    wanted_covers: list[records.Release] = []

    data = infra.SessionLocal()
    try:
        for page in collection.pages():
            for item in page:
                if not _is_vinyl(item):
                    continue
                _accumulate(grouped, item)
                present_ids.add(str(item["instance_id"]))
            processed += len(page)
            _advance(run_id, processed)

        for rid, release in grouped.items():
            existing = records.get(data, rid)
            stale_cover = existing is None or existing.cover_source_url != release.cover_source_url
            records.upsert(data, release)
            if release.cover_source_url and (stale_cover or not records.cover_file(rid).exists()):
                # Noted, not fetched. See _cache_covers for why the download
                # cannot happen here.
                wanted_covers.append(release)

        # Only on a complete fetch, inside the one data transaction (RFC section 9).
        records.reconcile_retirements(data, present_ids)
        data.commit()
    except Exception as exc:
        data.rollback()
        _close_run(run_id, SyncStatus.FAILED, error=str(exc))
        raise
    finally:
        data.close()

    # After the commit, deliberately: see _cache_covers.
    _cache_covers(wanted_covers, fetch)
    _close_run(run_id, SyncStatus.COMPLETE)


# --- Public surface --------------------------------------------------------

_lock = threading.Lock()


def trigger(client=None) -> bool:
    """Start a sync in a background thread (FR-1). Returns False, no-op, if one
    is already running: a non-blocking lock, since two threaded requests can
    interleave between a DB check and a write (RFC section 9).

    The run row is opened here, synchronously, before the thread is spawned, so a
    page that renders right after this returns already sees a `running` row and
    starts polling. The total is 0 until the thread learns it from the network.

    The spawn is guarded because `_run_and_release`'s `finally` only covers the
    thread body: if `_open_run` or `Thread.start()` raised, nothing would ever
    release the lock and sync would be dead for the life of the process, which is
    the exact failure that `finally` exists to prevent."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        run_id = _open_run(0)
        threading.Thread(target=_run_and_release, args=(run_id, client), daemon=True).start()
    except BaseException:
        _lock.release()
        raise
    return True


def _run_and_release(run_id: int, client) -> None:
    try:
        _run(_make_collection(client), run_id)
    except Exception as exc:
        # Backstop: catches a failure in _make_collection (before _run can mark
        # anything) as well as anything _run re-raised. Idempotent with _run's
        # own failure mark, so a double-mark is harmless. Never crash the thread.
        _close_run(run_id, SyncStatus.FAILED, error=str(exc))
        log.exception("sync thread failed")
    finally:
        _lock.release()


def latest(db: Session) -> SyncRun | None:
    """The most recent run, for progress polling and 'last synced'."""
    row = db.scalars(
        select(_SyncRunRow).order_by(_SyncRunRow.started_at.desc(), _SyncRunRow.id.desc())
    ).first()
    return _domain(row) if row else None


def reconcile_orphaned_runs(db: Session) -> int:
    """Mark any run still `running` as failed. Called once at app startup: the
    lock guards concurrency within a process but not a process killed mid-sync,
    which would otherwise leave a run `running` forever and a bar polling it
    (RFC section 9). Returns how many were reconciled. Caller frames the txn."""
    orphans = list(db.scalars(select(_SyncRunRow).where(_SyncRunRow.status == SyncStatus.RUNNING)))
    for row in orphans:
        row.status = SyncStatus.FAILED
        row.error = "interrupted by a restart"
        row.finished_at = infra.now()
    return len(orphans)


def _domain(row: _SyncRunRow) -> SyncRun:
    return SyncRun(
        id=row.id,
        status=row.status,
        total=row.total,
        processed=row.processed,
        started_at=row.started_at,
        finished_at=row.finished_at,
        error=row.error,
    )
