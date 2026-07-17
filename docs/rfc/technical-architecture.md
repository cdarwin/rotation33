# Rotation33 Technical Architecture

**Date:** 2026-07-15

**Status:** Request For Comments

**Related documents:** [`mvp.md`](/docs/prd/mvp.md),
[`recommendation-engine-design.md`](/docs/rfc/recommendation-engine-design.md),
[`discogs-collection-shape.md`](/docs/research/discogs-collection-shape.md)
(spike findings backing the sync design)

## 1. Scope

This document covers the system architecture around the recommendation engine:
the technology stack, code organization, the domain model and public interface
of each component, storage, Discogs sync, the web layer, and deployment.

The design was worked **interface-first**: each component's public contract was
settled from what its consumers need, the dataclass domain models fell out of
those contracts, and storage is treated as a consequence of both (Section 7) —
not as the driver. Section 8 records where this evolves the PRD and the engine
RFC.

## 2. Technology stack

| Concern | Choice | Why |
|---|---|---|
| Backend | **Flask + Jinja** | Minimal, server-rendered, one process. Covers a single-user LAN app with no async ceremony. |
| Interactivity | **htmx** | Partial HTML swaps for log-play, regenerate, and sync progress. No build step, no SPA, no JSON API split. |
| Datastore | **SQLite (WAL mode, `busy_timeout` set)** | Single file on the data volume, zero-config, trivial backup. WAL gives concurrent readers + one writer; `busy_timeout` absorbs the rare write-vs-sync contention (Section 12). |
| ORM | **SQLAlchemy** | Real relations map naturally; a hairy read can drop to hand-written SQL. |
| Discogs | **python3-discogs-client** | Handles auth, pagination, and rate-limit backoff so we don't own that code. |
| Background work | **In-process `threading.Thread`, single WSGI worker** | One sync at a time for one user. No Celery/Redis. The thread model requires a single worker process (Section 14) and a run guard (Section 9). |
| Time | **Configurable timezone, one clock read** | `TZ` env var; `infra.now()` feeds recency and mood pre-select. No calendar-day boundary (Section 6). |
| Migrations | **`create_all()` now, Alembic before first real data** | No migration ceremony while the schema churns pre-launch; adopt versioning the moment there is history worth protecting. |
| Cover art | **On disk, path + source URL in DB** | Images cached to the data volume during sync; DB holds the path and the source URL (for change detection). Served as static files. Required by NFR-4. |
| Deployment | **Dockerfile + docker-compose.yml** | Compose wires the app to a named data volume and env config; one `docker compose up`. |

## 3. Code organization: screaming architecture

The repository root is a small set of modules named for the product's own
vocabulary, so the layout announces the domain rather than a framework. Modules
start as a single `foo.py` and are promoted to a `foo/` package (splitting out
internal modules) only when one grows unwieldy. No speculative package trees.

### Component contract

Each root module is a domain component that:

- owns its **dataclass domain models** (its public currency) and its
  **ORM storage models** (private — never leaked past its own boundary);
- exposes functions that take an SQLAlchemy `Session` when they touch the
  database, and that accept and return **only dataclasses**, never ORM objects;
- does not import other components, **except** a component whose explicit job is
  coordination (a *facade*), which may depend downward on the components it
  wires. The rule is really "no import cycles"; a facade with a one-directional
  fan-out of dependencies is a legitimate organizing strategy.

A corollary used throughout: **a component enriches results only from what it
already depends on.** `sessions` returns ids and lets the view join to `records`
(it can't depend on `records`); the `recommendations` facade returns fully
rendered records (it already depends on `records`).

The **view layer** (`app.py`) owns the request-scoped database session, holds
only dataclasses, and stays thin — it hands off to a facade rather than
accreting orchestration logic. `db` in the signatures below is that session;
functions that touch no storage (e.g. `moods.for_time`) take no `db`, and the
one pure engine (`picker`) takes neither `db` nor a clock.

Small id aliases (`ReleaseId = str`, `InstanceId = str`) are defined locally in
each module that wants them — a bare `str` alias carries no coupling, and it
keeps `picker` importing nothing.

### Database access: the private mapper

Every component encapsulates all database access in a private, session-bound
`_Mapper`: it holds the queries and the row↔domain transforms and is the only
code in the module that touches a table. Public functions stay thin — they carry
validation and business rules, then delegate persistence to the mapper. So the
blast radius of an infra change is one private class per component, and the rules
never move.

```python
# records.py (abridged — full dataclasses in §5.1)

class _ReleaseRow(Base): ...        # private ORM rows; never leave the module
class _InstanceRow(Base): ...

class _Mapper:                      # the ONLY code that touches the tables
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
        for inst in release.instances:             # metadata only — never writes
            irow = self._db.get(_InstanceRow, inst.id) or _InstanceRow(
                id=inst.id, release_id=release.id) # is_playable / retirement_status
            irow.description = inst.description     # on existing rows (FR-2)
            self._db.add(irow)
        self._db.add(row)

# public surface: validate / enforce rules, then delegate
def recommendable(db: Session) -> list[Release]:
    return _Mapper(db).recommendable()

def set_playable(db: Session, instance_id: InstanceId, playable: bool) -> None:
    m = _Mapper(db)
    if m.get_instance(instance_id) is None:        # rule/validation lives here …
        raise UnknownInstance(instance_id)
    m.set_playable(instance_id, playable)          # … persistence lives in the mapper
```

The mapper stays a faithful translator with no judgment; every guard is readable
in the public surface without wading through SQL.

## 4. Module map

An acyclic dependency set. Arrows point from dependent to dependency.

```
app.py ──────────────► everything (thin views, request session)
recommendations.py ──► records, sessions, moods, picker   (facade)
sync.py ─────────────► records, infra                     (background thread)
records.py ──────────► infra
sessions.py ─────────► infra
moods.py ────────────► infra
picker.py                                                 (pure; imports nothing domain)
infra.py                                                  (Base, engine, sessionmaker, env, now)
```

| Module | Owns | Responsibility |
|---|---|---|
| `infra.py` | `Base`, `engine`, `SessionLocal`, env config, `now()` | The one shared SQLAlchemy bootstrap and cross-cutting infrastructure. No domain models, no domain logic. |
| `records.py` | Release, Instance | Collection catalog: album/pressing metadata, condition, retirement. |
| `moods.py` | Mood, Affinity | The five fixed moods, their editable descriptions, and the style→mood affinity map. |
| `sessions.py` | Session, Play | A sitting and the plays logged into it; release-level recency. |
| `recommendations.py` | Recommendation | **Facade.** Produces/persists/reads a session's recommendations; owns the recency window (Section 5.4). |
| `picker.py` | Candidate, Affinity | **Pure, swappable engine.** Fit filter + recency-weighted draw over releases. No `db`, no clock. |
| `sync.py` | SyncRun | Background Discogs sync: fetch, upsert via `records`, cache art, flag retirements, report progress. |
| `app.py` | — | Flask app factory, request session lifecycle, routes, rendering. Promotes to `app/` if it strains. |

## 5. Components: interfaces and domain models

`Release = album` (Discogs *master release*), `Instance = one owned physical
copy` (a Discogs *collection instance*). Recency, fit, and staleness are all
**release-level**; the specific instance matters only when a play is logged.
Rotation33 is **vinyl-only**: sync stores only instances with a Vinyl format
(Section 9), so `records` never contains a CD or cassette.

**Identity comes straight from Discogs** — no internal surrogate keys — and the
spike ([`discogs-collection-shape.md`](/docs/research/discogs-collection-shape.md))
pinned the exact mapping:

- **`Release.id` = the Discogs `master_id`**, namespaced `m<id>`. When
  `master_id` is `0`/absent (a release with no master), fall back to the release
  id, namespaced `r<id>`. Namespacing prevents a master id and a release id from
  aliasing in the one `release.id` column.
- **`Instance.id` = the Discogs collection `instance_id`** — the per-copy holding
  id (*not* the release id, which is `item.id` and is shared across copies of a
  pressing).

Each entity's own key is `id`; a reference to another entity keeps a pointing
name (`Play.release_id`, `Candidate.release_id`). An `Instance` is always reached
through its `Release` aggregate, so it holds no `release_id` on the dataclass —
that FK lives only in the private ORM model. Ids for internal entities that have
no Discogs counterpart (`Session`, `Play`) are minted as `uuid4` hex by their
write functions.

### 5.1 `records`

```python
class RetirementStatus(Enum):
    ACTIVE = "active"; PENDING = "pending"; RETIRED = "retired"

@dataclass(frozen=True)
class Instance:
    id: InstanceId                    # Discogs collection instance_id
    is_playable: bool                 # FR-13
    retirement_status: RetirementStatus
    description: str | None = None    # from the pressing's format text; only when >1 owned

@dataclass(frozen=True)
class Release:                        # the album, across pressings
    id: ReleaseId                     # m<master_id>, or r<release_id> when no master
    artist: str
    title: str
    styles: list[str]                 # drive mood fit
    cover_url: str | None             # local static URL the template renders (FR-6)
    year: int | None
    instances: list[Instance]         # aggregate root; the parent is always known when you hold an Instance
```

```python
# Reads
def browse(db) -> list[Release]            # every owned album, artist→title order (FR-12a)
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
is **not `RETIRED`** — a `PENDING` instance is still recommendable, because
FR-2a excludes an instance only once retirement is *confirmed*. `browse`/`search`
do not filter `is_playable` at all — a not-playable copy is still loggable and
browsable (FR-13a), only suppressed from suggestions. Retirement is a
whole-collection set-difference, so it's a separate `reconcile_retirements` (only
on a complete fetch, Section 9), not part of the per-album `upsert`.

### 5.2 `moods`

The five moods are a **fixed code constant** (name + time window); only the
description and the affinity map are editable, persisted with **code-level
defaults**. There is no seeding step — a read returns the persisted override if
present, else the default (this satisfies FR-17; a future reset is a delete).

```python
@dataclass(frozen=True)
class Mood:
    name: str                        # one of the fixed five
    description: str                 # editable (FR-15), defaulted from code
    window: TimeWindow               # soft pre-select window; code constant

@dataclass(frozen=True)
class Affinity:                      # everything needed to judge fit for ONE mood
    weights: Mapping[str, float]     # this mood's style → affinity
    mapped_styles: frozenset[str]    # every style anywhere in the map → unmapped means eligible
```

```python
# Reads
def choices(db) -> list[Mood]        # the five, descriptions merged — start-picker + settings
def get(db, name) -> Mood            # one mood for display
def affinity(db, name) -> Affinity   # fit lookup, for the facade→picker handoff
def for_time(now) -> str             # time-of-day pre-select (FR-3); NO db
def affinity_map(db) -> dict         # the whole editable map; keys feed the FR-18 diff

# Writes
def set_description(db, name, text) -> None                # FR-15
def set_affinity_map(db, mapping) -> None                  # FR-16
```

`for_time` returns a mood for **every** clock time: the five windows tile the
whole day, with **After Dark (18:00 →) wrapping past midnight to cover 00:00–
06:00** before First Light begins. `set_affinity_map` validates at the boundary:
affinities are floats in `[0, 1]` and every mood name in the submitted map is one
of the five (a typo'd mood fails loudly instead of silently never matching).
`Affinity` is deliberately dumb data — the fit *rules* (best-style-wins FR-8,
unmapped-eligible FR-18) live in `picker`.

### 5.3 `sessions`

A session is a *sitting*: it stays current until the next one starts — there is
no midnight rollover (Section 6). Recency is rolling and release-level; `Play`
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
    release_id: ReleaseId            # denormalized → release-level recency
    played_at: datetime
```

```python
def current(db) -> Session | None    # the latest session; None only on first run
def start(db, mood, now) -> Session   # always a new session; the prior one just stops being latest
def log_play(db, session_id, instance_id, release_id, played_at) -> Play
def remove_play(db, play_id) -> None  # FR-12b; enforces "current session only" against current()
def plays(db, session_id) -> list[Play]                # session log + FR-9 in-session exclusion
def latest_plays(db) -> dict[ReleaseId, datetime]      # most recent play per release
```

`latest_plays` is the single recency read: one entry per release ever played
(bounded by collection size), from which the facade derives **both** the recency
exclusion (`now - dt <= window`) and the staleness ranking (`now - dt`,
never-played ranks first). Retired instances keep their plays' `release_id`, so
they still contribute to release recency (FR-2a); removing a play just shrinks
the set, restoring eligibility with no special case (FR-12b).

The session-log *display* is assembled by the view: `plays` returns ids, the
view joins to `records` for artwork and titles.

### 5.4 `recommendations` (facade)

Release-level throughout. A recommendation *is* a `Release`; the instance is
chosen at log time, so there is no per-pick wrapper type. The facade also owns
the configurable recency window (FR-14).

```python
class EmptyReason(Enum):
    NOTHING_AVAILABLE = "nothing_available"  # no playable, non-retired records at all
    NO_FIT            = "no_fit"             # records exist, none fit this mood
    ALL_RECENT        = "all_recent"         # fit exists, but all played recently / excluded

@dataclass(frozen=True)
class RecommendationResult:
    releases: list[Release]              # records.Release, in draw order; empty when nothing was drawn
    reason: EmptyReason | None           # set iff releases is empty — the two travel together
```

```python
def generate(db, session_id, now, rng=None) -> RecommendationResult
    # First-generate and regenerate (FR-4, FR-9) — one act. Persists a new batch.
def active(db, session_id) -> RecommendationResult
    # The batch currently showing (latest generated_at), re-hydrated. Empty if none yet.
def window(db) -> timedelta                   # the recency window (FR-14); code default 3 days
def set_window(db, days) -> None              # persist the user-adjusted window (FR-14)
```

`generate` returns the empty-with-reason outcome as a **value** — FR-10 makes
"no picks, here's why" a designed, expected state, not a fault. `reason` rides
in the same object as `releases` so a caller can't silently drop it. Genuine
faults (unknown `session_id`, DB failure) still raise. It requests a fixed
`count = 5`; `picker` returns *up to* five. **A thin pool yielding 1–2 picks is a
valid result, shown as-is** — only a truly empty draw sets an `EmptyReason`
(FR-4's "3–5" is aspirational, not a floor; see Section 8).

The flow inside `generate`:

1. `affinity = moods.affinity(db, session.mood)`; map to `picker.Affinity`.
2. `pool = records.recommendable(db)`; `recency = sessions.latest_plays(db)`.
3. `candidates = [picker.Candidate(r.id, r.styles, now - recency.get(r.id, ...)) for r in pool]`.
4. `fit = picker.matching(candidates, affinity)` — also the FR-10 "does anything fit?" pool.
5. exclude release ids where `now - last_play <= window(db)` (FR-4), plus this
   session's played releases (`sessions.plays`) and shown releases (own
   recommendation rows) (FR-9).
6. `drawn = picker.draw(surviving_fit, 5, rng)` — ordered by the weighted draw.
7. if `drawn` is empty, pick the reason: pool empty → `NOTHING_AVAILABLE`; else
   fit empty → `NO_FIT`; else → `ALL_RECENT`.
8. persist `drawn` as a new batch with an explicit **`position`** per row
   (session_id, release_id, generated_at, position) so draw order survives.
   Build the result by filtering the in-hand `pool` to `drawn` (no re-`get`
   round trip), preserving order.

`active` — a later call without the pool in hand — reads the latest batch
ordered by `position` and `records.get`s each id, **dropping any release retired
or removed since it was generated** (rare; the batch just shows fewer). This is
the one place a persisted pick can silently vanish, and it's intended.

### 5.5 `picker` (pure engine)

```python
@dataclass(frozen=True)
class Candidate:
    release_id: ReleaseId
    styles: Sequence[str]
    staleness: timedelta             # now - last_played; timedelta.max = never played (ranks first)

@dataclass(frozen=True)
class Affinity:                      # picker's own fit input — self-contained at the swap boundary
    weights: Mapping[str, float]
    mapped_styles: frozenset[str]

def matching(candidates, affinity) -> list[Candidate]
    # Fit filter: keep a candidate if any style is unmapped (FR-18) or its best
    # mapped style has affinity > 0 (FR-8). No draw, no recency.

def draw(candidates, count, rng=None) -> list[ReleaseId]
    # Rank by staleness descending (never-played first), weight by rank position
    # (linear), weighted sample WITHOUT replacement up to `count`. Returns ids in
    # draw order. [] on empty; fewer than count when the pool runs out (not an
    # error). rng defaults to a module Random().
```

The facade calls `matching`, applies its own recency/session exclusion, then
`draw` — the split is why the fit pool is available on its own for FR-10. There
is no combined `pick`: nothing calls it. `picker.Affinity` duplicates
`moods.Affinity`'s shape by a few lines on purpose, so the swappable engine owns
its input contract rather than importing another component's.

## 6. Time: no calendar day

There is no "today." Every use of it collapsed to one of two clock reads:
recency (`now - played_at`) and the mood pre-select (`moods.for_time(now)`),
both served by `infra.now()`.

- **"Played today" exclusion (old FR-5)** is subsumed by the release-level
  recency window: anything played today is inside any window ≥ 1 day, and
  release-level is stronger than the old instance-level rule.
- **Recency is a rolling window**, not calendar days (played 2d23h ago →
  excluded at a 3-day window; 3d1h ago → eligible).
- **A session ends only when the next begins** — no midnight rollover.
  `current()` is simply the latest session. A long-idle current session is
  harmless: the open-app flow leads with *start a session* (mood pre-selected by
  `for_time(now)`).
- With `window = 0`, cross-session immediate repeats become possible; that is
  what 0 means. Intra-session repeats are still blocked by session scope (FR-9).

## 7. Storage as a consequence

Each component keeps its ORM models private; the tables below are what the
interfaces above *imply*, not a schema the design was built around.
Cross-component foreign keys are string-named (`ForeignKey("release.id")`) and
resolve against the single `Base` metadata in `infra`, so a DB-level reference
never becomes a code dependency.

- **`records`** — a `release` row per album (namespaced `master_id`/`release_id`;
  artist/title/year/styles/`cover_path`/`cover_source_url`) and an `instance` row
  per owned **vinyl** pressing (Discogs `instance_id`, release FK, `is_playable`,
  `retirement_status`, `description`). The `instance.release_id` FK is the one
  place that reference exists — it assembles the aggregate and is never on the
  dataclass. `styles` is a JSON list, not a join table. `cover_path` is the local
  file (the dataclass `cover_url` is derived from it); `cover_source_url` is the
  Discogs origin, kept so sync can detect a *changed* image, not just a missing
  one (Section 9). The ORM may also stash the pressing
  `release_id` per instance purely for a "view on Discogs" link — storage-only.
- **`moods`** — persisted *overrides* only: a description row per edited mood and
  the affinity map (one JSON document). Mood identity and windows are code, not
  rows; unmapped styles (FR-18) are derived, not stored.
- **`sessions`** — a `session` row and a `play` row (session FK, instance id,
  denormalized release id, `played_at`). Ids are uuid4.
- **`recommendations`** — a `recommendation` row per drawn release (session FK,
  release id, `generated_at`, `position`); the active batch is the greatest
  `generated_at` for the session, ordered by `position`. Plus a single-row
  **recency-window** setting (code default 3 days). Batch rows accumulate and are
  **not pruned** — acceptable at single-user scale, stated rather than overlooked.
- **`sync`** — a single latest `sync_run` row (status, `total`, `processed`,
  timestamps, error) for progress polling and "last synced."

Cover art is files under `DATA_DIR/covers`, named by release id (so a retried
sync overwrites rather than orphaning). `create_all()` runs on startup after
`app` imports the components so their tables register; Alembic is adopted before
the first deployment that holds history worth keeping.

## 8. Revisions to the PRD and engine RFC

The interface work (and the sync spike) evolved both prior documents. These are
settled here and should be reconciled back:

**PRD:**
- **Vinyl-only scope**: a Discogs collection can contain CDs and cassettes; sync
  keeps only vinyl instances. State this explicitly (it's implied by the product
  but never written).
- A Recommendation is a **Release**, not "a specific Instance" — the instance is
  chosen when a play is logged.
- "Already played today" (FR-5) becomes "within the recency window"; the
  "session rolls over at end of day" language is dropped for "until the next
  session starts."
- **FR-4's "3–5" is aspirational, not a floor**: a thin pool may yield 1–2 picks,
  shown as-is; only an empty draw is FR-10's explained-empty case.
- **`notes` is dropped** (FR-1 lists it, but nothing in the product consumes it);
  **`format` is kept and used** — it drives the vinyl filter and the instance
  `description`.
- FR-18 unmapped-style flagging is **derived on demand**, not written at sync.
- The five moods are code constants; descriptions and the affinity map are
  persisted with code defaults, so FR-17 needs no seeding step.
- **Known risk — Discogs identity drift**: master assignments change over time;
  a no-master→has-master transition re-keys an album on a later sync, which can
  split or merge its instances and their recency history. Rare; accepted for the
  MVP, documented so it isn't a surprise.

**Engine RFC:**
- The engine module is **`picker`**, release-level, exposing **`matching`** and
  **`draw`** (no combined `pick`); `Candidate`/`Affinity` are keyed on releases.
- The "coordinator" and "read model" are the `recommendations` facade; the
  eligible pool is composed in Python from public component functions, not one
  cross-table SQL query.
- `Play` carries a denormalized `release_id`.

## 9. Discogs sync

A manual trigger (FR-1) first **checks for an in-progress `sync_run`** and
no-ops if one is running (the guard for "one sync at a time" — two rapid
triggers can't both start). Otherwise it spawns a `threading.Thread` that owns
its own `Session` from `SessionLocal` for its lifetime — the one deliberate
exception to "the view owns the session," since it has no request context. It:

1. reads the collection total up front (`folder.count`) into `sync_run.total`,
   then walks the **full collection** page by page (the client library handles
   pagination and rate-limit backoff), advancing `sync_run.processed` per page;
2. keeps only **vinyl** instances — an instance is kept iff any of its formats is
   `"Vinyl"` (`basic_information.formats[].name`), so CDs/cassettes are dropped;
3. groups kept instances into albums by **`master_id`** (from `basic_information`
   — present with no extra fetch), falling back to `r<release_id>` when
   `master_id` is `0`/absent; upserts release + album metadata + instances via
   `records.upsert`. A release exists iff it has ≥ 1 kept vinyl instance;
4. downloads cover art to `DATA_DIR/covers` when the file is **missing or its
   `cover_source_url` changed** (both cheap: the image URL is in the listing);
5. calls `records.reconcile_retirements` once with every currently-present vinyl
   `instance_id`: absent-locally-present instances become `pending`, reappeared
   ones flip back to `active` (FR-2a);
6. updates `sync_run` throughout; the progress bar shows `processed / total`.

**Failure isolation (FR-2, FR-2a):** metadata commit and retirement-flagging
happen only after a complete, successful fetch. A partial or failed fetch marks
`sync_run` failed and writes no retirements and no half-collection. A single
cover-art download failure is logged and skipped (old file kept); it does not
fail the sync. Cover files are named by release id, so a failed sync's partial
downloads are simply overwritten on the next run rather than accumulating —
accepted, not cleaned up. Sync never touches plays, condition flags, or session
history.

**Verified against a live collection** (see the spike doc); two edges remain
lightly evidenced and worth a glance during build: the `master_id: 0` no-master
shape (one real example) and a single Discogs release listing multiple formats
(e.g. an LP+CD — the `any(format == "Vinyl")` rule keeps it, untested live).

## 10. Web layer and htmx

Routes live in `app.py` (promoting to `app/` with blueprints if it strains). The
view opens a request-scoped session, commits/closes on teardown, and passes only
dataclasses to templates. The UI is **responsive** across phone/tablet/desktop
(NFR-3) — a design constraint on the templates and CSS, straightforward with
server-rendered Jinja + htmx and fluid layout.

- **Home** — pre-selects the time-appropriate mood (`moods.for_time(now)`); shows
  the current session (if any) and the start-a-session control. An **empty
  collection** special-cases to a "run a sync" prompt (FR-19) instead.
- **Session** — start (pick a mood → `sessions.start` → `recommendations.generate`);
  regenerate (htmx-swaps the picks panel); picks rendered with cover art (FR-6);
  the FR-10 message when `RecommendationResult` is empty.
- **Play logging** — log a release into the session, choosing the instance when
  more than one is owned (FR-12a); browse/search via `records`; remove a play
  from the active session (FR-12b).
- **Condition** — toggle `is_playable` (FR-13).
- **Settings** — recency window (FR-14, via `recommendations.window`/`set_window`);
  mood descriptions (FR-15); the affinity map as a validated JSON `<textarea>`
  (FR-16); the FR-18 review list (view diff of `records.styles` vs
  `moods.affinity_map`).
- **Sync** — trigger; a status endpoint htmx polls for `processed / total`
  progress; the retirement-confirmation list from `records.pending_retirements`.

## 11. Configuration and secrets

Environment only, never committed: `DISCOGS_TOKEN`, `DISCOGS_USERNAME`, `TZ`,
`DATA_DIR` (default `/data`). `infra` reads them and constructs the engine,
sessionmaker, `Base`, and `now()`. The recency window is **not** an env var — it
is user-editable at runtime and persisted by `recommendations` (code default 3
days).

## 12. Error handling and concurrency

- Sync failures are isolated (Section 9): no silent retirements, no partial
  writes, one guard against concurrent runs.
- The empty-recommendation outcome is a value carrying its `EmptyReason`, never
  a silent empty render (Section 5.4); genuine faults raise.
- `set_affinity_map` validates at the write boundary (Section 5.2).
- **SQLite writer contention**: WAL allows concurrent readers but a single
  writer, so a request write (`log_play`) can collide with the sync thread's
  commit. `busy_timeout` (≈5 s) is set on every connection so the loser waits and
  retries rather than erroring; the sync thread commits once at the very end, so
  the write window is small. This holds only under a **single WSGI worker**
  (Section 14) — multiple workers would each have their own threads and no shared
  guard.

## 13. Testing strategy

- **`picker`** — the engine RFC's strategy, unchanged in spirit: `pytest` with a
  seeded/unseeded `random.Random`; deterministic exact-output tests, invariants
  (never exceeds count, no dupes, `[]` on empty, unmapped-style always fits), and
  the statistical test that a never-played release is drawn meaningfully more
  than a recently-played one. `matching` and `draw` tested independently.
- **`recommendations`** — the facade over a temp SQLite DB: generate, regenerate
  (FR-9), recency/session exclusion, draw-order preservation, thin-pool (1–2)
  results, all three `EmptyReason`s, and window get/set.
- **`sessions` / `records` / `moods`** — recency, exclusion, retirement,
  condition, affinity-map validation, and `for_time` coverage across the full
  24-hour clock (including the post-midnight wrap).
- **`sync`** — mock the Discogs client; assert the vinyl filter, `master_id`
  grouping + no-master fallback, retirement diff, the run guard, and failure
  isolation (a partial fetch writes nothing).
- **`app`** — Flask test-client smoke tests for the session, log-play, and sync
  flows.

## 14. Deployment

A `Dockerfile` builds the app image; `docker-compose.yml` mounts a named volume
at `DATA_DIR` (SQLite file plus `covers/`) and supplies env configuration, and
pins the app to a **single WSGI worker** (e.g. `gunicorn -w 1`, threaded for
request concurrency) — the in-process sync thread and its run guard assume one
process. One `docker compose up` runs it alongside other self-hosted services.
No authentication — the LAN is the trust boundary (NFR-2); no external call
anywhere except a user-triggered sync (NFR-4).
