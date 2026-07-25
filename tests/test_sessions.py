"""Phase 3: sittings, plays, and release-level recency.

The assertions that matter here are `latest_plays` being one entry per release
(the facade's single recency read), "current session only" removal immediately
shrinking that set, a retired instance's plays still counting,
and the independence from `records` that the denormalized `release_id` buys.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import DeclarativeBase

import records
import sessions
from sessions import NotCurrentSession, Play, Session, UnknownPlay, UnknownSession

REPO_ROOT = Path(__file__).resolve().parent.parent

T0 = datetime(2026, 7, 19, 20, 0)


def at(**delta) -> datetime:
    return T0 + timedelta(**delta)


# --- Sessions --------------------------------------------------------------


def test_current_is_none_only_on_a_virgin_database(db):
    assert sessions.current(db) is None


def test_start_creates_a_session_and_makes_it_current(db):
    started = sessions.start(db, "Peak", T0)
    assert started.mood == "Peak"
    assert started.started_at == T0
    assert sessions.current(db) == started


def test_get_returns_a_session_by_id(db):
    started = sessions.start(db, "Peak", T0)
    assert sessions.get(db, started.id) == started


def test_get_finds_a_session_that_is_no_longer_current(db):
    """The whole reason `get` exists: the facade is keyed by id, not by "latest"."""
    older = sessions.start(db, "Peak", T0)
    sessions.start(db, "After Dark", at(hours=2))

    assert sessions.current(db).id != older.id
    assert sessions.get(db, older.id) == older


def test_get_raises_on_an_unknown_id(db):
    with pytest.raises(sessions.UnknownSession):
        sessions.get(db, "no-such-session")


def test_start_always_creates_a_new_session(db):
    first = sessions.start(db, "Peak", T0)
    second = sessions.start(db, "Peak", at(minutes=1))
    assert second.id != first.id


def test_starting_a_session_makes_the_prior_one_stop_being_current(db):
    first = sessions.start(db, "Peak", T0)
    second = sessions.start(db, "After Dark", at(hours=2))
    assert sessions.current(db) == second
    assert sessions.current(db).id != first.id


def test_a_prior_session_is_not_deleted_only_superseded(db):
    first = sessions.start(db, "Peak", T0)
    sessions.start(db, "After Dark", at(hours=2))
    sessions.log_play(db, first.id, "i1", "m1", T0)
    # Its log is still readable even though it is no longer current.
    assert len(sessions.plays(db, first.id)) == 1


def test_a_long_idle_session_stays_current(db):
    """There is no midnight rollover: a session ends when the next starts (RFC 6)."""
    started = sessions.start(db, "Peak", T0)
    assert sessions.current(db) == started
    assert sessions.current(db).started_at == T0


def test_session_ids_are_uuid4_hex(db):
    sid = sessions.start(db, "Peak", T0).id
    assert len(sid) == 32
    assert int(sid, 16) >= 0  # hex, no dashes


# --- Plays -----------------------------------------------------------------


@pytest.fixture
def session(db):
    return sessions.start(db, "Peak", T0)


def test_log_play_records_both_ids(db, session):
    play = sessions.log_play(db, session.id, "i1", "m1", at(minutes=5))
    assert (play.session_id, play.instance_id, play.release_id) == (session.id, "i1", "m1")
    assert play.played_at == at(minutes=5)
    assert len(play.id) == 32


def test_log_play_raises_on_an_unknown_session(db):
    with pytest.raises(UnknownSession):
        sessions.log_play(db, "nope", "i1", "m1", T0)


def test_plays_returns_this_sessions_log_oldest_first(db, session):
    sessions.log_play(db, session.id, "i2", "m2", at(minutes=10))
    sessions.log_play(db, session.id, "i1", "m1", at(minutes=5))
    assert [p.release_id for p in sessions.plays(db, session.id)] == ["m1", "m2"]


def test_plays_is_scoped_to_one_session(db, session):
    other = sessions.start(db, "After Dark", at(hours=3))
    sessions.log_play(db, session.id, "i1", "m1", at(minutes=5))
    sessions.log_play(db, other.id, "i2", "m2", at(hours=3, minutes=5))
    assert [p.release_id for p in sessions.plays(db, other.id)] == ["m2"]


def test_plays_is_empty_for_a_session_with_nothing_logged(db, session):
    assert sessions.plays(db, session.id) == []


# --- latest_plays: the single recency read ---------------------------------


def test_latest_plays_is_empty_on_a_virgin_database(db):
    assert sessions.latest_plays(db) == {}


def test_latest_plays_returns_one_entry_per_release_the_most_recent(db, session):
    sessions.log_play(db, session.id, "i1", "m1", at(days=-5))
    sessions.log_play(db, session.id, "i1", "m1", at(days=-1))
    sessions.log_play(db, session.id, "i2", "m2", at(days=-3))
    assert sessions.latest_plays(db) == {"m1": at(days=-1), "m2": at(days=-3)}


def test_latest_plays_is_release_level_across_different_pressings(db, session):
    """No album recurs through a different copy: recency keys on the release."""
    sessions.log_play(db, session.id, "i1", "m1", at(days=-5))
    sessions.log_play(db, session.id, "i2", "m1", at(days=-1))  # other pressing, same album
    assert sessions.latest_plays(db) == {"m1": at(days=-1)}


def test_latest_plays_spans_every_session_ever(db):
    old = sessions.start(db, "Peak", at(days=-10))
    new = sessions.start(db, "After Dark", T0)
    sessions.log_play(db, old.id, "i1", "m1", at(days=-10))
    sessions.log_play(db, new.id, "i2", "m2", T0)
    assert sessions.latest_plays(db) == {"m1": at(days=-10), "m2": T0}


def test_a_retired_instances_plays_still_contribute_to_release_recency(db, session):
    """A sold copy keeps its play history, and the album stays "recently played".

    Set up through `records` so the retirement is real, then assert `sessions`
    answers the recency question without consulting it at all. This is exactly
    what the denormalized `release_id` is for.
    """
    records.upsert(
        db,
        records.Release(
            id="m1",
            artist="Zappa",
            title="Apostrophe",
            styles=["Prog Rock"],
            cover_url=None,
            year=1974,
            instances=[
                records.Instance(
                    id="i1",
                    is_playable=True,
                    retirement_status=records.RetirementStatus.ACTIVE,
                    pressing_release_id=99,
                )
            ],
        ),
    )
    sessions.log_play(db, session.id, "i1", "m1", at(days=-1))

    records.reconcile_retirements(db, present_instance_ids=set())
    records.confirm_retirement(db, ["i1"])
    db.flush()

    assert records.recommendable(db) == []  # the copy is gone from suggestions
    assert sessions.latest_plays(db) == {"m1": at(days=-1)}  # the recency is not


# --- remove_play -----------------------------------------------------------


def test_remove_play_deletes_it_from_the_session_log(db, session):
    play = sessions.log_play(db, session.id, "i1", "m1", at(minutes=5))
    sessions.remove_play(db, play.id)
    assert sessions.plays(db, session.id) == []


def test_remove_play_immediately_shrinks_latest_plays(db, session):
    """Eligibility is restored with no special case: recency is derived, not stored."""
    play = sessions.log_play(db, session.id, "i1", "m1", at(minutes=5))
    assert "m1" in sessions.latest_plays(db)
    sessions.remove_play(db, play.id)
    assert sessions.latest_plays(db) == {}


def test_remove_play_falls_back_to_the_previous_play_of_the_same_release(db, session):
    old = sessions.log_play(db, session.id, "i1", "m1", at(days=-5))
    new = sessions.log_play(db, session.id, "i1", "m1", at(minutes=5))
    sessions.remove_play(db, new.id)
    assert sessions.latest_plays(db) == {"m1": old.played_at}


def test_remove_play_refuses_a_play_from_a_non_current_session(db):
    """Only the active session is editable; earlier history is not."""
    old = sessions.start(db, "Peak", T0)
    play = sessions.log_play(db, old.id, "i1", "m1", at(minutes=5))
    sessions.start(db, "After Dark", at(hours=3))  # old is no longer current

    with pytest.raises(NotCurrentSession):
        sessions.remove_play(db, play.id)
    assert sessions.plays(db, old.id) == [play]
    assert sessions.latest_plays(db) == {"m1": at(minutes=5)}


def test_remove_play_raises_on_an_unknown_play(db, session):
    with pytest.raises(UnknownPlay):
        sessions.remove_play(db, "nope")


# --- Independence and the _Mapper boundary ---------------------------------


def test_sessions_does_not_import_records():
    """The denormalized release_id only earns its keep if this stays true (RFC 5.3)."""
    source = (REPO_ROOT / "sessions.py").read_text(encoding="utf-8")
    for component in ("records", "moods", "picker", "recommendations"):
        assert f"import {component}" not in source
    assert "records" not in dir(sessions)


def test_no_orm_row_type_is_reachable_from_outside_the_module():
    leaked = [
        name
        for name in dir(sessions)
        if not name.startswith("_")
        and isinstance(getattr(sessions, name), type)
        and issubclass(getattr(sessions, name), DeclarativeBase)
    ]
    assert leaked == []


def test_public_functions_return_only_dataclasses(db):
    started = sessions.start(db, "Peak", T0)
    assert dataclasses.is_dataclass(started)
    assert dataclasses.is_dataclass(sessions.current(db))
    assert dataclasses.is_dataclass(sessions.log_play(db, started.id, "i1", "m1", T0))
    assert all(dataclasses.is_dataclass(p) for p in sessions.plays(db, started.id))


def test_domain_models_are_frozen(db):
    started = sessions.start(db, "Peak", T0)
    play = sessions.log_play(db, started.id, "i1", "m1", T0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        started.mood = "mutated"
    with pytest.raises(dataclasses.FrozenInstanceError):
        play.release_id = "mutated"


def test_domain_types_are_what_the_rfc_names():
    assert {f.name for f in dataclasses.fields(Session)} == {"id", "mood", "started_at"}
    assert {f.name for f in dataclasses.fields(Play)} == {
        "id",
        "session_id",
        "instance_id",
        "release_id",
        "played_at",
    }
