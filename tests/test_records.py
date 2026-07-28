"""Phase 2: the collection catalog.

The assertions that matter here are the ones protecting local state from a sync
from a sync, the ones keeping a not-playable copy browsable, and the one
holding the `_Mapper` boundary that every later component copies.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy.orm import DeclarativeBase

import records
from records import Instance, Release, RetirementStatus


def instance(iid, *, playable=True, status=RetirementStatus.ACTIVE, pressing=1, desc=None):
    return Instance(
        id=iid,
        is_playable=playable,
        retirement_status=status,
        pressing_release_id=pressing,
        description=desc,
    )


def release(rid, artist="Artist", title="Title", styles=(), instances=(), year=1970, cover=None):
    return Release(
        id=rid,
        artist=artist,
        title=title,
        styles=list(styles),
        cover_url=None,
        year=year,
        instances=list(instances),
        cover_source_url=cover,
    )


# --- Identity --------------------------------------------------------------


@pytest.mark.parametrize(
    ("master_id", "pressing", "expected"),
    [
        (1234, 99, "m1234"),
        (0, 99, "r99"),  # the no-master shape the sync spike found
        (None, 99, "r99"),  # master_id absent from basic_information entirely
    ],
)
def test_release_id_namespaces_master_and_release_ids(master_id, pressing, expected):
    assert records.release_id(master_id, pressing) == expected


def test_release_id_namespace_prevents_aliasing():
    """The whole point of the prefix: the same integer must not collide."""
    assert records.release_id(7, 999) != records.release_id(0, 7)


# --- Reads -----------------------------------------------------------------


@pytest.fixture
def collection(db):
    """Three albums covering every recommendable case."""
    records.upsert(
        db,
        release(
            "m1",
            artist="Zappa",
            title="Apostrophe",
            styles=["Jazz-Rock", "Prog"],
            instances=[instance("i1")],
        ),
    )
    records.upsert(
        db,
        release(
            "m2",
            artist="alice coltrane",
            title="Journey",
            styles=["Jazz", "Prog"],
            instances=[instance("i2", playable=False)],
        ),
    )
    records.upsert(
        db,
        release(
            "m3",
            artist="Miles Davis",
            title="Bitches Brew",
            styles=["Jazz"],
            instances=[instance("i3")],
        ),
    )
    records.confirm_retirement(db, ["i3"])
    return db


def test_recommendable_excludes_retired_and_not_playable(collection):
    assert [r.id for r in records.recommendable(collection)] == ["m1"]


def test_recommendable_keeps_pending(collection):
    """An instance is excluded only once its retirement is *confirmed*."""
    records.reconcile_retirements(collection, present_instance_ids=set())
    assert records.get(collection, "m1").instances[0].retirement_status is (
        RetirementStatus.PENDING
    )
    assert "m1" in [r.id for r in records.recommendable(collection)]


def test_recommendable_keeps_an_album_with_one_good_copy_among_bad_ones(db):
    records.upsert(
        db,
        release("m1", instances=[instance("bad", playable=False), instance("good")]),
    )
    assert [r.id for r in records.recommendable(db)] == ["m1"]


def test_browse_does_not_filter_on_playability(collection):
    """A not-playable copy is suppressed from suggestions, not from the shelf."""
    assert {r.id for r in records.browse(collection)} == {"m1", "m2", "m3"}


def test_search_does_not_filter_on_playability(collection):
    assert [r.id for r in records.search(collection, "Journey")] == ["m2"]


def test_browse_orders_by_artist_then_title(db):
    records.upsert(db, release("m1", artist="Bowie", title="Low"))
    records.upsert(db, release("m2", artist="Bowie", title="Heroes"))
    records.upsert(db, release("m3", artist="Aphex Twin", title="Xtal"))
    assert [(r.artist, r.title) for r in records.browse(db)] == [
        ("Aphex Twin", "Xtal"),
        ("Bowie", "Heroes"),
        ("Bowie", "Low"),
    ]


@pytest.mark.parametrize("text", ["alice", "ALICE", "AlIcE"])
def test_search_is_case_insensitive_on_artist(collection, text):
    assert [r.id for r in records.search(collection, text)] == ["m2"]


@pytest.mark.parametrize("text", ["bitches", "BITCHES", "Bitches"])
def test_search_is_case_insensitive_on_title(collection, text):
    assert [r.id for r in records.search(collection, text)] == ["m3"]


def test_search_matches_either_field(collection):
    assert {r.id for r in records.search(collection, "jour")} == {"m2"}
    assert {r.id for r in records.search(collection, "zappa")} == {"m1"}


def test_get_returns_none_for_an_unknown_release(db):
    assert records.get(db, "m404") is None


def test_styles_returns_the_distinct_set_present(collection):
    assert records.styles(collection) == {"Jazz-Rock", "Prog", "Jazz"}


def test_styles_is_empty_on_a_virgin_database(db):
    assert records.styles(db) == set()


def test_pending_retirements_narrows_the_album_to_its_pending_copies(db):
    records.upsert(db, release("m1", instances=[instance("keep"), instance("gone")]))
    records.reconcile_retirements(db, present_instance_ids={"keep"})

    pending = records.pending_retirements(db)
    assert [r.id for r in pending] == ["m1"]
    assert [i.id for i in pending[0].instances] == ["gone"]


# --- upsert: sync never writes local state ---------------------------------


def test_upsert_creates_a_new_release_with_defaults(db):
    records.upsert(
        db,
        release(
            "m1",
            artist="Sun Ra",
            title="Lanquidity",
            styles=["Free Jazz"],
            instances=[instance("i1", pressing=555, desc="LP, Album")],
        ),
    )
    got = records.get(db, "m1")
    assert (got.artist, got.title, got.year) == ("Sun Ra", "Lanquidity", 1970)
    assert got.styles == ["Free Jazz"]
    assert got.cover_url is None  # nothing downloaded yet
    inst = got.instances[0]
    assert inst.is_playable is True
    assert inst.retirement_status is RetirementStatus.ACTIVE
    assert inst.pressing_release_id == 555
    assert inst.description == "LP, Album"


def test_upsert_never_touches_is_playable_or_retirement_on_an_existing_row(db):
    """A sync is one-way and must not write local behavioral state.

    The re-upsert below carries the values a fresh Discogs listing would carry
    (playable, active) against a row the user has since marked not-playable and
    that a previous sync flagged pending. Both local values must survive.
    """
    records.upsert(db, release("m1", instances=[instance("i1", desc="LP")]))
    records.set_playable(db, "i1", False)
    records.reconcile_retirements(db, present_instance_ids=set())

    records.upsert(
        db,
        release("m1", artist="Renamed", instances=[instance("i1", desc="LP, Reissue")]),
    )

    inst = records.get(db, "m1").instances[0]
    assert inst.is_playable is False
    assert inst.retirement_status is RetirementStatus.PENDING
    # ...while the metadata it *is* allowed to write did update.
    assert inst.description == "LP, Reissue"
    assert records.get(db, "m1").artist == "Renamed"


def test_upsert_does_not_resurrect_a_confirmed_retirement(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.confirm_retirement(db, ["i1"])
    records.upsert(db, release("m1", instances=[instance("i1")]))
    assert records.get(db, "m1").instances[0].retirement_status is RetirementStatus.RETIRED


def test_upsert_updates_metadata_on_an_existing_release(db):
    records.upsert(db, release("m1", styles=["Rock"], year=1970, instances=[instance("i1")]))
    records.upsert(
        db,
        release("m1", styles=["Rock", "Psychedelic"], year=1971, instances=[instance("i1")]),
    )
    got = records.get(db, "m1")
    assert got.styles == ["Rock", "Psychedelic"]
    assert got.year == 1971
    assert [i.id for i in got.instances] == ["i1"]  # updated in place, not duplicated


def test_upsert_adds_a_newly_acquired_copy_to_an_existing_album(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.upsert(db, release("m1", instances=[instance("i1"), instance("i2")]))
    assert {i.id for i in records.get(db, "m1").instances} == {"i1", "i2"}


def test_upsert_stores_the_pressing_release_id(db):
    """The durable handle for a master reassignment."""
    records.upsert(db, release("m1", instances=[instance("i1", pressing=8675309)]))
    assert records.get(db, "m1").instances[0].pressing_release_id == 8675309


def test_cover_url_derives_from_the_cached_file(db, data_dir):
    records.upsert(db, release("m1", cover="https://img.discogs.com/abc.jpg"))
    assert records.get(db, "m1").cover_url is None  # recorded, not yet downloaded

    records.cover_file("m1").write_bytes(b"jpegbytes")
    assert records.get(db, "m1").cover_url == "/covers/m1.jpg"


def test_cover_source_url_round_trips_for_sync_change_detection(db):
    records.upsert(db, release("m1", cover="https://img.discogs.com/abc.jpg"))
    assert records.get(db, "m1").cover_source_url == "https://img.discogs.com/abc.jpg"


# --- reconcile_retirements -------------------------------------------------


def test_reconcile_flags_absent_instances_pending(db):
    records.upsert(db, release("m1", instances=[instance("here"), instance("sold")]))

    flagged = records.reconcile_retirements(db, present_instance_ids={"here"})

    assert [i.id for i in flagged] == ["sold"]
    by_id = {i.id: i for i in records.get(db, "m1").instances}
    assert by_id["sold"].retirement_status is RetirementStatus.PENDING
    assert by_id["here"].retirement_status is RetirementStatus.ACTIVE


def test_reconcile_flips_a_reappeared_instance_back_to_active(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.reconcile_retirements(db, present_instance_ids=set())

    records.reconcile_retirements(db, present_instance_ids={"i1"})

    assert records.get(db, "m1").instances[0].retirement_status is RetirementStatus.ACTIVE


def test_reconcile_un_retires_a_confirmed_retirement_that_reappears(db):
    """A reappearing instance un-retires and reconnects to its history."""
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.confirm_retirement(db, ["i1"])

    records.reconcile_retirements(db, present_instance_ids={"i1"})

    assert records.get(db, "m1").instances[0].retirement_status is RetirementStatus.ACTIVE


def test_reconcile_leaves_an_already_pending_absent_instance_alone(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.reconcile_retirements(db, present_instance_ids=set())

    flagged_again = records.reconcile_retirements(db, present_instance_ids=set())

    assert flagged_again == []  # reported once, not on every sync
    assert records.get(db, "m1").instances[0].retirement_status is RetirementStatus.PENDING


def test_reconcile_never_deletes(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.reconcile_retirements(db, present_instance_ids=set())
    assert records.get(db, "m1") is not None


# --- Writes and validation -------------------------------------------------


def test_set_playable_toggles_the_flag(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.set_playable(db, "i1", False)
    assert records.get(db, "m1").instances[0].is_playable is False
    records.set_playable(db, "i1", True)
    assert records.get(db, "m1").instances[0].is_playable is True


def test_set_playable_raises_on_an_unknown_instance(db):
    with pytest.raises(records.UnknownInstance):
        records.set_playable(db, "nope", False)


def test_confirm_retirement_raises_on_an_unknown_instance(db):
    with pytest.raises(records.UnknownInstance):
        records.confirm_retirement(db, ["nope"])


def test_confirm_retirement_excludes_the_instance_from_recommendations(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    records.reconcile_retirements(db, present_instance_ids=set())
    assert [r.id for r in records.recommendable(db)] == ["m1"]  # pending still counts

    records.confirm_retirement(db, ["i1"])

    assert records.recommendable(db) == []


# --- The _Mapper boundary --------------------------------------------------


def test_no_orm_row_type_is_reachable_from_outside_the_module():
    """The pattern every other component copies: ORM rows never leave here.

    Anything public on `records` must not be a mapped class, so no caller can
    hold a row, lazy-load off it, or bind itself to the storage shape.
    """
    leaked = [
        name
        for name in dir(records)
        if not name.startswith("_")
        and isinstance(getattr(records, name), type)
        and issubclass(getattr(records, name), DeclarativeBase)
    ]
    assert leaked == []


def test_public_functions_return_only_dataclasses(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))

    got = records.get(db, "m1")
    assert dataclasses.is_dataclass(got)
    assert all(dataclasses.is_dataclass(i) for i in got.instances)
    assert all(dataclasses.is_dataclass(r) for r in records.browse(db))
    assert all(dataclasses.is_dataclass(r) for r in records.recommendable(db))
    assert all(
        dataclasses.is_dataclass(i)
        for i in records.reconcile_retirements(db, present_instance_ids=set())
    )


def test_domain_models_are_frozen(db):
    records.upsert(db, release("m1", instances=[instance("i1")]))
    got = records.get(db, "m1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.artist = "mutated"
    with pytest.raises(dataclasses.FrozenInstanceError):
        got.instances[0].is_playable = False


def test_the_instance_dataclass_carries_no_aggregate_foreign_key():
    """An Instance is always reached through its Release, so the FK is ORM-only."""
    assert "release_id" not in {f.name for f in dataclasses.fields(Instance)}


# --- Owned copies vs the whole aggregate -----------------------------------


class TestOwnedInstances:
    """A confirmed retirement is gone from the shelf, so no screen may offer it.

    `instances` stays complete, because a sold copy's play history still counts
    `owned_instances` is what a choice-of-copy control renders.
    """

    def test_owned_instances_drops_a_retired_copy(self):
        rel = release(
            "m1",
            instances=(
                instance("gone", status=RetirementStatus.RETIRED),
                instance("here"),
            ),
        )

        assert [i.id for i in rel.owned_instances] == ["here"]
        assert len(rel.instances) == 2  # the aggregate is untouched

    def test_owned_instances_keeps_a_not_playable_copy(self):
        # Damaged still means owned, and still loggable.
        rel = release("m1", instances=(instance("warped", playable=False),))

        assert [i.id for i in rel.owned_instances] == ["warped"]

    def test_owned_instances_keeps_a_pending_copy(self):
        # Excluded only once the user confirms it.
        rel = release("m1", instances=(instance("maybe", status=RetirementStatus.PENDING),))

        assert [i.id for i in rel.owned_instances] == ["maybe"]


def test_cover_url_survives_a_moved_data_dir(db, data_dir, monkeypatch, tmp_path):
    """`cover_path` holds an absolute path built from DATA_DIR at sync time.

    Trusting that column meant every cover silently vanished the day the volume
    moved. The URL is derived from the release id instead.
    """
    records.upsert(db, release("m1", cover="https://img.example/a.jpg"))
    db.flush()
    records.cover_file("m1").write_bytes(b"IMG")
    assert records.get(db, "m1").cover_url == "/covers/m1.jpg"

    moved = tmp_path / "elsewhere"
    (moved / "covers").mkdir(parents=True)
    monkeypatch.setenv("DATA_DIR", str(moved))
    records.cover_file("m1").write_bytes(b"IMG")  # same bytes, new volume

    assert records.get(db, "m1").cover_url == "/covers/m1.jpg"
