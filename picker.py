"""The recommendation engine: a pure, swappable selection algorithm.

This module imports nothing from the rest of the project and touches neither the
database nor the clock (NFR-5, architecture RFC section 3). Everything it needs
arrives as arguments, which is what makes it replaceable at a seam rather than
entangled with storage. `ReleaseId` is a local alias for exactly that reason: a
bare `str` alias carries no coupling.

The two public functions are deliberately not fused into one `pick`. The facade
calls `matching`, applies its own recency and session exclusion to the result,
then calls `draw`. Keeping them apart is what makes the fit pool separately
available for the FR-10 "does anything fit this mood at all" question.
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

ReleaseId = str

_rng = random.Random()
"""Module-level default source of randomness, used when a caller passes no rng.

Tests inject a seeded `random.Random` instead; production lets this stand.
"""


@dataclass(frozen=True)
class Candidate:
    """One release offered to the engine, reduced to just what selection needs."""

    release_id: ReleaseId
    styles: Sequence[str]
    staleness: timedelta
    """`now - last_played`; `timedelta.max` means never played, and ranks first.

    The sentinel is only ever sorted on, never added to a datetime, so it cannot
    overflow. See architecture RFC section 5.4 step 3 for why the caller branches
    to this value rather than subtracting a sentinel date.
    """


@dataclass(frozen=True)
class Affinity:
    """A mood's style affinities, self-contained at the engine swap boundary.

    Identical in shape to `moods.Affinity`, and deliberately a separate type: the
    engine owns its own input contract and imports no other component.
    """

    weights: Mapping[str, float]
    """Style -> affinity in [0, 1] for this mood. A mapped style absent here is 0."""

    mapped_styles: frozenset[str]
    """Every style the affinity map knows about, across all moods.

    A style outside this set is *unmapped* and eligible everywhere (FR-18). A
    style inside it but missing from `weights` is a deliberate 0 for this mood.
    """


def matching(candidates: Iterable[Candidate], affinity: Affinity) -> list[Candidate]:
    """Return the candidates that fit the mood, order preserved.

    A candidate fits if it carries no styles at all, or if any of its styles is
    unmapped (FR-18: an unmapped style is eligible for every mood, so a release is
    never silently dropped because of a tag nobody has classified yet), or if its
    best mapped style has affinity above zero (FR-8, best-style-wins: one
    unrelated tag does not penalize a release that also carries a great-fit one).

    The no-styles case is the same principle as the unmapped one: absence of
    classification is not evidence of a poor fit, and the alternative is a record
    that can never be recommended for any mood, with nothing on screen to say so.

    This is a hard filter, not a score. Fit and staleness are never blended, which
    keeps "why wasn't X picked" a two-step question: it failed the fit filter, or
    it lost the weighted draw.
    """
    return [c for c in candidates if _fits(c, affinity)]


def _fits(candidate: Candidate, affinity: Affinity) -> bool:
    styles = candidate.styles
    if not styles:
        # A release Discogs carries no style tags for is unclassified, not
        # disqualified. Without this branch both `any()` calls below are False
        # and the release is silently excluded from every mood forever, which is
        # the exact failure FR-18 exists to prevent. Real collections contain
        # these: the captured fixture has two.
        return True
    if any(style not in affinity.mapped_styles for style in styles):
        return True
    return any(affinity.weights.get(style, 0.0) > 0.0 for style in styles)


def draw(
    candidates: Sequence[Candidate],
    count: int,
    rng: random.Random | None = None,
) -> list[ReleaseId]:
    """Draw up to `count` distinct release ids, staleness-weighted, in draw order.

    The pool is ranked by staleness descending (never-played first) and each
    candidate is weighted by its rank position: 1 for the least stale up to N for
    the most stale. A linear rank weight, not exponential decay or bucketed tiers.

    Sampling is without replacement via pop-and-redraw, because `random.choices`
    samples *with* replacement and the stdlib has no weighted without-replacement
    primitive. Weights are recomputed against the shrinking pool each round, so
    rank position keeps meaning the same thing after a winner is removed.

    Candidates of *equal* staleness are ordered randomly, by a `random()` second
    sort key. Rank position is the draw weight and Python's sort is stable, so
    leaving ties in caller order silently converts that order into a weighting.
    Not a corner case: on a freshly synced collection every release is
    never-played and therefore tied at `timedelta.max`, and
    `records.recommendable` returns them sorted by artist, which weighted the
    whole first-run experience toward the top of the alphabet by up to 69x.

    Returns `[]` on an empty pool and fewer than `count` when the pool runs out.
    Neither is an error: a thin pool yielding one or two picks is a valid result,
    and the facade decides what an empty draw means (FR-10).
    """
    if count <= 0:
        return []

    source = _rng if rng is None else rng
    # The random() second key is the tie-break; see the note above.
    pool = sorted(candidates, key=lambda c: (c.staleness, source.random()), reverse=True)
    drawn: list[ReleaseId] = []

    while pool and len(drawn) < count:
        # Rank weights over the current pool: most stale (index 0) gets len(pool),
        # least stale gets 1. Recomputed each round as the pool shrinks.
        weights = range(len(pool), 0, -1)
        # Draw an index, not the candidate itself: two candidates can compare equal
        # (same id, styles and staleness), and removing by value would drop the
        # wrong one.
        winner = source.choices(range(len(pool)), weights=weights, k=1)[0]
        drawn.append(pool.pop(winner).release_id)

    return drawn
