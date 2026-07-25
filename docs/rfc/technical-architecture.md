# Rotation33 Technical Architecture

**Date:** 2026-07-15

**Status:** Request For Comments

**Related documents:** [`mvp.md`](/docs/prd/mvp.md),
[`recommendation-engine-design.md`](/docs/rfc/recommendation-engine-design.md),
[`discogs-collection-shape.md`](/docs/research/discogs-collection-shape.md)
(sync spike findings)

## 1. Scope

This document covers the system architecture around the recommendation engine:
the technology stack, code organization, the domain model and public interface
of each component, storage, Discogs sync, the web layer, and deployment.

Storage (Section 7) follows from the interfaces rather than driving them.
Section 8 lists the points where this design revises the PRD and the engine RFC.

## 2. Technology stack

| Concern | Choice | Why |
|---|---|---|
| Backend | Flask + Jinja | Server-rendered, one process. Enough for a single-user LAN app with no async. |
| Interactivity | htmx | Partial HTML swaps for regenerate and the sync progress poll. No build step, no SPA, no JSON API. Log-play and the other form actions are plain post-and-redirect: they carry no partial-update benefit that would repay the extra moving part, and they keep working without JavaScript. |
| Datastore | SQLite (WAL mode, `busy_timeout` set) | Single file on the data volume, zero-config, trivial backup. WAL gives concurrent readers plus one writer; `busy_timeout` absorbs write-vs-sync contention (Section 12). |
| ORM | SQLAlchemy | Relations map cleanly; a hairy read can drop to raw SQL. |
| Discogs | python3-discogs-client | Handles auth, pagination, and rate-limit backoff. |
| Background work | In-process `threading.Thread`, single WSGI worker | One sync at a time. No Celery/Redis. Requires a single worker process (Section 14) and a run guard (Section 9). |
| Time | Configurable timezone | `TZ` env var; `infra.now()` feeds recency and mood pre-select. Returns naive local time in the configured zone, so no comparison mixes aware and naive datetimes. No calendar-day boundary (Section 6). |
| Migrations | Alembic from the first table | Play history cannot be regenerated: a collection re-syncs from Discogs, plays do not. The first sync is real data, so there is no window in which `create_all()` alone is safe. |
| Styling | One hand-written stylesheet, mobile-first | No framework and no build step. CSS custom properties for the palette, flexbox and grid for layout. NFR-3 is a per-screen obligation, not a pass at the end. |
| Cover art | On disk, path plus source URL in DB | Cached to the data volume during sync; served as static files. Source URL stored for change detection. Required by NFR-4. |
| Deployment | Dockerfile + docker-compose.yml | Compose wires the app to a named volume and env config. |

## 3. Code organization

The repository root is a small set of modules named for the product's
vocabulary. A module starts as a single `foo.py` and is promoted to a `foo/`
package only when it grows unwieldy.

### Component contract

Each root module is a domain component that:

- owns its public dataclass domain models and its private ORM storage models
  (the ORM models never leave the module);
- exposes functions that take an SQLAlchemy `Session` when they touch the
  database, and that accept and return only dataclasses, never ORM objects;
- does not import other components. The exception is a facade, a component whose
  job is coordination, which may depend downward on the components it wires. The
  rule is "no import cycles"; a one-directional fan-out is fine.

A component enriches results only from what it already depends on. `sessions`
returns ids and lets the view join to `records`, because it cannot depend on
`records`. The `recommendations` facade returns fully rendered records, because
it already depends on `records`.

The view layer (`app.py`) owns the request-scoped database session and holds only
dataclasses. The rule it follows is that a view may join across components for
display, but may not implement a rule. Assembling the session log from
`sessions.plays` and `records`, or diffing `records.styles` against
`moods.affinity_map` for the FR-18 review list, is display composition and
belongs in the view. Anything that decides something, such as which releases are
eligible or why a result is empty, belongs in a facade. `db` in the signatures
below is that session. Functions that touch no
storage (for example `moods.for_time`) take no `db`, and the pure engine
(`picker`) takes neither `db` nor a clock.

Id aliases (`ReleaseId = str`, `InstanceId = str`) are defined locally in each
module that uses them. A bare `str` alias carries no coupling and keeps `picker`
importing nothing.

### Database access: the private mapper

Each component encapsulates its database access in a private, session-bound
`_Mapper`. The mapper holds the queries and the row-to-domain and
domain-to-row transforms, and is the only code in the module that touches a
table. Public functions carry validation and business rules, then delegate
persistence to the mapper. An infra change is contained to one private class per
component, and the rules stay out of the SQL.

```python
# records.py (abridged; full dataclasses in Section 5.1)

class _ReleaseRow(Base): ...        # private ORM rows; never leave the module
class _InstanceRow(Base): ...

class _Mapper:                      # the only code that touches the tables
    def __init__(self, db: Session):
        self._db = db

    @classmethod                    # row -> domain
    def _release(cls, r: _ReleaseRow) -> Release:
        return Release(r.id, r.artist, r.title, list(r.styles),
                       _served_url(r.cover_path), r.year,
                       [cls._instance(i) for i in r.instances])

    def recommendable(self) -> list[Release]:      # query -> domain
        stmt = (select(_ReleaseRow).join(_InstanceRow)
                .where(_InstanceRow.is_playable,
                       _InstanceRow.retirement_status != RetirementStatus.RETIRED)
                .distinct())
        return [self._release(r) for r in self._db.scalars(stmt)]

    def upsert(self, release: Release) -> None:    # domain -> rows
        row = self._db.get(_ReleaseRow, release.id) or _ReleaseRow(id=release.id)
        row.artist, row.title, row.year, row.styles = (
            release.artist, release.title, release.year, list(release.styles))
        for inst in release.instances:             # metadata only; does not write
            irow = self._db.get(_InstanceRow, inst.id) or _InstanceRow(
                id=inst.id, release_id=release.id) # is_playable / retirement_status
            irow.description = inst.description     # on existing rows (FR-2)
            self._db.add(irow)
        self._db.add(row)

# public surface: validate or enforce rules, then delegate
def recommendable(db: Session) -> list[Release]:
    return _Mapper(db).recommendable()

def set_playable(db: Session, instance_id: InstanceId, playable: bool) -> None:
    m = _Mapper(db)
    if m.get_instance(instance_id) is None:        # rule/validation here
        raise UnknownInstance(instance_id)
    m.set_playable(instance_id, playable)          # persistence in the mapper
```

## 4. Module map

An acyclic dependency set. Arrows point from dependent to dependency.

```
app             -> everything (thin views, request session)
recommendations -> records, sessions, moods, picker    (facade)
sync            -> records, infra                       (background thread)
records         -> infra
sessions        -> infra
moods           -> infra
picker          (pure; imports nothing domain)
infra           (Base, engine, sessionmaker, env, now)
```

| Module | Owns | Responsibility |
|---|---|---|
| `infra.py` | `Base`, `engine`, `SessionLocal`, env config, `now()` | Shared SQLAlchemy bootstrap and cross-cutting infrastructure. No domain models or logic. |
| `records.py` | Release, Instance | Collection catalog: album/pressing metadata, condition, retirement. |
| `moods.py` | Mood, Affinity | The five fixed moods, their editable descriptions, and the style-to-mood affinity map. |
| `sessions.py` | Session, Play | A sitting and the plays logged into it; release-level recency. |
| `recommendations.py` | Recommendation | Facade. Produces, persists, and reads a session's recommendations; owns the recency window (Section 5.4). |
| `picker.py` | Candidate, Affinity | Pure, swappable engine. Fit filter plus recency-weighted draw over releases. No `db`, no clock. |
| `sync.py` | SyncRun | Background Discogs sync: fetch, upsert via `records`, cache art, flag retirements, report progress. |
| `app.py` | (none) | Flask app factory, request session lifecycle, routes, rendering. Promotes to `app/` if it strains. |

## 5. Components: interfaces and domain models

A Release is an album (Discogs master release); an Instance is one owned
physical copy (a Discogs collection instance). Recency, fit, and staleness are
release-level; the specific instance matters only when a play is logged.
Rotation33 is vinyl-only: sync stores only instances with a Vinyl format
(Section 9), so `records` never contains a CD or cassette.

Identity comes from Discogs; there are no internal surrogate keys. The sync spike
([`discogs-collection-shape.md`](/docs/research/discogs-collection-shape.md))
pinned the mapping:

- `Release.id` is the Discogs `master_id`, namespaced `m<id>`. When `master_id`
  is `0` or absent (a release with no master), it falls back to the release id,
  namespaced `r<id>`. Namespacing prevents a master id and a release id from
  aliasing in the one `release.id` column.
- `Instance.id` is the Discogs collection `instance_id`, the per-copy holding id.
  It is not the release id (`item.id`), which is shared across copies of a
  pressing.

Each entity's own key is `id`; a reference to another entity keeps a pointing
name (`Play.release_id`, `Candidate.release_id`). An Instance is always reached
through its Release aggregate, so it holds no `release_id` on the dataclass; that
FK lives only in the ORM. Ids for internal entities with no Discogs counterpart
(`Session`, `Play`) are `uuid4` hex, minted by their write functions.

### 5.1 `records`

```python
class RetirementStatus(Enum):
    ACTIVE = "active"; PENDING = "pending"; RETIRED = "retired"

@dataclass(frozen=True)
class Instance:
    id: InstanceId                    # Discogs collection instance_id
    is_playable: bool                 # FR-13
    retirement_status: RetirementStatus
    description: str | None = None    # from the pressing's format text; only useful with >1 copy

@dataclass(frozen=True)
class Release:                        # the album, across pressings
    id: ReleaseId                     # m<master_id>, or r<release_id> when no master
    artist: str
    title: str
    styles: list[str]                 # drive mood fit
    cover_url: str | None             # local static URL the template renders (FR-6)
    year: int | None
    instances: list[Instance]         # aggregate root
```

```python
# Reads
def browse(db) -> list[Release]            # every owned album, artist then title (FR-12a)
def search(db, text) -> list[Release]      # albums matching artist or title, case-insensitive
def get(db, release_id) -> Release | None
def recommendable(db) -> list[Release]     # albums with >=1 playable instance whose status != RETIRED
def pending_retirements(db) -> list[Release]    # albums narrowed to their PENDING instances (FR-2a)
def styles(db) -> set[str]                 # distinct styles present; view diffs vs moods for FR-18

# Writes
def upsert(db, release) -> None            # sync metadata (FR-1); never touches condition/retirement (FR-2)
def reconcile_retirements(db, present_instance_ids) -> list[Instance]  # whole-fetch diff (FR-2a)
def confirm_retirement(db, instance_ids) -> None
def set_playable(db, instance_id, playable) -> None                    # FR-13
```

`recommendable` returns albums with at least one playable instance whose status
is not `RETIRED`. A `PENDING` instance is still recommendable, because FR-2a
excludes an instance only once retirement is confirmed. `browse` and `search` do
not filter `is_playable`: a not-playable copy is still loggable and browsable
(FR-13a), only suppressed from suggestions. Retirement is a whole-collection
set-difference, so it is a separate `reconcile_retirements` (only on a complete
fetch, Section 9), not part of the per-album `upsert`.

### 5.2 `moods`

The five moods are a fixed code constant (name plus time window). Only the
description and the affinity map are editable, persisted with code-level
defaults. There is no seeding step: a read returns the persisted override if one
exists, otherwise the default. This satisfies FR-17, and a future reset is a
delete.

```python
@dataclass(frozen=True)
class Mood:
    name: str                        # one of the fixed five
    description: str                 # editable (FR-15), defaulted from code
    window: TimeWindow               # soft pre-select window; code constant

@dataclass(frozen=True)
class Affinity:                      # everything needed to judge fit for one mood
    weights: Mapping[str, float]     # this mood's style -> affinity
    mapped_styles: frozenset[str]    # every style anywhere in the map; unmapped means eligible
```

```python
# Reads
def choices(db) -> list[Mood]        # the five, descriptions merged; start-picker and settings
def get(db, name) -> Mood            # one mood for display
def affinity(db, name) -> Affinity   # fit lookup, for the facade-to-picker handoff
def for_time(now) -> str             # time-of-day pre-select (FR-3); no db
def affinity_map(db) -> dict         # the whole editable map; keys feed the FR-18 diff

# Writes
def set_description(db, name, text) -> None                # FR-15
def set_affinity_map(db, mapping) -> None                  # FR-16
```

`for_time` returns a mood for every clock time: the five windows tile the whole
day, with After Dark (18:00 onward) wrapping past midnight to cover 00:00 to
06:00 before First Light begins. `set_affinity_map` validates at the boundary:
affinities are floats in `[0, 1]` and every mood name in the submitted map is one
of the five, so a typo fails loudly instead of silently never matching. The fit
rules (best-style-wins FR-8, unmapped-eligible FR-18) live in `picker`, not on
`Affinity`.

### 5.3 `sessions`

A session is a sitting. It stays current until the next one starts; there is no
midnight rollover (Section 6). Recency is rolling and release-level. `Play`
carries a denormalized `release_id` so `sessions` answers recency from its own
table without depending on `records`.

```python
@dataclass(frozen=True)
class Session:
    id: str                          # uuid4, minted by start()
    mood: str                        # chosen mood name
    started_at: datetime

@dataclass(frozen=True)
class Play:
    id: str                          # uuid4, minted by log_play()
    session_id: str
    instance_id: InstanceId          # which copy was played
    release_id: ReleaseId            # denormalized for release-level recency
    played_at: datetime
```

```python
def current(db) -> Session | None    # the latest session; None only on first run
def get(db, session_id) -> Session   # one session by id; raises UnknownSession
def start(db, mood, now) -> Session   # always a new session; the prior one stops being latest
def log_play(db, session_id, instance_id, release_id, played_at) -> Play
def remove_play(db, play_id) -> None  # FR-12b; enforces "current session only" against current()
def plays(db, session_id) -> list[Play]                # session log and FR-9 in-session exclusion
def latest_plays(db) -> dict[ReleaseId, datetime]      # most recent play per release
```

`latest_plays` is the single recency read: one entry per release ever played
(bounded by collection size). The facade derives both the recency exclusion
(`now - dt <= window`) and the staleness ranking (`now - dt`, never-played ranks
first) from it. Retired instances keep their plays' `release_id`, so they still
contribute to release recency (FR-2a). Removing a play shrinks the set, restoring
eligibility with no special case (FR-12b).

`get` is non-optional and raises `UnknownSession`, unlike `records.get` which
returns `None`. The facade needs a session's mood by id (Section 5.4 step 1) and
`current` cannot serve that read: it would make `generate` fail on a valid but
no-longer-latest session, and it conflates "not the current session" with "no
such session." One of those is a normal state and the other is a fault, so they
cannot share a return value.

The session-log display is assembled by the view: `plays` returns ids, and the
view joins to `records` for artwork and titles.

### 5.4 `recommendations` (facade)

Release-level throughout. A recommendation is a Release; the instance is chosen
at log time, so there is no per-pick wrapper type. The facade also owns the
configurable recency window (FR-14).

```python
class EmptyReason(Enum):
    NOTHING_AVAILABLE = "nothing_available"  # no playable, non-retired records at all
    NO_FIT            = "no_fit"             # records exist, none fit this mood
    ALL_RECENT        = "all_recent"         # fit exists, all of it played inside the window
    SESSION_EXHAUSTED = "session_exhausted"  # fit exists and is not recent; this session saw it

@dataclass(frozen=True)
class RecommendationResult:
    releases: list[Release]              # records.Release, in draw order; empty when nothing was drawn
    reason: EmptyReason | None           # set only when releases is empty
```

```python
def generate(db, session_id, now, rng=None) -> RecommendationResult
    # First-generate and regenerate (FR-4, FR-9) are one act. Persists a new batch.
def active(db, session_id) -> RecommendationResult
    # The batch currently showing (latest generated_at), re-hydrated. Empty if none yet.
def window(db) -> timedelta                   # the recency window (FR-14); code default 3 days
def set_window(db, days) -> None              # persist the user-adjusted window (FR-14)
```

`generate` returns the empty-with-reason outcome as a value, not an exception:
FR-10 makes "no picks, and why" an expected state. `reason` travels in the same
object as `releases` so a caller cannot drop it. Genuine faults (unknown
`session_id`, DB failure) raise. `generate` requests a fixed `count = 5`; `picker`
returns up to five. A thin pool yielding 1 or 2 picks is a valid result, shown
as-is; only an empty draw sets an `EmptyReason` (FR-4's "3 to 5" is not a floor;
see Section 8).

The flow inside `generate`:

1. `affinity = moods.affinity(db, session.mood)`, mapped to `picker.Affinity`.
2. `pool = records.recommendable(db)`; `recency = sessions.latest_plays(db)`.
3. `candidates = [picker.Candidate(r.id, r.styles, staleness) for r in pool]`,
   where `staleness` is `now - recency[r.id]` for a release that has plays and
   the sentinel `timedelta.max` for one that has none, so a never-played release
   ranks first. The branch is deliberate. `timedelta.max` is only ever sorted on
   and never added to a datetime, so it is safe; deriving the same effect by
   subtracting a sentinel *date* is not, because it would raise on the
   never-played path alone if `infra.now()` ever returned an aware datetime
   (Section 2 pins it naive for this reason).
4. `fit = picker.matching(candidates, affinity)`, which is also the FR-10 "does anything fit" pool.
5. exclude release ids where `now - last_play <= window(db)` (FR-4), plus this
   session's played releases (`sessions.plays`) and shown releases (own
   recommendation rows) (FR-9).
6. `drawn = picker.draw(surviving_fit, 5, rng)`, ordered by the weighted draw.
7. if `drawn` is empty, choose the reason: pool empty gives `NOTHING_AVAILABLE`,
   else fit empty gives `NO_FIT`, else `ALL_RECENT` when every fitting release is
   inside the recency window, else `SESSION_EXHAUSTED`. The last two are worth
   telling apart because the remedy differs: waiting or widening the window helps
   the first, and only a new session helps the second. Folding them together
   reported "played recently" to a user who had played nothing.
8. persist `drawn` as a new batch with a `position` per row (session_id,
   release_id, generated_at, position) so draw order survives. Build the result
   by filtering the in-hand `pool` to `drawn`, preserving order, with no re-`get`.

`active` is a later call without the pool in hand. It reads the latest batch
ordered by `position` and calls `records.get` on each id, dropping any release
retired or removed since it was generated (rare; the batch shows fewer). This is
the one place a persisted pick can silently vanish, and it is intended.

Before anything has been generated for a session, `active` returns empty with
`reason` as `None`. "Nothing generated yet" is not one of FR-10's explained-empty
states, and it must not be reported as one: every `EmptyReason` is a claim about
the collection ("nothing available", "nothing fits", "all played recently"), and
none of those has been established when no draw has happened. Only `generate`
holds the pool and fit sets needed to tell them apart, so `active` cannot
manufacture a reason without re-deriving them. The view renders this state as
"no picks yet," distinct from FR-10's explained empty.

The batch is keyed by a surrogate id rather than the natural
`(session_id, generated_at, position)`. "Latest batch" means greatest
`generated_at`, which is ambiguous only if two generates share a timestamp — not
reachable in production at microsecond resolution with a user-driven trigger,
but routine in tests that pass a fixed `now`. Under the natural key that
collision would be an integrity error rather than a harmless merge, so the
surrogate key keeps a test-only artifact from becoming a crash.

### 5.5 `picker` (pure engine)

```python
@dataclass(frozen=True)
class Candidate:
    release_id: ReleaseId
    styles: Sequence[str]
    staleness: timedelta             # now - last_played; timedelta.max = never played (ranks first)

@dataclass(frozen=True)
class Affinity:                      # picker's own fit input, self-contained at the swap boundary
    weights: Mapping[str, float]
    mapped_styles: frozenset[str]

def matching(candidates, affinity) -> list[Candidate]
    # Fit filter: keep a candidate if it has no styles at all, or any style is
    # unmapped (FR-18), or its best mapped style has affinity > 0 (FR-8).
    # No draw, no recency. The no-styles case is an explicit branch: both other
    # conditions are vacuously false over an empty list, so omitting it excludes
    # unclassified releases from every mood forever (engine RFC, fit filter).

def draw(candidates, count, rng=None) -> list[ReleaseId]
    # Rank by staleness descending (never-played first), weight by rank position
    # (linear), sample without replacement up to count. Returns ids in draw order.
    # [] on empty; fewer than count when the pool runs out. rng defaults to a
    # module Random().
```

The facade calls `matching`, applies its own recency and session exclusion, then
`draw`. The split is what makes the fit pool available on its own for FR-10.
There is no combined `pick`; nothing calls it. `picker` defines its own
`Affinity` (identical in shape to `moods.Affinity`) so the swappable engine owns
its input contract and imports no other component.

## 6. Time

There is no calendar day. Every former use of "today" reduces to one of two clock
reads, both from `infra.now()`: recency (`now - played_at`) and the mood
pre-select (`moods.for_time(now)`).

- "Played today" (old FR-5) is subsumed by the release-level recency window.
  Anything played today is inside any window of 1 day or more, and release-level
  is stronger than the old instance-level rule.
- Recency is a rolling window, not calendar days: played 2d23h ago is excluded at
  a 3-day window; 3d1h ago is eligible.
- A session ends only when the next one starts. `current()` is the latest
  session. A long-idle current session is harmless, because the open-app flow
  leads with starting a session (mood pre-selected by `for_time(now)`).
- With `window = 0`, cross-session immediate repeats become possible, which is
  what 0 means. Intra-session repeats are still blocked by session scope (FR-9).

## 7. Storage

Each component keeps its ORM models private. The tables below are what the
interfaces imply. Cross-component foreign keys are string-named
(`ForeignKey("release.id")`) and resolve against the single `Base` metadata in
`infra`, so a DB-level reference does not become a code dependency.

- `records`: a `release` row per album (namespaced `master_id`/`release_id`;
  artist, title, year, styles, `cover_path`, `cover_source_url`) and an
  `instance` row per owned vinyl pressing (Discogs `instance_id`, release FK,
  `is_playable`, `retirement_status`, `description`). The `instance.release_id`
  FK is the only place that reference exists; it assembles the aggregate and is
  not on the dataclass. `styles` is a JSON list, not a join table. `cover_path` is
  the local file (the dataclass `cover_url` derives from it); `cover_source_url`
  is the Discogs origin, kept so sync can detect a changed image, not just a
  missing one (Section 9). The instance row also carries the Discogs pressing
  `release_id`. This is required, not optional: it powers a "view on Discogs"
  link, but its real job is to be the durable handle for the identity-drift risk
  in Section 8. If a master reassignment re-keys an album and splits its recency
  history, the pressing id is the only surviving evidence of which instances used
  to belong together. It is one integer, free from the listing payload, and it is
  the sole recovery path for the one accepted data-loss risk in this design.
- `moods`: persisted overrides only, a description row per edited mood and the
  affinity map as one JSON document. Mood identity and windows are code, not
  rows; unmapped styles (FR-18) are derived, not stored.
- `sessions`: a `session` row and a `play` row (session FK, instance id,
  denormalized release id, `played_at`). Ids are uuid4.
- `recommendations`: a `recommendation` row per drawn release (session FK,
  release id, `generated_at`, `position`); the active batch is the greatest
  `generated_at` for the session, ordered by `position`. Plus a single-row
  recency-window setting (code default 3 days). Batch rows accumulate and are not
  pruned, which is acceptable at single-user scale.
- `sync`: a single latest `sync_run` row (status, `total`, `processed`,
  timestamps, error) for progress polling and "last synced." It is metadata about
  a run, not collection data, which is what lets Section 9 commit it on its own
  schedule.

Cover art is files under `DATA_DIR/covers`, named by release id, so a retried
sync overwrites rather than orphaning. Schema is managed by Alembic from the
first table (Section 2); `alembic upgrade head` runs on startup, and `env.py`
imports every component so autogenerate sees the full `Base` metadata.

## 8. Revisions to the PRD and engine RFC

The interface work and the sync spike revise both prior documents. These are
settled here and should be reconciled back.

PRD:

- Vinyl-only scope: a Discogs collection can contain CDs and cassettes; sync
  keeps only vinyl instances. This is implied by the product but never stated.
- A Recommendation is a Release, not a specific Instance; the instance is chosen
  when a play is logged.
- "Already played today" (FR-5) becomes "within the recency window." The "session
  rolls over at end of day" language becomes "until the next session starts."
- FR-4's "3 to 5" is not a floor: a thin pool may yield 1 or 2 picks, shown
  as-is; only an empty draw is FR-10's explained-empty case.
- `notes` is dropped (FR-1 lists it, but nothing consumes it). `format` is kept
  and used: it drives the vinyl filter and the instance `description`.
- FR-18 unmapped-style flagging is derived on demand, not written at sync.
- The five moods are code constants; descriptions and the affinity map are
  persisted with code defaults, so FR-17 needs no seeding step.
- Logging is search-only, not "browse or search" (FR-12a). The dedicated log
  screen is folded into the session workspace (Section 10), where logging always
  targets the current session; the workspace shows search results on submit
  rather than the full collection. Browsing the full collection still exists on
  the Condition screen. Recorded from real use; see the session-workspace spec.
- Known risk, Discogs identity drift: master assignments change over time, so a
  no-master to has-master transition re-keys an album on a later sync and can
  split or merge its instances and recency history. Rare; accepted for the MVP.

Engine RFC:

- The engine module is `picker`, release-level, exposing `matching` and `draw`
  (no combined `pick`); `Candidate` and `Affinity` are keyed on releases.
- The "coordinator" and "read model" are the `recommendations` facade; the
  eligible pool is composed in Python from public component functions, not one
  cross-table SQL query.
- `Play` carries a denormalized `release_id`.

## 9. Discogs sync

A manual trigger (FR-1) acquires a module-level `threading.Lock` with a
non-blocking `acquire()`; a trigger that fails to acquire it no-ops, so two
rapid triggers cannot both start (a DB check-then-insert is not enough here,
since gunicorn's threaded workers, Section 14, can interleave two requests
between the check and the write). The acquiring thread spawns a
`threading.Thread`, which releases the lock in a `finally` covering its whole
body. Releasing only on the success path would strand the lock for the life of
the process if the thread raised, leaving sync permanently dead with nothing on
screen to explain it.

The lock guards concurrency within one process, but not state across restarts. A
process killed mid-sync leaves a `sync_run` at `running` forever, and the
progress endpoint would poll it indefinitely. The app factory therefore reconciles
on startup, marking any run still `running` as failed.

The thread owns its sessions rather than the view, since it has no request
context. It holds two, and the split matters:

- a **data session** from `SessionLocal`, open across the whole fetch and
  committed once at the end;
- a **progress session**, opened and committed and closed per page, writing only
  `sync_run`.

One session cannot do both jobs. Progress written inside the data transaction is
invisible to the polling request, which reads on another connection, so the bar
would sit at zero and then jump to complete; committing progress from the data
session as it goes would break the failure isolation below. Splitting them is
sound because `sync_run` is metadata about the run, not collection data
(Section 7), so committing it early leaves nothing partial behind.

The thread:

1. reads the collection total up front (`folder.count`) into `sync_run.total`,
   then walks the full collection page by page (the client library handles
   pagination and rate-limit backoff), advancing `sync_run.processed` per page
   via the progress session;
2. keeps only vinyl instances, where an instance is kept if any of its formats is
   `"Vinyl"` (`basic_information.formats[].name`), dropping CDs and cassettes;
3. groups kept instances into albums by `master_id` (present in
   `basic_information`, no extra fetch), falling back to `r<release_id>` when
   `master_id` is `0` or absent, and upserts release metadata and instances via
   `records.upsert`. A release exists if it has at least one kept vinyl instance;
4. notes which releases need cover art (the file is missing, or its
   `cover_source_url` changed) without fetching any of it yet;
5. calls `records.reconcile_retirements` once with every currently-present vinyl
   `instance_id`, inside the data transaction: absent-locally-present instances
   become `pending`, reappeared ones flip back to `active` (FR-2a);
6. commits the data session once;
7. *then* downloads the noted cover art to `DATA_DIR/covers`, outside the
   transaction, and marks `sync_run` complete.

Step 7 is separated from step 4 deliberately, and the ordering is load bearing.
SQLAlchemy autoflushes before a query, so the `db.get` inside `records.upsert`
flushes the previous album and the data session takes SQLite's database-wide
write lock on the very first release of a cold sync. Downloading inside that
loop therefore held the write lock for the whole run rather than for the commit:
measured at 7.86s of a 7.87s sync against the 72-release fixture with 40ms stub
downloads, and at realistic network latency a concurrent `log_play` exhausted
`busy_timeout` and failed with `database is locked`. A first sync is exactly when
every cover is missing, so it was the first-run path that broke. Deferring is
safe because covers are not collection data: `upsert` records `cover_path` inside
the transaction and `records._served_url` renders from the file's existence, so a
release whose art has not landed yet shows the placeholder rather than a broken
image.

Failure isolation (FR-2, FR-2a): metadata commit and retirement-flagging happen
only after a complete, successful fetch, in the single data-session commit at
step 6. A partial or failed fetch rolls that session back and marks `sync_run`
failed, writing no retirements and no half-collection. A single
cover-art download failure is logged and skipped (old file kept); it does not
fail the sync. Cover files are named by release id, so a failed sync's partial
downloads are overwritten on the next run rather than accumulating. Sync never
touches plays, condition flags, or session history.

Two edges are lightly evidenced from the spike and worth confirming during build:
the `master_id: 0` no-master shape (one real example), and a single Discogs
release listing multiple formats such as an LP-plus-CD (the `any(format ==
"Vinyl")` rule keeps it, untested against live data).

## 10. Web layer and htmx

Routes live in `app.py` (promoting to `app/` with blueprints if it strains). The
view passes only dataclasses to templates.

Transactions follow SQLAlchemy's begin/commit/rollback framing convention
([session basics][sa-framing]). Components never commit; the caller frames the
unit of work, and the view is that caller. The framing is a context manager, so
commit and rollback are structural rather than hand-written on success and
except paths:

- A **read-only view** uses the request-scoped session (`db()`), which the
  `teardown_appcontext` handler closes. Reads leave nothing to commit.
- A **writing view** frames its work in `with write() as db:`, where `write()`
  is `SessionLocal.begin()`. The block commits on success, rolls back on any
  exception, and closes the session either way.

A writing view frames a fresh session rather than mutating the request read
session, which keeps a write from entangling with the request's reads and
sidesteps SQLAlchemy 2.0 autobegin: `begin()` must be a session's first use, and
the read session may already have issued a query. Committing in teardown instead
would commit the finished half of a request that raised partway through, since
teardown runs on the exception path too; framing removes that path entirely.

[sa-framing]: https://docs.sqlalchemy.org/en/20/orm/session_basics.html#framing-out-a-begin-commit-rollback-block

The UI is responsive across phone, tablet, and desktop (NFR-3). With no framework
and no build step (Section 2), that is hand-written CSS across three breakpoints
for every screen, so it is scoped per screen rather than deferred to a styling
pass at the end.

- Home: the start-a-session control with the time-appropriate mood pre-selected
  (`moods.for_time(now)`), or a "run a sync" prompt for an empty collection
  (FR-19). When a session is already active it redirects to the session
  workspace; `?new=1` overrides the redirect to start a fresh session.
- Session (the workspace): the primary screen for an active session, holding the
  recommendations, an on-demand search-to-log, and the plays logged this session
  together, until a new session is explicitly started. Start picks a mood then
  `recommendations.generate`; regenerate carries the pinned picks as `keep` and
  htmx-swaps the recommendations panel (FR-9); picks render with cover art (FR-6)
  and the FR-10 message when empty. A pick is logged in place, choosing the copy
  inline when more than one is owned (FR-11, FR-12a). "Log something else"
  searches `records` and shows results only on submit; a play is removed from the
  active session (FR-12b). There is no separate log screen: logging always
  targets the current session, which is the screen already in view. See
  [`docs/superpowers/specs/2026-07-20-session-workspace-design.md`](/docs/superpowers/specs/2026-07-20-session-workspace-design.md).
- Condition: toggle `is_playable` (FR-13).
- Settings: recency window (FR-14, via `recommendations.window`/`set_window`);
  mood descriptions (FR-15); the affinity map as a validated JSON `<textarea>`
  (FR-16); the FR-18 review list (view diff of `records.styles` vs
  `moods.affinity_map`).
- Sync: trigger; a status endpoint htmx polls for `processed / total` progress;
  the retirement-confirmation list from `records.pending_retirements`.

## 11. Configuration and secrets

Environment only, never committed: `DISCOGS_TOKEN`, `DISCOGS_USERNAME`, `TZ`,
`DATA_DIR` (default `/data`). `infra` reads them and constructs the engine,
sessionmaker, `Base`, and `now()`. The recency window is not an env var: it is
user-editable at runtime and persisted by `recommendations` (code default 3
days).

## 12. Error handling and concurrency

- Sync failures are isolated (Section 9): no silent retirements, no partial
  writes, one guard against concurrent runs.
- The empty-recommendation outcome is a value carrying its `EmptyReason`, not a
  silent empty render (Section 5.4); genuine faults raise.
- `set_affinity_map` validates at the write boundary (Section 5.2).
- SQLite writer contention: WAL allows concurrent readers but a single writer, so
  a request write (`log_play`) can collide with the sync thread's commit.
  `busy_timeout` (about 5 seconds) is set on every connection, so the loser waits
  and retries rather than erroring. What keeps that wait inside the timeout is
  not the single commit by itself: the data session becomes a writer at its first
  autoflush, not at `commit()`, so the write window is everything between that
  flush and the commit. Keeping it short means keeping *network* out of it, which
  is why cover art is fetched after the commit (Section 9). The per-page progress
  commits are single-row, brief, and happen during the fetch while the data
  session has issued no writes at all, so they never contend.
  `busy_timeout` also does not cover every case: a session that reads and *then*
  upgrades to a write gets `SQLITE_BUSY_SNAPSHOT` immediately, with no retry, if
  another connection committed in between. The view layer therefore has an
  `OperationalError` handler that rolls back, tells the user the database was
  busy, and invites a retry, rather than showing a traceback. All of this holds
  only under a single WSGI worker (Section 14); multiple workers would each have
  their own threads and no shared guard.

## 13. Testing strategy

- `picker`: `pytest` with a seeded or unseeded `random.Random`. Deterministic
  exact-output tests, invariants (never exceeds count, no duplicates, `[]` on
  empty, unmapped style always fits), and a statistical test that a never-played
  release is drawn meaningfully more often than a recently-played one. `matching`
  and `draw` tested independently.
- `recommendations`: the facade over a temporary SQLite DB. Generate, regenerate
  (FR-9), recency and session exclusion, draw-order preservation, thin-pool
  results, all three `EmptyReason`s, and window get/set.
- `sessions`, `records`, `moods`: recency, exclusion, retirement, condition,
  affinity-map validation, and `for_time` coverage across the full 24-hour clock
  including the post-midnight wrap.
- `sync`: mock the Discogs client; assert the vinyl filter, `master_id` grouping
  and no-master fallback, the retirement diff, the run guard, and failure
  isolation (a partial fetch writes nothing). Also the recovery paths: a thread
  that raises still releases the lock, a `running` run left by a killed process
  is reconciled to failed on startup, and progress is readable from another
  connection while the fetch is still in flight (the assertion that catches a
  regression back to one session).
- `app`: Flask test-client smoke tests for the session, log-play, and sync flows.

Each test runs against a fresh SQLite database from a `tmp_path` fixture. Two
session fixtures encode the transaction convention (Section 10): `db` for
arrange-act-assert inside one uncommitted transaction, which is most tests, and
`begin` for the begin/commit/rollback framing (`with begin() as db:`) wherever a
test must observe state across a real commit boundary. A test then exercises a
component through the same framing the view layer uses, rather than a bespoke
commit pattern that production never runs.

## 14. Deployment

A `Dockerfile` builds the app image. `docker-compose.yml` mounts a named volume
at `DATA_DIR` (SQLite file plus `covers/`), supplies env configuration, and pins
the app to a single WSGI worker (for example `gunicorn -w 1`, threaded for
request concurrency), since the in-process sync thread and its run guard assume
one process. No authentication: the LAN is the trust boundary (NFR-2), and there
is no external call except a user-triggered sync (NFR-4).
