# Rotation33 Recommendation Engine Design

**Date:** 2026-07-15

**Status:** Request For Comments

**Related documents:** [`mvp.md`](/docs/prd/mvp.md)

## Scope

This document details the swappable recommendation engine, the `picker` module.
`picker` is a pure set of functions: it takes a pool of candidate releases and a
mood's style affinity and returns a ranked, varied selection. It has no database
access and no knowledge of dates, sessions, or regeneration.

The surrounding orchestration (building the candidate pool, applying recency and
per-session exclusion, persisting a batch, and choosing the FR-10 reason for an
empty result) is the `recommendations` facade. It is summarized here only as
the engine's calling context.

The domain model (Release, Instance, Play, Session, Recommendation, the five
moods) is settled in the PRD. Recommendations are
release-level: a pick is a Release, and the specific instance is chosen when a
play is logged.

## Calling context

A mood is chosen per session, on demand. The same mood can be picked more than
once in a day, each in its own session. There is no full-day pre-generation:
picks are generated when a session asks for them.

The facade calls the engine in two steps:

1. `matching(candidates, affinity)` filters the pool to releases that fit the
   mood.
2. `draw(matching, count, rng)` ranks the fit pool by staleness and draws a
   varied selection.

Between the two, the facade removes releases excluded by the recency window
(FR-4) and by same-session state (FR-9). Splitting fit from draw lets the facade
measure the fit pool on its own, which it needs in order to tell an FR-10
"nothing fits this mood" apart from "everything that fits played recently."
Condition and retirement are handled upstream (by `records` and the facade), so
by the time candidates reach the engine only playable, non-retired releases
remain, and `Candidate` carries no such flags.

## Public surface

Module `picker`. There is no `Protocol` or ABC: there is one implementation, and
a formal interface would be ceremony. The swappable contract is the module
boundary plus these signatures. An alternative engine is a second module
exposing the same names, and the facade's import line is the only thing that
changes.

```python
@dataclass(frozen=True)
class Candidate:
    release_id: str
    styles: Sequence[str]
    staleness: timedelta             # now - last_played; timedelta.max = never played

@dataclass(frozen=True)
class Affinity:
    weights: Mapping[str, float]     # this mood's style -> affinity in [0, 1]
    mapped_styles: frozenset[str]    # every style present anywhere in the map

def matching(candidates, affinity) -> list[Candidate]
def draw(candidates, count, rng=None) -> list[str]   # release_ids, up to count, in draw order
```

`Affinity` carries only what fit needs, not the editable Mood Description
(FR-15): the description informs the listener, not the engine. `picker` defines
its own `Affinity` (identical in shape to the one `moods` produces) so the engine
imports no other component. `draw` returns up to `count` release ids and never
raises on a thin pool; fewer than `count`, including zero, is a valid result.

There is no combined `pick`. The facade always needs the two halves separately,
so only `matching` and `draw` are exposed.

## Style-to-mood affinity

The affinity map lives in settings, shaped as nested weights. A style maps to a
dict of `{mood: affinity}`, where affinity is a float in `[0, 1]`:

```python
{
    "Bossa Nova": {"First Light": 1.0, "Golden Hour": 0.6},
    "Cool Jazz":  {"First Light": 0.8, "After Dark": 0.9},
    "Prog Rock":  {"Golden Hour": 0.7},
    "Funk":       {"Peak": 1.0},
}
```

A mood absent from a style's dict means affinity 0 for that mood via that style.
It is not stored and is not an error. Validation (floats in `[0, 1]`, and every
mood name known) happens where the map is written, in `moods.set_affinity_map`;
the engine assumes a valid `Affinity`.

### Unmapped styles (FR-18)

A style entirely absent from the map is eligible for every mood: it clears the
fit filter everywhere. This differs from a style present in the map but with no
entry for a given mood, which is affinity 0 for that mood. A release is never
silently excluded because one of its styles is unmapped. `Affinity.mapped_styles`
encodes the distinction: a style not in that set is unmapped and eligible; a
style in it with no weight for the mood is 0.

Surfacing unmapped styles for review (FR-18) is derived on demand by the UI (a
diff of the styles present in the collection against the affinity map keys), not
written during sync.

### Releases with no styles at all

A release carrying an empty style list fits every mood, on the same principle:
absence of classification is not evidence of a poor fit.

This needs stating separately because it does not fall out of the unmapped rule.
"Any style is unmapped" and "any style has positive affinity" are both vacuously
false over an empty list, so the natural implementation of the two rules above
excludes such a release from every mood, permanently, with nothing on screen to
explain it. That is precisely the outcome FR-18 exists to prevent, arrived at by
a different route.

These are not hypothetical. Discogs leaves `styles` empty on some releases, and
the captured collection fixture contains two — both owned pressings of the same
album, which without this rule could never be recommended.

### Multi-style resolution

A release's fit for a mood is the best of its styles for that mood
(best-style-wins): the maximum mapped affinity across its styles, or eligible if
any style is unmapped. A release with one great-fit style and one unrelated style
is still a great fit, and is not penalized for the unrelated tag (FR-8).

## Selection algorithm

`matching(candidates, affinity)`:

1. Keep a candidate if it fits the mood. A release fits if its best-matching
   style has positive affinity for the mood (best-matching is the maximum mapped
   affinity across its styles), or if any of its styles is unmapped (FR-18,
   eligible everywhere), or if it carries no styles at all. "Fits at all" is the
   line; there is no separate tunable threshold.

   The empty-style-list case must be an explicit branch, not an emergent
   property: both preceding conditions are vacuously false over an empty list,
   so without it an unclassified release is excluded from every mood forever.

`draw(candidates, count, rng)`, over the fit pool the facade passes in after its
own recency and session exclusion:

2. If the pool is empty, return `[]`. The facade, not the engine, decides whether
   empty means no-fit or all-recently-played (FR-10).
3. Shuffle the pool, then rank it by staleness descending; never-played
   (`timedelta.max`) ranks first. The shuffle is not decoration: rank position is
   the draw weight and Python's sort is stable, so leaving equal-staleness
   candidates in caller order silently converts that order into a weighting. On a
   freshly synced collection every release is never-played and therefore tied,
   and `records.recommendable` returns them sorted by artist, which weighted the
   entire first-run experience toward the top of the alphabet by up to 69x.
4. Weight each candidate by its rank position (1 = least stale, up to N = most
   stale): a linear rank weight, not exponential decay or bucketed tiers.
5. Draw `count` distinct releases by weighted sampling without replacement: call
   `rng.choices`, remove the winner from the pool, and repeat until `count` are
   drawn or the pool is exhausted. `random.choices` samples with replacement, so
   this pop-and-redraw loop is what makes the draw without-replacement; there is
   no stdlib primitive for weighted sampling without replacement.
6. Return the drawn release ids in draw order. Fewer than `count` if the pool ran
   out first, which is not an error.

Fit and recency are not blended into one continuous score. Fit is a hard filter;
staleness is the only signal driving the weighted draw within the fit pool. This
keeps "why wasn't X picked" a two-step question: it either failed the fit filter
or lost the recency-weighted draw, never an opaque blended score. The weighted
draw, rather than a fixed top-N by staleness, is what gives session-to-session
variety (FR-7).

## Testing strategy

`draw` takes an injected `random.Random`, defaulting to a module-level `Random()`
in production. No `hypothesis` dependency; stdlib `pytest` plus a seeded or
unseeded `Random` covers it. `matching` and `draw` are tested independently.

- Deterministic: seed the RNG and assert exact release ids for a small fixed
  pool.
- Invariant (seeded, checking shape not exact values): never returns a candidate
  that failed the fit filter, never exceeds `count`, never duplicates a release
  id, returns `[]` on an empty pool, and returns fewer than `count` without
  raising on a thin pool. For FR-18, a candidate whose only style is unmapped is
  never filtered out.
- Statistical: build a fixed pool with a known staleness spread, including a
  never-played release and one played yesterday, both fitting the mood, run
  `draw` a few hundred times with an unseeded RNG, and assert the never-played
  release is drawn meaningfully more often than the recently-played one. This is
  the test that catches an inverted-weight bug; the deterministic and invariant
  tests would not.

  The pool needs **at least five candidates** for that assertion to have room to
  hold, and the reason is worth stating because the obvious two-release version
  of this test cannot pass. Linear rank weighting (step 4 above) assigns weights
  `1..N` by rank. Over a two-release pool the weights are exactly `2` and `1`, so
  the stalest release is drawn first with probability `2/3` and the ratio
  converges on exactly `2.0`. A "more than 2x" threshold is then unreachable at
  any sample size: the test is not flaky, it is arithmetically impossible, and it
  fails against a correct implementation. Widen the pool instead. At five
  candidates the never-played release carries weight `5` against yesterday's `1`,
  and the margin is comfortable.

## Explicitly deferred

- Admin UI for editing the affinity map. Validation beyond floats-in-`[0, 1]` and
  known mood names lives in `moods`, not here.
- Pool-exhaustion fallback (relaxing the fit filter or recency window when a
  mood's pool is thin). For now a thin pool returns fewer than `count`, with no
  retry or relaxation.
- A formal `Protocol` or ABC for the engine boundary. Add one if and when a
  second engine implementation exists.
- Consuming Mood Descriptions (FR-15) in scoring.
