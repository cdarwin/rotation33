"""The recommendation engine: a pure, swappable selection algorithm.

Imports nothing from the rest of the project and touches neither the database
nor the clock, which is what makes it replaceable at a seam. `matching` and
`draw` stay separate because the caller needs the fit pool on its own, to tell
"nothing fits this mood" apart from "everything that fits played recently".
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

ReleaseId = str

_rng = random.Random()
"""Default randomness. Tests inject a seeded Random instead."""


@dataclass(frozen=True)
class Candidate:
    """One release offered to the engine, reduced to what selection needs."""

    release_id: ReleaseId
    styles: Sequence[str]
    staleness: timedelta
    """`now - last_played`; `timedelta.max` means never played, and ranks first.

    Only ever sorted on, never added to a datetime, so it cannot overflow.
    """


@dataclass(frozen=True)
class Affinity:
    """A mood's style affinities. Deliberately not shared with `moods`: the
    engine owns its input contract and imports no other component."""

    weights: Mapping[str, float]
    """Style -> affinity in [0, 1]. A mapped style absent here is 0."""

    mapped_styles: frozenset[str]
    """Every style the map knows about, across all moods. A style outside this
    set is unmapped and eligible everywhere; one inside it but missing from
    `weights` is a deliberate 0."""


def matching(candidates: Iterable[Candidate], affinity: Affinity) -> list[Candidate]:
    """The candidates that fit the mood, order preserved.

    A hard filter, not a score. Fit and staleness are never blended, so "why
    wasn't X picked" stays a two-step question.
    """
    return [c for c in candidates if _fits(c, affinity)]


def _fits(candidate: Candidate, affinity: Affinity) -> bool:
    styles = candidate.styles
    if not styles:
        # Unclassified, not disqualified. Without this branch both `any()` calls
        # below are False and the release is excluded from every mood forever.
        # Real collections contain these: the captured fixture has two.
        return True
    if any(style not in affinity.mapped_styles for style in styles):
        return True
    # Best-style-wins: one unrelated tag must not sink a release that also
    # carries a great-fit one.
    return any(affinity.weights.get(style, 0.0) > 0.0 for style in styles)


def draw(
    candidates: Sequence[Candidate],
    count: int,
    rng: random.Random | None = None,
) -> list[ReleaseId]:
    """Draw up to `count` distinct release ids, staleness-weighted, in draw order.

    Ranked by staleness descending, then weighted by rank position: 1 for the
    least stale up to N for the most. Fewer than `count`, including zero, is a
    valid result, not an error.
    """
    if count <= 0:
        return []

    source = _rng if rng is None else rng
    # The random() second key breaks staleness ties. Rank position *is* the
    # weight and Python's sort is stable, so ties left in caller order turn that
    # order into a weighting: every release on a fresh collection is never-played
    # and therefore tied, and the caller supplies them sorted by artist, which
    # skewed draws toward the top of the alphabet by 69x.
    pool = sorted(candidates, key=lambda c: (c.staleness, source.random()), reverse=True)
    drawn: list[ReleaseId] = []

    while pool and len(drawn) < count:
        # `random.choices` samples *with* replacement and the stdlib has no
        # weighted without-replacement primitive, so pop and redraw. Weights are
        # recomputed each round so rank keeps meaning the same thing.
        weights = range(len(pool), 0, -1)
        # Draw an index, not the candidate: two candidates can compare equal, and
        # removing by value would drop the wrong one.
        winner = source.choices(range(len(pool)), weights=weights, k=1)[0]
        drawn.append(pool.pop(winner).release_id)

    return drawn
