"""Sync, exercised entirely against the captured fixture, no Discogs and no
network. The fixture is the contract (execution plan section 2); the one live
run is the Phase 5 exit gate, done by hand.

The assertions read results from fresh sessions rather than the setup session,
so a stale read snapshot can never mask what sync actually committed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import infra
import records
import sessions
import sync

FIXTURE = Path(__file__).parent / "fixtures" / "collection.json"

# Facts about the captured collection, asserted against so a fixture change that
# quietly drops one of these shapes fails loudly here.
VINYL_ALBUMS = 72
TOTAL_ITEMS = 75
MUTINY = "m4160569"  # a master with two owned vinyl pressings and one CD
MUTINY_VINYL = {"2124727382", "2124926800"}
MUTINY_CD = "2124926875"
VINYL_PLUS_CD = "m9000002"  # the hand-added multi-format edition
NO_MASTER = ("r37907304", "r37109289")  # master_id: 0 falls back to r<release_id>


def items() -> list[dict]:
    return json.loads(FIXTURE.read_text())["items"]


def _noop_fetch(_url: str) -> bytes:
    return b"IMG"


class FakeCollection:
    """Feeds `_run` the fixture in pages. `probe`, if given, is called with the
    running total after each page's progress has been committed, which is where
    the D8 mid-fetch-visibility assertion lives."""

    def __init__(self, rows: list[dict], per_page: int = 50, probe=None):
        self._rows = rows
        self._per_page = per_page
        self._probe = probe

    @property
    def total(self) -> int:
        return len(self._rows)

    def pages(self):
        seen = 0
        for start in range(0, len(self._rows), self._per_page):
            page = self._rows[start : start + self._per_page]
            yield page
            seen += len(page)
            if self._probe is not None:
                self._probe(seen)


class Boom(Exception):
    pass


@pytest.fixture
def read(engine):
    """A factory for fresh sessions, so a result read never rides a snapshot the
    setup session opened before sync committed."""

    def _open():
        return infra.SessionLocal()

    return _open


# --- Vinyl filter and identity ---------------------------------------------


class TestMapping:
    def test_syncs_the_vinyl_albums(self, engine, read):
        sync._run(FakeCollection(items()), fetch=_noop_fetch)
        with read() as s:
            assert len(records.browse(s)) == VINYL_ALBUMS
            run = sync.latest(s)
            assert run.status is sync.SyncStatus.COMPLETE
            assert run.total == TOTAL_ITEMS
            assert run.processed == TOTAL_ITEMS

    def test_drops_the_cd_and_keeps_both_vinyl_pressings_of_a_shared_master(self, engine, read):
        sync._run(FakeCollection(items()), fetch=_noop_fetch)
        with read() as s:
            release = records.get(s, MUTINY)
            assert {i.id for i in release.instances} == MUTINY_VINYL
            assert MUTINY_CD not in {i.id for i in release.instances}

    def test_keeps_a_multi_format_vinyl_plus_cd_release(self, engine, read):
        sync._run(FakeCollection(items()), fetch=_noop_fetch)
        with read() as s:
            assert records.get(s, VINYL_PLUS_CD) is not None

    def test_no_master_items_fall_back_to_r_release_id(self, engine, read):
        sync._run(FakeCollection(items()), fetch=_noop_fetch)
        with read() as s:
            for rid in NO_MASTER:
                assert records.get(s, rid) is not None

    def test_a_cd_only_release_is_absent_entirely(self, engine, read):
        sync._run(FakeCollection(items()), fetch=_noop_fetch)
        with read() as s:
            assert records.get(s, "r37561794") is None  # not a real synced id


# --- Retirement reconciliation ---------------------------------------------


class TestRetirement:
    def test_absent_instance_is_flagged_pending_then_reactivated(self, engine, read):
        all_items = items()
        sync._run(FakeCollection(all_items), fetch=_noop_fetch)

        gone = str(next(iter(MUTINY_VINYL)))
        subset = [it for it in all_items if str(it["instance_id"]) != gone]
        sync._run(FakeCollection(subset), fetch=_noop_fetch)
        with read() as s:
            inst = next(i for i in records.get(s, MUTINY).instances if i.id == gone)
            assert inst.retirement_status is records.RetirementStatus.PENDING

        sync._run(FakeCollection(all_items), fetch=_noop_fetch)
        with read() as s:
            inst = next(i for i in records.get(s, MUTINY).instances if i.id == gone)
            assert inst.retirement_status is records.RetirementStatus.ACTIVE


# --- Failure isolation ------------------------------------------------------


class TestFailureIsolation:
    def test_a_mid_fetch_exception_writes_nothing_and_marks_the_run_failed(self, engine, read):
        class Raising(FakeCollection):
            def pages(self):
                yield self._rows[:10]
                raise Boom("connection dropped")

        with pytest.raises(Boom):
            sync._run(Raising(items()), fetch=_noop_fetch)

        with read() as s:
            assert records.browse(s) == []  # no half-collection
            run = sync.latest(s)
            assert run.status is sync.SyncStatus.FAILED
            assert run.error

    def test_a_failed_sync_leaves_a_prior_successful_one_intact(self, engine, read):
        sync._run(FakeCollection(items()), fetch=_noop_fetch)

        class Raising(FakeCollection):
            def pages(self):
                yield self._rows[:5]
                raise Boom("dropped")

        with pytest.raises(Boom):
            sync._run(Raising(items()), fetch=_noop_fetch)

        with read() as s:
            assert len(records.browse(s)) == VINYL_ALBUMS  # the good data survives
            assert sync.latest(s).status is sync.SyncStatus.FAILED

    def test_sync_never_touches_plays_condition_or_session_history(self, engine, read):
        sync._run(FakeCollection(items()), fetch=_noop_fetch)

        inst_id = next(iter(MUTINY_VINYL))
        with infra.SessionLocal.begin() as s:
            records.set_playable(s, inst_id, False)
            session = sessions.start(s, "After Dark", infra.now())
            sessions.log_play(s, session.id, inst_id, MUTINY, infra.now())
            session_id = session.id

        sync._run(FakeCollection(items()), fetch=_noop_fetch)  # re-sync over the top

        with read() as s:
            inst = next(i for i in records.get(s, MUTINY).instances if i.id == inst_id)
            assert inst.is_playable is False  # condition flag untouched (FR-2)
            assert inst.retirement_status is records.RetirementStatus.ACTIVE
            assert len(sessions.plays(s, session_id)) == 1  # play survives


# --- Concurrency and recovery ----------------------------------------------


class TestGuardAndRecovery:
    def test_a_second_trigger_no_ops_while_one_holds_the_lock(self):
        assert sync._lock.acquire(blocking=False)
        try:
            assert sync.trigger() is False  # returns without spawning a thread
        finally:
            sync._lock.release()

    def test_a_thread_that_raises_before_running_still_releases_the_lock(self, engine, monkeypatch):
        def explode(_client):
            raise Boom("could not reach discogs")

        monkeypatch.setattr(sync, "_make_collection", explode)
        run_id = sync._open_run(0)
        sync._lock.acquire()  # trigger would have taken it before spawning

        sync._run_and_release(run_id, None)  # must not propagate, must release in finally

        assert sync._lock.acquire(blocking=False), "lock was stranded"
        sync._lock.release()
        with infra.SessionLocal() as s:
            assert sync.latest(s).status is sync.SyncStatus.FAILED  # marked, not left running

    def test_trigger_creates_a_visible_running_row_before_the_thread_works(self, engine):
        # The regression guard: the run row must exist and read `running` the
        # instant trigger() returns, before the background thread does its
        # network fetch. Otherwise the sync page renders "never synced" and never
        # starts polling.
        import threading

        release_it = threading.Event()

        def stall(run_id, client):
            release_it.wait(5)  # hold the thread so it does no real work
            sync._lock.release()

        original = sync._run_and_release
        sync._run_and_release = stall
        try:
            assert sync.trigger() is True
            with infra.SessionLocal() as s:
                run = sync.latest(s)
                assert run is not None, "no run row after trigger returned"
                assert run.status is sync.SyncStatus.RUNNING
        finally:
            release_it.set()
            sync._run_and_release = original

    def test_a_run_left_running_by_a_crash_is_reconciled_on_startup(self, engine, read):
        with infra.SessionLocal.begin() as s:
            s.add(
                sync._SyncRunRow(
                    status=sync.SyncStatus.RUNNING,
                    total=50,
                    processed=12,
                    started_at=infra.now(),
                )
            )

        with infra.SessionLocal.begin() as s:
            assert sync.reconcile_orphaned_runs(s) == 1

        with read() as s:
            run = sync.latest(s)
            assert run.status is sync.SyncStatus.FAILED
            assert run.error

    def test_reconcile_is_a_no_op_when_nothing_is_running(self, engine):
        with infra.SessionLocal.begin() as s:
            assert sync.reconcile_orphaned_runs(s) == 0


# --- Progress, and the D8 two-session split --------------------------------


class TestProgress:
    def test_progress_is_readable_from_another_connection_mid_fetch(self, engine):
        # The assertion that catches a regression back to a single session: if
        # progress were written on the long-lived data transaction, a fresh
        # connection would read 0 until the whole sync committed.
        observed: list[int] = []

        def probe(processed_so_far: int) -> None:
            with infra.SessionLocal() as other:
                run = sync.latest(other)
                assert run.processed == processed_so_far  # committed, and visible
                assert run.status is sync.SyncStatus.RUNNING
                assert records.browse(other) == []  # data session has not committed
                observed.append(run.processed)

        sync._run(FakeCollection(items(), per_page=20, probe=probe), fetch=_noop_fetch)

        assert observed == [20, 40, 60, 75]  # advanced page by page, not in one jump

    def test_latest_is_none_before_any_sync(self, engine, read):
        with read() as s:
            assert sync.latest(s) is None


# --- Cover art --------------------------------------------------------------


class TestCoverArt:
    def test_downloads_a_cover_per_album_and_writes_the_file(self, engine, read):
        calls: list[str] = []

        def fetch(url: str) -> bytes:
            calls.append(url)
            return b"IMG"

        sync._run(FakeCollection(items()), fetch=fetch)

        assert len(calls) == VINYL_ALBUMS
        assert records.cover_file(MUTINY).read_bytes() == b"IMG"

    def test_an_unchanged_cover_is_not_redownloaded(self, engine):
        calls: list[str] = []

        def fetch(url: str) -> bytes:
            calls.append(url)
            return b"IMG"

        sync._run(FakeCollection(items()), fetch=fetch)
        assert len(calls) == VINYL_ALBUMS
        calls.clear()

        sync._run(FakeCollection(items()), fetch=fetch)  # files exist, urls unchanged
        assert calls == []

    def test_a_cover_download_failure_is_not_fatal(self, engine, read):
        def boom(_url: str) -> bytes:
            raise OSError("no network")

        sync._run(FakeCollection(items()), fetch=boom)

        with read() as s:
            assert sync.latest(s).status is sync.SyncStatus.COMPLETE  # sync still succeeds
            assert len(records.browse(s)) == VINYL_ALBUMS  # metadata still written
        assert not records.cover_file(MUTINY).exists()  # no file, old one would be kept
