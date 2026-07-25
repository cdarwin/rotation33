"""Tests for the facade, over a real temporary SQLite database.

The ORM is never mocked. Everything below goes through the public functions of
`records`, `sessions` and `moods` to build state, so a test failing here means
the wiring is wrong rather than that a fake drifted from the real thing.

`rng` is a seeded `random.Random` throughout. The draw is weighted, so an
unseeded run would make every assertion about *which* releases came back a coin
flip; seeding turns the facade into a deterministic function of its inputs.
"""

from __future__ import annotations

import dataclasses
import random
from datetime import datetime, timedelta

import pytest

import moods
import recommendations
import records
import sessions
from recommendations import EmptyReason, RecommendationResult

NOW = datetime(2026, 7, 19, 14, 0)


def rng():
    """A fresh seeded source, so tests do not consume each other's randomness."""
    return random.Random(20260719)


def release(db, rid, styles=("Ambient",), playable=True, artist=None, title=None):
    """Persist one release owning a single playable pressing."""
    records.upsert(
        db,
        records.Release(
            id=rid,
            artist=artist or f"Artist {rid}",
            title=title or f"Title {rid}",
            styles=list(styles),
            cover_url=None,
            year=1979,
            instances=[
                records.Instance(
                    id=f"i-{rid}",
                    is_playable=playable,
                    retirement_status=records.RetirementStatus.ACTIVE,
                    pressing_release_id=int(rid[1:]),
                )
            ],
        ),
    )
    db.flush()
    return rid


def collection(db, n, styles=("Ambient",)):
    return [release(db, f"m{100 + i}", styles=styles) for i in range(n)]


def a_session(db, mood=moods.HEADS_DOWN):
    return sessions.start(db, mood, NOW).id


def played(db, session_id, rid, when):
    sessions.log_play(db, session_id, f"i-{rid}", rid, when)
    db.flush()


# --- Generating ------------------------------------------------------------


def test_generate_produces_up_to_five(db):
    collection(db, 12)
    result = recommendations.generate(db, a_session(db), NOW, rng())

    assert len(result.releases) == 5
    assert result.reason is None
    assert len({r.id for r in result.releases}) == 5


def test_generate_returns_fully_rendered_releases(db):
    """The facade already depends on `records`, so it does not hand back bare ids."""
    collection(db, 3)
    result = recommendations.generate(db, a_session(db), NOW, rng())

    assert all(isinstance(r, records.Release) for r in result.releases)
    assert all(r.artist and r.title for r in result.releases)


def test_generate_excludes_non_recommendable_releases(db):
    """Anything `records.recommendable` filters out never reaches the pool."""
    collection(db, 3)
    release(db, "m900", playable=False)
    result = recommendations.generate(db, a_session(db), NOW, rng())

    assert "m900" not in {r.id for r in result.releases}


# --- A regenerate repeats nothing ------------------------------------------


def test_regenerate_excludes_releases_already_shown(db):
    collection(db, 12)
    sid = a_session(db)

    first = recommendations.generate(db, sid, NOW, rng())
    second = recommendations.generate(db, sid, NOW + timedelta(minutes=5), rng())

    assert second.releases
    assert not {r.id for r in first.releases} & {r.id for r in second.releases}


def test_regenerate_excludes_releases_already_played_this_session(db):
    collection(db, 12)
    sid = a_session(db)

    first = recommendations.generate(db, sid, NOW, rng())
    played(db, sid, first.releases[0].id, NOW + timedelta(minutes=1))

    second = recommendations.generate(db, sid, NOW + timedelta(minutes=5), rng())
    assert first.releases[0].id not in {r.id for r in second.releases}


def test_regenerating_until_exhaustion_ends_in_session_exhausted(db):
    """The shown-set grows monotonically, so repeated regenerates must terminate.

    Nothing is ever played here, so the reason must not blame recency."""
    collection(db, 7)
    sid = a_session(db)

    seen: set[str] = set()
    for _ in range(3):
        result = recommendations.generate(db, sid, NOW, rng())
        assert not (seen & {r.id for r in result.releases})
        seen |= {r.id for r in result.releases}
        if not result.releases:
            break

    assert seen == {f"m{100 + i}" for i in range(7)}
    assert recommendations.generate(db, sid, NOW, rng()).reason is EmptyReason.SESSION_EXHAUSTED


def test_exclusion_is_scoped_to_its_own_session(db):
    """A new sitting starts clean: the exclusions are per-session."""
    collection(db, 12)
    first_sid = a_session(db)
    shown = recommendations.generate(db, first_sid, NOW, rng()).releases

    second_sid = sessions.start(db, moods.HEADS_DOWN, NOW + timedelta(hours=2)).id
    db.flush()
    again = recommendations.generate(db, second_sid, NOW + timedelta(hours=2), rng())

    # Nothing was *played*, so the recency window does not exclude them either.
    assert {r.id for r in again.releases} & {r.id for r in shown}


# --- The recency window ----------------------------------------------------


def test_recency_window_excludes_a_play_just_inside_it(db):
    """2d23h ago is inside a 3-day window, so the release is out."""
    collection(db, 4)
    old_sid = a_session(db)
    played(db, old_sid, "m100", NOW - timedelta(days=2, hours=23))

    sid = sessions.start(db, moods.HEADS_DOWN, NOW).id
    db.flush()
    result = recommendations.generate(db, sid, NOW, rng())

    assert "m100" not in {r.id for r in result.releases}


def test_recency_window_admits_a_play_just_outside_it(db):
    """3d1h ago is outside a 3-day window, so the release is eligible again."""
    collection(db, 4)
    old_sid = a_session(db)
    played(db, old_sid, "m100", NOW - timedelta(days=3, hours=1))

    sid = sessions.start(db, moods.HEADS_DOWN, NOW).id
    db.flush()
    result = recommendations.generate(db, sid, NOW, rng())

    assert "m100" in {r.id for r in result.releases}


def test_recency_window_follows_the_persisted_setting(db):
    collection(db, 4)
    old_sid = a_session(db)
    played(db, old_sid, "m100", NOW - timedelta(days=5))
    recommendations.set_window(db, 10)

    sid = sessions.start(db, moods.HEADS_DOWN, NOW).id
    db.flush()
    result = recommendations.generate(db, sid, NOW, rng())

    assert "m100" not in {r.id for r in result.releases}


def test_a_never_played_release_is_never_excluded_by_recency(db):
    """The `timedelta.max` sentinel must rank, never participate in arithmetic."""
    collection(db, 5)
    result = recommendations.generate(db, a_session(db), NOW, rng())
    assert len(result.releases) == 5


# --- Draw order round trip -------------------------------------------------


def test_draw_order_survives_the_persist_and_rehydrate_round_trip(db):
    collection(db, 12)
    sid = a_session(db)

    drawn = recommendations.generate(db, sid, NOW, rng())
    rehydrated = recommendations.active(db, sid)

    assert [r.id for r in rehydrated.releases] == [r.id for r in drawn.releases]
    # And not merely equal as a set: the order has to be the drawn one, which is
    # not the collection's artist/title order.
    assert [r.id for r in drawn.releases] != sorted(r.id for r in drawn.releases)


def test_active_reads_the_latest_batch_only(db):
    collection(db, 12)
    sid = a_session(db)

    recommendations.generate(db, sid, NOW, rng())
    second = recommendations.generate(db, sid, NOW + timedelta(minutes=5), rng())

    assert [r.id for r in recommendations.active(db, sid).releases] == [
        r.id for r in second.releases
    ]


def test_active_is_empty_before_anything_is_generated(db):
    collection(db, 4)
    result = recommendations.active(db, a_session(db))

    assert result.releases == []
    # "Nothing generated yet" is not the explained-empty state.
    assert result.reason is None


def test_active_drops_a_release_that_vanished_since_it_was_drawn(db):
    """The one place a persisted pick can silently disappear, and it is intended."""
    collection(db, 6)
    sid = a_session(db)
    drawn = recommendations.generate(db, sid, NOW, rng())
    gone = drawn.releases[0].id

    # Removed outright, not retired: `records.get` still returns a retired
    # release, so only a genuine disappearance exercises the drop.
    db.execute(
        records._InstanceRow.__table__.delete().where(records._InstanceRow.release_id == gone)
    )
    db.execute(records._ReleaseRow.__table__.delete().where(records._ReleaseRow.id == gone))
    db.flush()
    db.expunge_all()

    rehydrated = recommendations.active(db, sid)
    assert gone not in {r.id for r in rehydrated.releases}
    assert len(rehydrated.releases) == len(drawn.releases) - 1
    # Order among the survivors is untouched by the drop.
    assert [r.id for r in rehydrated.releases] == [r.id for r in drawn.releases[1:]]


# --- Thin pools ------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 2])
def test_a_thin_pool_yields_fewer_picks_with_no_reason(db, size):
    """Three picks is not a floor: show what qualifies."""
    collection(db, size)
    result = recommendations.generate(db, a_session(db), NOW, rng())

    assert len(result.releases) == size
    assert result.reason is None


# --- The empty reasons -----------------------------------------------------


def test_nothing_available_when_the_collection_is_empty(db):
    result = recommendations.generate(db, a_session(db), NOW, rng())

    assert result.releases == []
    assert result.reason is EmptyReason.NOTHING_AVAILABLE


def test_nothing_available_when_every_release_is_unplayable(db):
    """A non-empty collection can still produce an empty *pool*."""
    release(db, "m100", playable=False)
    release(db, "m101", playable=False)
    result = recommendations.generate(db, a_session(db), NOW, rng())

    assert result.reason is EmptyReason.NOTHING_AVAILABLE


def test_no_fit_when_records_exist_but_none_suit_the_mood(db):
    """Mapped styles that carry zero weight for this mood: present, but no fit.

    Deliberately not an unmapped style, which is eligible everywhere and
    which would therefore land in ALL_RECENT instead.
    """
    collection(db, 4, styles=("Funk", "Disco"))  # both mapped, both Peak-only
    result = recommendations.generate(db, a_session(db, moods.HEADS_DOWN), NOW, rng())

    assert result.releases == []
    assert result.reason is EmptyReason.NO_FIT


def test_all_recent_when_everything_that_fits_played_recently(db):
    collection(db, 4)  # Ambient: fits Heads Down
    old_sid = a_session(db)
    for i in range(4):
        played(db, old_sid, f"m{100 + i}", NOW - timedelta(hours=1))

    sid = sessions.start(db, moods.HEADS_DOWN, NOW).id
    db.flush()
    result = recommendations.generate(db, sid, NOW, rng())

    assert result.releases == []
    assert result.reason is EmptyReason.ALL_RECENT


def test_shown_this_session_is_not_reported_as_played_recently(db):
    """The exclusion source differs, and so does the explanation.

    Nothing has been played, so "everything was played recently" would be a
    false sentence and would point the user at the recency window, which is not
    the setting that would help.
    """
    collection(db, 3)
    sid = a_session(db)
    recommendations.generate(db, sid, NOW, rng())

    result = recommendations.generate(db, sid, NOW, rng())
    assert result.reason is EmptyReason.SESSION_EXHAUSTED


def test_the_three_reasons_are_distinguished_not_conflated(db):
    """The same session id, three deliberately different states, three answers.

    The reason ladder is order-dependent (pool, then fit, then everything else),
    so asserting each in isolation would not catch a mis-ordered ladder.
    """
    sid = a_session(db, moods.HEADS_DOWN)
    assert recommendations.generate(db, sid, NOW, rng()).reason is EmptyReason.NOTHING_AVAILABLE

    release(db, "m200", styles=("Funk",))
    assert recommendations.generate(db, sid, NOW, rng()).reason is EmptyReason.NO_FIT

    release(db, "m201", styles=("Ambient",))
    played(db, sid, "m201", NOW - timedelta(hours=1))
    assert recommendations.generate(db, sid, NOW, rng()).reason is EmptyReason.ALL_RECENT


# --- Faults raise rather than returning empty ------------------------------


def test_unknown_session_raises(db):
    collection(db, 4)
    with pytest.raises(sessions.UnknownSession):
        recommendations.generate(db, "no-such-session", NOW, rng())


def test_unknown_mood_raises(db):
    """`sessions.start` cannot validate the mood (it imports no components), so a
    bad name survives to here. It is a fault, not an empty state: rendering
    a typo as "nothing fits this mood" would hide the bug. Validating at the POST
    boundary is the view layer's job."""
    collection(db, 4)
    sid = sessions.start(db, "Brunch", NOW).id
    db.flush()

    with pytest.raises(moods.UnknownMood):
        recommendations.generate(db, sid, NOW, rng())


def test_active_on_an_unknown_session_is_simply_empty(db):
    """`active` is a read of "what is showing", so it has nothing to fault on."""
    assert recommendations.active(db, "no-such-session").releases == []


# --- The recency window setting --------------------------------------------


def test_window_defaults_to_three_days(db):
    assert recommendations.window(db) == timedelta(days=3)


def test_set_window_persists(db):
    recommendations.set_window(db, 10)
    assert recommendations.window(db) == timedelta(days=10)

    recommendations.set_window(db, 1)
    assert recommendations.window(db) == timedelta(days=1)


def test_window_of_zero_is_allowed_and_excludes_nothing_by_recency(db):
    """Zero means cross-session immediate repeats become possible."""
    recommendations.set_window(db, 0)
    assert recommendations.window(db) == timedelta(0)

    collection(db, 4)
    old_sid = a_session(db)
    played(db, old_sid, "m100", NOW - timedelta(minutes=1))

    sid = sessions.start(db, moods.HEADS_DOWN, NOW).id
    db.flush()
    assert "m100" in {r.id for r in recommendations.generate(db, sid, NOW, rng()).releases}


@pytest.mark.parametrize("bad", [-1, "3", 3.5, None, True])
def test_set_window_rejects_a_nonsensical_value(db, bad):
    with pytest.raises(recommendations.InvalidWindow):
        recommendations.set_window(db, bad)


# --- Nothing commits -------------------------------------------------------


def test_nothing_commits(db):
    """The caller owns the transaction: a rollback must undo the whole batch."""
    collection(db, 6)
    db.commit()
    sid = a_session(db)
    db.commit()

    recommendations.generate(db, sid, NOW, rng())
    db.rollback()

    assert recommendations.active(db, sid).releases == []


# --- The _Mapper boundary --------------------------------------------------


def test_public_functions_return_only_dataclasses(db):
    collection(db, 4)
    sid = a_session(db)

    result = recommendations.generate(db, sid, NOW, rng())
    assert dataclasses.is_dataclass(result)
    assert all(dataclasses.is_dataclass(r) for r in result.releases)
    assert all(dataclasses.is_dataclass(r) for r in recommendations.active(db, sid).releases)


def test_the_result_is_frozen(db):
    result = RecommendationResult(releases=[], reason=EmptyReason.NO_FIT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.reason = None


def test_result_fields_are_exactly_the_documented_two(db):
    assert {f.name for f in dataclasses.fields(RecommendationResult)} == {"releases", "reason"}


def test_orm_rows_never_leave_the_module(db):
    """Every public return is a dataclass, so no ORM row can escape by accident."""
    collection(db, 4)
    sid = a_session(db)
    for value in (
        recommendations.generate(db, sid, NOW, rng()),
        recommendations.active(db, sid),
    ):
        assert not isinstance(value, recommendations._RecommendationRow)
        for r in value.releases:
            assert not hasattr(r, "__mapper__")


# --- Keeping pinned picks across a regenerate (session workspace) -----------


class TestKeep:
    """generate(keep=...) carries chosen releases into the new batch and refills
    the rest, without duplicating or resurrecting a pick that left the pool."""

    def test_pinned_picks_survive_and_lead_the_batch(self, db):
        collection(db, 8)
        sid = a_session(db)
        first = recommendations.generate(db, sid, NOW, rng=rng())
        assert len(first.releases) == recommendations.COUNT

        keep = [first.releases[0].id, first.releases[1].id]
        second = recommendations.generate(db, sid, NOW, keep=keep, rng=rng())
        ids = [r.id for r in second.releases]

        assert ids[:2] == keep  # kept, in the order passed
        assert len(second.releases) == recommendations.COUNT  # refilled to five

    def test_unpinned_slots_are_replaced_with_fresh_picks(self, db):
        collection(db, 8)
        sid = a_session(db)
        first = recommendations.generate(db, sid, NOW, rng=rng())
        keep = [first.releases[0].id]

        second = recommendations.generate(db, sid, NOW, keep=keep, rng=rng())
        replaced = [r.id for r in second.releases[1:]]

        # The refilled slots are none of the previously-shown picks (exclusion still
        # excludes shown), so they are genuinely new.
        assert set(replaced).isdisjoint({r.id for r in first.releases})

    def test_keep_never_duplicates(self, db):
        collection(db, 8)
        sid = a_session(db)
        first = recommendations.generate(db, sid, NOW, rng=rng())
        keep = [first.releases[0].id, first.releases[1].id]

        second = recommendations.generate(db, sid, NOW, keep=keep, rng=rng())
        ids = [r.id for r in second.releases]
        assert len(ids) == len(set(ids))

    def test_a_kept_release_that_left_the_pool_is_dropped(self, db):
        collection(db, 8)
        sid = a_session(db)
        first = recommendations.generate(db, sid, NOW, rng=rng())
        gone = first.releases[0].id

        records.set_playable(db, f"i-{gone}", False)  # no longer recommendable
        db.flush()

        second = recommendations.generate(db, sid, NOW, keep=[gone], rng=rng())
        assert gone not in {r.id for r in second.releases}

    def test_empty_keep_matches_a_plain_regenerate(self, db):
        collection(db, 10)
        sid = a_session(db)
        first = recommendations.generate(db, sid, NOW, rng=rng())

        second = recommendations.generate(db, sid, NOW, keep=[], rng=rng())
        # With no pins, a regenerate is exactly today's behaviour: it excludes
        # everything already shown, so the two batches are disjoint.
        assert {r.id for r in second.releases}.isdisjoint({r.id for r in first.releases})


# --- An empty batch is a batch ---------------------------------------------


def test_an_empty_generate_does_not_resurrect_the_previous_batch(db):
    """An empty generate used to write no rows at all.

    `active` then found the *previous* batch and showed picks the user had
    already rejected, with the explanation gone. A page reload after an
    empty regenerate must show the explanation, not stale picks.
    """
    collection(db, 3)
    sid = a_session(db)
    first = recommendations.generate(db, sid, NOW, rng())
    assert first.releases

    empty = recommendations.generate(db, sid, NOW + timedelta(minutes=1), rng())
    assert empty.releases == []

    after = recommendations.active(db, sid)
    assert after.releases == []
    assert after.reason is empty.reason


def test_active_carries_the_reason_that_generate_persisted(db):
    """The "why" has to survive the round trip, or nothing renders it."""
    sid = a_session(db, moods.HEADS_DOWN)

    generated = recommendations.generate(db, sid, NOW, rng())
    assert generated.reason is EmptyReason.NOTHING_AVAILABLE

    assert recommendations.active(db, sid).reason is EmptyReason.NOTHING_AVAILABLE


def test_active_has_no_reason_before_anything_is_generated(db):
    """ "Nothing generated yet" is not "nothing qualifies"."""
    sid = a_session(db)

    result = recommendations.active(db, sid)

    assert result.releases == []
    assert result.reason is None


def test_a_marker_row_does_not_count_as_a_release_shown(db):
    """An empty batch showed the user nothing, so it used nothing up."""
    collection(db, 3)
    sid = a_session(db)
    recommendations.generate(db, sid, NOW, rng())  # shows all 3
    recommendations.generate(db, sid, NOW + timedelta(minutes=1), rng())  # empty marker

    # A second session sees the whole collection: the marker is not an exclusion.
    other = sessions.start(db, moods.HEADS_DOWN, NOW + timedelta(hours=1)).id
    assert len(recommendations.generate(db, other, NOW + timedelta(hours=1), rng()).releases) == 3


# --- active() drops picks that stopped qualifying ---------------------------


def test_active_drops_a_pick_whose_copies_were_all_retired(db):
    """`records.get` answers "does this exist", not "may it be recommended".

    Confirming a sale left the sold record sitting in the picks until the next
    regenerate.
    """
    collection(db, 3)
    sid = a_session(db)
    shown = recommendations.generate(db, sid, NOW, rng()).releases
    victim = shown[0]

    records.confirm_retirement(db, [i.id for i in victim.instances])
    db.flush()

    remaining = {r.id for r in recommendations.active(db, sid).releases}
    assert victim.id not in remaining
    assert len(remaining) == len(shown) - 1


def test_active_drops_a_pick_marked_not_playable(db):
    collection(db, 3)
    sid = a_session(db)
    shown = recommendations.generate(db, sid, NOW, rng()).releases
    victim = shown[0]

    for inst in victim.instances:
        records.set_playable(db, inst.id, False)
    db.flush()

    assert victim.id not in {r.id for r in recommendations.active(db, sid).releases}
