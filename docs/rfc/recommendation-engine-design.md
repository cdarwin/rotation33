# Rotation33 Recommendation Engine Design

**Date:** 2026-07-15

**Status:** Request For Comments

## Scope

This document covers three things:

1. The consumer side of the eligible-candidates read model.
2. The per-session exclusion coordinator.
3. The swappable recommendation engine.

Out of scope: DB schema and migrations, Discogs sync, and web routes and
templates. The domain model (`Instance`, `Play`, `Session`, `Recommendation`,
`Settings`, the five moods, 3 to 5 picks per session) is settled in the PRD and
treated as given here.

The five moods are First Light, Heads Down, Peak, Golden Hour, and After Dark.
A mood is chosen per session, on demand. The same mood can be picked more than
once in a day, each time in its own session. There is no full-day
pre-generation: picks are generated when a session asks for them.

## Component boundaries

Three layers, each independently testable, each with one job.

### 1. Read model

```python
eligible_candidates(date) -> list[Candidate]
```

Applies release-level N-day exclusion only. An instance is excluded if its
release was played (any pressing) within `exclusion_window_days`, which
defaults to 3.

It also excludes instances that are not playable (FR-13) or retired (FR-2a). A
retired instance's plays still count toward release-level recency for any other
owned pressing of the same release, so retirement drops the instance from the
candidate pool without erasing its freshness contribution. Because recency is
derived from the remaining play history, removing a play (FR-12b) restores that
release's eligibility with no special handling.

The read model is mood- and session-agnostic. It knows nothing about moods,
sessions, or same-day state.

### 2. Coordinator

A plain service module or class that orchestrates generating (or regenerating)
one session's picks. It:

1. Calls the read model for the day's eligible pool.
2. Subtracts the same-day and same-session exclusion set (see below).
3. Calls the engine with the filtered pool.
4. Persists the returned picks as a new `Recommendation` batch. New rows,
   nothing overwritten.
5. If the filtered pool supports no picks, works out why for FR-10 (see below).

The exclusion set has two parts:

- Every `instance_id` played today. Day-scoped, per FR-5.
- Every `instance_id` shown or logged earlier in *this* session. Session-scoped,
  per FR-9. That means rows from this session's own prior (now-superseded)
  `Recommendation` batches, plus plays logged into it.

A record shown but not played in an earlier session is not excluded from a later
session. It only failed to be picked, and FR-9 scopes passed-over exclusion to
the current session, not the whole day.

For the FR-10 "why is nothing showing" case, the coordinator compares the
fit-filtered pool (before recency and same-day exclusion) against the final
pool. "Empty because nothing fits the mood" and "empty because everything that
fits played recently" are different messages, and the coordinator holds both
pools, so it can tell them apart. Rendering the message is out of scope;
surfacing the reason is not.

This is the only layer that touches persistence, and the only layer that knows
what "today", "this session", or "regenerate" mean. Same-day and same-session
exclusion lives here, not in the read model (which stays purely about
release-level freshness) and not in the engine (which stays stateless).

### 3. Engine package

Package `recommendation_engine/`. A pure function with no persistence access and
no knowledge of dates, sessions, or regeneration. The public surface is one
function exported from `__init__.py`:

```python
def pick(
    candidates: list[Candidate],
    mood: Mood,
    count: int,                        # 3 to 5, chosen by the caller
    rng: random.Random | None = None,  # defaults to a module-level Random()
) -> list[str]:
    """Returns up to `count` distinct instance_ids. Returns fewer than
    `count` (possibly []) if the fit-filtered pool doesn't support it,
    and never raises for a thin pool."""
```

`Mood` carries the mood's style-affinity lookup (see below). It does not carry
the editable Mood Description (FR-15). This engine scores on style affinity and
recency only. The human-facing description informs the listener; it is not
consumed here. Leaving it out of the engine is deliberate, not an omission.

There is no `Protocol` or ABC, because there is exactly one implementation and a
formal interface would just be ceremony. The "swappable module" contract is the
package boundary plus this function signature. An alternative engine is a second
package exposing the same name and signature, and the coordinator's import line
is the only thing that changes. If a second implementation ever exists, that is
the moment to decide whether a real interface earns its keep.

## Data flow: generate vs. regenerate

One coordinator method, `generate_for_session(session)`, drives both flows.

**First generate for a session.** The user starts a session by choosing a mood
(FR-3), and the coordinator generates that session's first batch of 3 to 5 picks
on demand. Nothing is pre-generated for other moods or later in the day.

**Regenerate (FR-9).** Call `generate_for_session(session)` again. The exclusion
set already includes this session's own prior (now-superseded) batches and
everything played today, so a new set never repeats a record played today,
logged into this session, or shown and passed over earlier in this session.
Regenerate and first-generate need no special-casing between them.

**"Currently active" picks** for a session are the rows from the most recent
`generated_at` for that session. This is a read-side query concern
(`ORDER BY generated_at DESC` within the session), not something the coordinator
or engine tracks specially. All of the session's rows, active or superseded,
count toward the session-scoped exclusion.

## Style-to-mood affinity mapping

The mapping lives in `Settings`, shaped as nested weights. A style maps to a
dict of `{mood: affinity}`, where affinity is a float in [0, 1]:

```python
{
    "Bossa Nova": {"First Light": 1.0, "Golden Hour": 0.6},
    "Cool Jazz":  {"First Light": 0.8, "After Dark": 0.9},
    "Prog Rock":  {"Golden Hour": 0.7},
    "Funk":       {"Peak": 1.0},
}
```

A mood absent from a style's dict means affinity 0 for that mood via that style.
It is not stored and it is not an error. The mapping is hand-edited as a plain
nested structure (dict or JSON). There is no admin UI and no validation beyond
"floats in [0, 1]", because you are the only editor and a bad number just means
a record shows up in the wrong mood until you correct it.

### Unmapped styles (FR-18)

A style that is entirely absent from the map is treated as eligible for every
mood. It clears the fit filter everywhere, and it is flagged for review at sync
time (the flagging lives in sync, out of scope here). This is distinct from a
style that is in the map but has no entry for the mood in question, which counts
as affinity 0 via that style. A record is never silently excluded from
recommendations because one of its styles is unmapped.

The affinity lookup carried by `Mood` encodes this distinction: unmapped style
means eligible; mapped-but-no-entry-for-this-mood means 0.

### Multi-style resolution

A release's fit for a given mood is the best of its styles for that mood
(best-style-wins): the max mapped affinity across its styles, or "eligible" if
any of its styles is unmapped. A release with one great-fit style and one
unrelated style is still a great fit. A genuinely hybrid record is not penalized
for also being tagged something irrelevant to the mood in question (FR-8).

## Condition and quality flag

`Instance` gets a dedicated boolean field (for example `is_playable`), set
manually when you flag a damaged copy. It is not derived from parsing Discogs
notes. The read model excludes non-playable instances before the engine ever
sees them (FR-13), so the engine's `Candidate` type does not need to carry this
field at all: by the time candidates reach `pick()`, only playable, non-retired
ones remain.

A non-playable instance can still be logged as an off-recommendation play
(FR-13a), but that is a play-logging concern and out of scope here. The flag
only suppresses suggestions.

## Selection algorithm (inside `pick()`)

1. Filter candidates to those that fit the mood. A candidate fits if its
   best-matching style has positive affinity for `mood` (best-matching being the
   max mapped affinity across the release's styles), or if any of the release's
   styles is unmapped (FR-18, eligible everywhere). "Fits at all" is the
   fit/no-fit line; there is no separate tunable threshold.
2. If the filtered pool is empty, return `[]`. The coordinator, not the engine,
   decides whether "empty" means no-fit or all-recently-played for FR-10.
3. Rank the filtered pool by days-since-last-played (release-level, the same
   freshness signal the read model uses), descending. Never-played instances are
   treated as infinitely stale and rank first.
4. Set each candidate's weight to its rank position (1 = least stale, up to N =
   most stale). This is a linear rank weight, not exponential decay or bucketed
   tiers.
5. Draw `count` distinct instances via weighted sampling without replacement:
   call `rng.choices(pool, weights=...)`, remove the winner from the pool, and
   repeat until `count` are drawn or the pool is exhausted. Stdlib
   `random.choices` samples with replacement, so this pop-and-redraw loop is
   what makes it without-replacement; there is no stdlib primitive for weighted
   sampling without replacement.
6. Return the drawn instance_ids. This may be fewer than `count` if the pool ran
   out first, which is not an error condition.

Mood-fit and recency are deliberately not blended into one continuous score
(for example `fit * recency_weight`). Fit is a hard filter; recency is the only
thing driving the weighted draw within the filtered pool. This keeps "why wasn't
X picked" a two-step debugging question: either it failed the fit filter, or it
lost the recency-weighted draw, never an opaque blended score. The weighted
random draw (rather than a fixed top-N by staleness) is what gives
session-to-session variety (FR-7).

## Testing strategy

`pick()` takes an injected `random.Random`, defaulting to the stdlib
module-level `random` in production. There is no `hypothesis` dependency; stdlib
`pytest` plus a seeded or unseeded `random.Random` covers this.

**Deterministic tests.** Seed the RNG and assert exact output for small fixed
pools. Given these three candidates and this seed, exactly these instance_ids
come back.

**Invariant tests** (seeded, checking shape rather than exact values). The
function never returns a candidate that failed the fit filter, never exceeds
`count`, never duplicates an instance_id, returns `[]` on an empty filtered
pool, and returns fewer than `count` without raising when the pool is thin. For
FR-18, a candidate whose only style is unmapped is never filtered out.

**Statistical test.** Build a fixed pool with a known staleness spread (one
never-played candidate and one played yesterday, both fitting the mood), run
`pick()` a few hundred times with an unseeded RNG, and assert the never-played
candidate is selected meaningfully more often than the recently-played one (for
example more than 2x). This is the one test that would actually catch an
inverted-weight bug; the deterministic and invariant tests would not.

## Explicitly deferred

Not designed here:

- Admin UI or validation for editing the affinity mapping.
- Pool-exhaustion fallback (for example, relaxing the fit filter or exclusion
  window when a mood's pool is thin). For now, thin pools just return fewer than
  `count` picks, with no retry or relaxation logic.
- A formal `Protocol` or ABC for the engine boundary. Add one if and when a
  second engine implementation actually exists.
- Consuming Mood Descriptions (FR-15) in scoring. Not used by this engine.
