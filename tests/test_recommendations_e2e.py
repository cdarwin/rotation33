"""Phase 4's exit criterion, as a test: a real recommendation from a real collection.

`tools/e2e_demo.py` is the scripted run; this imports the same functions so the
demo cannot rot into a script nobody executes. Everything below goes through the
public surface of `records`, `sessions`, `moods` and `recommendations` over the
75-item captured fixture.

The assertions are deliberately structural rather than exact. Pinning the five
ids that come back would make this a change-detector for `picker`'s weighting
and for the fixture, neither of which this test is about: what it proves is that
the components compose into a non-empty, well-formed recommendation over real
data.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import e2e_demo  # noqa: E402

import moods  # noqa: E402
import recommendations  # noqa: E402
import sessions  # noqa: E402

NOW = datetime(2026, 7, 19, 10, 30)  # inside the Heads Down window


def test_the_fixture_loads_as_a_real_collection(db):
    total, _ = e2e_demo.run(db, moods.HEADS_DOWN, NOW)
    # 75 raw items, a few of them non-vinyl or extra pressings of one album.
    assert 50 < total < 75


def test_a_real_recommendation_comes_back_from_the_real_collection(db):
    """The milestone. A non-empty, well-formed batch over 70-odd real albums."""
    _, result = e2e_demo.run(db, moods.HEADS_DOWN, NOW)

    assert result.reason is None
    assert 1 <= len(result.releases) <= recommendations.COUNT
    assert len({r.id for r in result.releases}) == len(result.releases)
    for release in result.releases:
        assert release.artist and release.title
        assert release.id.startswith(("m", "r"))


@pytest.mark.parametrize("mood", [m.name for m in moods.MOODS])
def test_every_mood_finds_something_in_this_collection(db, mood):
    """A real collection should not leave any of the five moods empty-handed.

    If one ever does, that is a signal about the affinity map's coverage (FR-18)
    rather than about the facade, which is exactly why it is worth asserting.
    """
    _, result = e2e_demo.run(db, mood, NOW)
    assert result.releases, result.reason


def test_the_vinyl_filter_drops_the_non_vinyl_items(db):
    """The fixture carries a non-vinyl item on purpose; it must not become a record."""
    import json

    items = json.loads(e2e_demo.FIXTURE.read_text())["items"]
    assert any(not e2e_demo.is_vinyl(i) for i in items), "fixture lost its non-vinyl item"

    kept = [i for i in items if e2e_demo.is_vinyl(i)]
    assert len(kept) < len(items)


def test_a_multi_pressing_album_becomes_one_release_with_two_instances(db):
    """The `m<master_id>` grouping rule, over the real duplicate in the fixture."""
    e2e_demo.run(db, moods.HEADS_DOWN, NOW)

    import records

    multi = [r for r in records.browse(db) if len(r.instances) > 1]
    assert multi, "fixture lost its multi-pressing album"
    for release in multi:
        assert len({i.id for i in release.instances}) == len(release.instances)


def test_regenerating_over_the_real_collection_repeats_nothing(db):
    """FR-9 end to end, on real data rather than a synthetic pool."""
    total, first = e2e_demo.run(db, moods.HEADS_DOWN, NOW)
    session_id = sessions.current(db).id

    second = recommendations.generate(db, session_id, NOW + timedelta(minutes=5))

    assert second.releases
    assert not {r.id for r in first.releases} & {r.id for r in second.releases}


def test_the_batch_rehydrates_in_draw_order_over_the_real_collection(db):
    _, drawn = e2e_demo.run(db, moods.HEADS_DOWN, NOW)
    session_id = sessions.current(db).id

    rehydrated = recommendations.active(db, session_id)
    assert [r.id for r in rehydrated.releases] == [r.id for r in drawn.releases]
