"""Tests for the pure selection engine.

Three classes, matching the engine RFC's testing strategy: deterministic (seeded,
exact ids), invariant (shape, not values), and statistical (the only kind that
catches an inverted weight).

`matching` and `draw` are exercised independently of each other throughout. A
test that ran the two together would let a fit bug hide behind a draw bug.
"""

from __future__ import annotations

import ast
import collections
import random
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

import picker
from picker import Affinity, Candidate

NEVER_PLAYED = timedelta.max


def candidate(release_id, styles=("Ambient",), days=0):
    """A candidate with staleness expressed in days, for readable pools."""
    staleness = NEVER_PLAYED if days is None else timedelta(days=days)
    return Candidate(release_id=release_id, styles=styles, staleness=staleness)


def affinity(weights=None, mapped=None):
    """An Affinity whose mapped_styles defaults to exactly the weighted styles.

    Passing `mapped` explicitly is how a test builds the distinction: a
    style that is mapped-but-zero versus one that is absent from the map entirely.
    """
    weights = {"Ambient": 0.9} if weights is None else weights
    mapped = frozenset(weights) if mapped is None else frozenset(mapped)
    return Affinity(weights=weights, mapped_styles=mapped)


def pool_by_staleness(*day_offsets):
    """A pool of candidates all fitting the default mood, ids r0..rN."""
    return [candidate(f"r{i}", days=d) for i, d in enumerate(day_offsets)]


class TestDeterministic:
    """Seeded RNG, exact expected ids from a fixed pool."""

    def test_draw_is_reproducible_for_a_fixed_seed(self):
        pool = pool_by_staleness(1, 2, 3, 4, 5, 6, 7, 8)

        first = picker.draw(pool, 5, random.Random(20260719))
        second = picker.draw(pool, 5, random.Random(20260719))

        assert first == second
        assert len(first) == 5

    def test_draw_returns_the_expected_ids_for_a_known_seed(self):
        # A regression pin: any change to the weighting or the pop-and-redraw loop
        # moves these ids. Regenerated deliberately, never adjusted to fit a bug.
        # Last regenerated when `draw` gained a random() tie-break key, so that
        # equal staleness stops being ordered by the caller (see the tie tests).
        pool = pool_by_staleness(1, 2, 3, 4, 5, 6, 7, 8)

        assert picker.draw(pool, 3, random.Random(7)) == ["r7", "r5", "r6"]

    def test_two_different_seeds_can_produce_different_orders(self):
        # Guards against a "weighting" that has collapsed into a plain sort: with a
        # real weighted draw, some pair of seeds must disagree.
        pool = pool_by_staleness(*range(1, 11))

        orders = {tuple(picker.draw(pool, 5, random.Random(s))) for s in range(25)}

        assert len(orders) > 1

    def test_matching_preserves_input_order(self):
        pool = [candidate("a"), candidate("b"), candidate("c")]

        assert [c.release_id for c in picker.matching(pool, affinity())] == ["a", "b", "c"]


class TestInvariants:
    """Shape guarantees that must hold whatever the RNG does."""

    def test_draw_never_exceeds_count(self):
        pool = pool_by_staleness(*range(1, 21))

        for seed in range(50):
            assert len(picker.draw(pool, 5, random.Random(seed))) <= 5

    def test_draw_never_duplicates_a_release(self):
        pool = pool_by_staleness(*range(1, 21))

        for seed in range(50):
            drawn = picker.draw(pool, 5, random.Random(seed))
            assert len(drawn) == len(set(drawn))

    def test_draw_returns_empty_list_on_an_empty_pool(self):
        assert picker.draw([], 5, random.Random(1)) == []

    def test_draw_returns_fewer_than_count_on_a_thin_pool_without_raising(self):
        pool = pool_by_staleness(1, 2)

        drawn = picker.draw(pool, 5, random.Random(1))

        assert len(drawn) == 2
        assert set(drawn) == {"r0", "r1"}

    def test_draw_on_a_single_candidate_pool_returns_it(self):
        assert picker.draw([candidate("only", days=3)], 5, random.Random(1)) == ["only"]

    def test_draw_returns_only_ids_that_were_in_the_pool(self):
        pool = pool_by_staleness(*range(1, 11))
        ids = {c.release_id for c in pool}

        for seed in range(25):
            assert set(picker.draw(pool, 5, random.Random(seed))) <= ids

    def test_draw_handles_candidates_that_compare_equal(self):
        # Frozen dataclasses with identical fields compare equal. Removal must be
        # positional, or one round drops the wrong entry and the count goes wrong.
        pool = [candidate("same", days=5) for _ in range(4)]

        drawn = picker.draw(pool, 4, random.Random(3))

        assert len(drawn) == 4

    def test_draw_defaults_to_the_module_rng_when_none_is_passed(self):
        pool = pool_by_staleness(1, 2, 3, 4, 5)

        drawn = picker.draw(pool, 3)

        assert len(drawn) == 3
        assert len(set(drawn)) == 3

    def test_draw_returns_empty_for_a_non_positive_count(self):
        pool = pool_by_staleness(1, 2, 3)

        assert picker.draw(pool, 0, random.Random(1)) == []
        assert picker.draw(pool, -1, random.Random(1)) == []

    def test_draw_never_returns_a_candidate_that_failed_matching(self):
        # The two functions are still tested independently: `matching` decides the
        # pool, `draw` is then checked to add nothing back that `matching` removed.
        pool = [
            candidate("fits", styles=("Ambient",), days=1),
            candidate("zero", styles=("Thrash",), days=99),
            candidate("also-fits", styles=("Dub",), days=2),
        ]
        mood = affinity(weights={"Ambient": 0.8, "Dub": 0.4, "Thrash": 0.0})

        fit = picker.matching(pool, mood)
        drawn = picker.draw(fit, 5, random.Random(11))

        assert "zero" not in drawn
        assert set(drawn) == {"fits", "also-fits"}

    def test_matching_keeps_a_candidate_whose_only_style_is_unmapped(self):
        # Unmapped means eligible everywhere, even for a mood that weights
        # nothing it carries.
        mood = affinity(weights={"Ambient": 0.9}, mapped={"Ambient", "Dub"})
        unmapped = candidate("obscure", styles=("Hauntology",))

        assert picker.matching([unmapped], mood) == [unmapped]

    def test_matching_filters_out_a_candidate_whose_best_mapped_style_is_zero(self):
        # Mapped-but-zero is a deliberate 0, not an unknown. It must not survive.
        mood = affinity(weights={"Ambient": 0.9}, mapped={"Ambient", "Thrash", "Dub"})
        zero = candidate("wrong-mood", styles=("Thrash", "Dub"))

        assert picker.matching([zero], mood) == []

    def test_matching_uses_the_best_style_not_the_worst(self):
        # One unrelated tag alongside a great-fit tag must not disqualify.
        mood = affinity(weights={"Ambient": 0.9}, mapped={"Ambient", "Thrash"})
        mixed = candidate("mixed", styles=("Thrash", "Ambient"))

        assert picker.matching([mixed], mood) == [mixed]

    def test_matching_keeps_a_candidate_with_one_unmapped_style_among_zeros(self):
        mood = affinity(weights={"Ambient": 0.9}, mapped={"Ambient", "Thrash"})
        mixed = candidate("mixed", styles=("Thrash", "Hauntology"))

        assert picker.matching([mixed], mood) == [mixed]

    def test_matching_on_an_empty_pool_returns_empty(self):
        assert picker.matching([], affinity()) == []

    def test_matching_keeps_a_candidate_with_no_styles_at_all(self):
        # Unclassified, not disqualified. Dropping it would exclude the release
        # from every mood forever with nothing on screen to explain it, which is
        # the failure the unmapped rule exists to prevent. Real collections
        # contain these.
        bare = candidate("bare", styles=())

        assert picker.matching([bare], affinity()) == [bare]

    def test_a_styleless_candidate_fits_every_mood(self):
        bare = candidate("bare", styles=())
        picky = Affinity(weights={"Jazz": 1.0}, mapped_styles=frozenset({"Jazz", "Funk"}))
        indifferent = Affinity(weights={}, mapped_styles=frozenset())

        assert picker.matching([bare], picky) == [bare]
        assert picker.matching([bare], indifferent) == [bare]

    def test_matching_treats_every_style_as_eligible_when_the_map_is_empty(self):
        # Before any mood is configured, nothing is mapped, so nothing is excluded.
        empty = Affinity(weights={}, mapped_styles=frozenset())
        pool = [candidate("a"), candidate("b", styles=("Dub",))]

        assert picker.matching(pool, empty) == pool

    def test_draw_does_not_mutate_the_caller_pool(self):
        pool = pool_by_staleness(1, 2, 3, 4, 5)
        before = list(pool)

        picker.draw(pool, 3, random.Random(1))

        assert pool == before

    def test_never_played_sentinel_is_only_sorted_never_arithmetic(self):
        # timedelta.max is safe precisely because draw only ever compares it. If the
        # implementation started adding to it this pool would raise OverflowError.
        pool = [candidate("never", days=None), candidate("old", days=400)]

        assert picker.draw(pool, 2, random.Random(1))[0] in {"never", "old"}


class TestPurity:
    """The engine is swappable because it depends on nothing here."""

    def test_picker_imports_only_stdlib(self):
        source = Path(picker.__file__).read_text()
        tree = ast.parse(source)

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])

        assert imported <= sys.stdlib_module_names, (
            f"picker must import stdlib only; found {sorted(imported - sys.stdlib_module_names)}"
        )

    def test_picker_imports_no_project_module(self):
        project_modules = {
            path.stem for path in Path(picker.__file__).parent.glob("*.py") if path.stem != "picker"
        }
        source = Path(picker.__file__).read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "picker must not use a relative import"
                names = {node.module.split(".")[0]} if node.module else set()
            else:
                continue
            assert not (names & project_modules), f"picker imports project module {names}"


class TestStatistical:
    """Unseeded draws over a known staleness spread.

    This is the only test that catches an inverted weight: with the sort flipped,
    the never-played release becomes the *least* likely pick and the ratio inverts
    well below 1x. The deterministic and invariant tests all still pass under that
    bug, which is exactly why this class exists.
    """

    def test_never_played_beats_played_yesterday_by_more_than_2x(self):
        # The pool carries three filler releases between the two under test. That is
        # not padding: linear rank weights over a pool of N run from N down to 1, so
        # a bare two-candidate pool weights them exactly 2 and 1 and can never
        # exceed 2x however many draws are run. The engine RFC's testing section
        # offers ">2x on a two-release pool" while its algorithm section specifies
        # the linear rank weight that makes 2x the hard ceiling; the two do not fit
        # together. The algorithm wins, and the pool widens to give the assertion
        # room. At N=5 the never-played release carries weight 5 against
        # yesterday's 1, so the true ratio is 5x and >2x is a real signal.
        never = candidate("never-played", days=None)
        yesterday = candidate("played-yesterday", days=1)
        pool = [
            yesterday,
            candidate("f1", days=3),
            never,
            candidate("f2", days=9),
            candidate("f3", days=20),
        ]

        wins = Counter(picker.draw(pool, 1)[0] for _ in range(600))

        assert wins["never-played"] > 2 * wins["played-yesterday"], (
            f"staleness weighting looks inverted or flat: {dict(wins)}"
        )

    def test_the_stalest_of_a_wider_pool_is_drawn_first_most_often(self):
        # A second angle on the same signal: over a graded pool, first place should
        # go to the most stale release far more often than to the freshest.
        pool = pool_by_staleness(1, 10, 100, 1000)

        firsts = Counter(picker.draw(pool, 1)[0] for _ in range(600))

        assert firsts["r3"] > firsts["r0"]

    def test_draw_order_trends_from_stale_to_fresh(self):
        # Averaged over many unseeded draws, the release taken first should be
        # staler than the one taken last.
        pool = pool_by_staleness(1, 10, 100, 1000)
        rank = {"r0": 0, "r1": 1, "r2": 2, "r3": 3}

        runs = [picker.draw(pool, 4) for _ in range(400)]
        mean_first = sum(rank[r[0]] for r in runs) / len(runs)
        mean_last = sum(rank[r[-1]] for r in runs) / len(runs)

        assert mean_first > mean_last


class TestStalenessTiesAreNotCallerOrder:
    """Rank position is the draw weight, so a tie must not inherit input order.

    On a freshly synced collection every release is never-played and therefore
    tied, and `records.recommendable` hands them over sorted by artist. A stable
    sort turned that into a weighting: measured against the 72-release fixture,
    the first artist alphabetically was drawn 556 times to the last one's 8.
    """

    def test_tied_candidates_are_drawn_evenly_regardless_of_input_order(self):
        pool = [candidate(f"r{i}", days=None) for i in range(40)]  # all never-played
        rng = random.Random(11)

        counts = collections.Counter()
        for _ in range(4000):
            for rid in picker.draw(pool, 5, rng):
                counts[rid] += 1

        # Uniform expectation is 4000 * 5 / 40 = 500 each. Generous bounds: the
        # bug produced a ~250x spread between the ends, so anything within 2x of
        # even is a pass and the regression is still caught.
        assert min(counts.values()) > 250, counts
        assert max(counts.values()) < 1000, counts

    def test_a_real_staleness_spread_still_favours_the_stalest(self):
        # The shuffle must not flatten the actual ranking it exists to protect.
        pool = pool_by_staleness(*range(1, 21))
        rng = random.Random(13)

        counts = collections.Counter()
        for _ in range(4000):
            for rid in picker.draw(pool, 5, rng):
                counts[rid] += 1

        assert counts["r19"] > counts["r0"]  # 20 days stale beats 1 day stale
