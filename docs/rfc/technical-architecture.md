# Rotation33 Technical Architecture

**Date:** 2026-07-15

**Status:** Request For Comments

**Related documents:** [`mvp.md`](/docs/prd/mvp.md),
[`recommendation-engine-design.md`](/docs/rfc/recommendation-engine-design.md)

## 1. Scope

This document covers the system architecture around the already-specified
recommendation engine: the technology stack, code organization, data model,
Discogs sync, the web layer, and deployment.

The product requirements are settled in the PRD. The recommendation
engine's internal algorithm (fit filter plus recency-weighted draw) is settled
in the engine RFC and is not re-derived here; Section 8 records the few places
that RFC evolves to fit this architecture.

## 2. Technology stack

| Concern | Choice | Why |
|---|---|---|
| Backend | **Flask + Jinja** | Minimal, server-rendered, one process. Covers a single-user LAN app with no async ceremony. |
| Interactivity | **htmx** | Partial HTML swaps for log-play, regenerate, and sync progress. No build step, no SPA, no JSON API split. |
| Datastore | **SQLite (WAL mode)** | Single file on the data volume, zero-config, trivial backup. WAL lets the sync thread write while the UI reads. |
| ORM | **SQLAlchemy** | Real relations map naturally; hairy reads can drop to hand-written SQL. |
| Discogs | **python3-discogs-client** | Handles auth, pagination, and rate-limit backoff so we don't own that code. |
| Background work | **In-process `threading.Thread`** | One sync at a time for one user. No Celery/Redis. Progress tracked in a DB row, polled by htmx. |
| Time | **Configurable timezone, local calendar days** | `TZ` env var; "today," recency, rollover, and mood pre-select all reason in local time. |
| Migrations | **`create_all()` now, Alembic before first real data** | No migration ceremony while the schema churns pre-launch; adopt versioning the moment there is history worth protecting. |
| Cover art | **On disk, path in DB** | Images cached to the data volume during sync; DB holds the path. Keeps the DB small, served as static files. Required by NFR-4 (no external call in the daily path). |
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

The **view layer** (`app.py`) owns the request-scoped database session, holds
only dataclasses, and stays thin — it hands off to a facade rather than
accreting orchestration logic.

Because each component's ORM models are private, a facade composes the other
components through their **public dataclass functions**, not by joining across
their tables. Cross-component relationships at the database level are expressed
as string-named foreign keys (`ForeignKey("instance.id")`), which resolve at
DDL time against the shared metadata without creating a code dependency.

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
infra.py                                                  (Base, engine, sessionmaker, env, clock)
```

| Module | Owns | Responsibility |
|---|---|---|
| `infra.py` | `Base`, `engine`, `SessionLocal`, env config, `today()`/`now()` | The one shared SQLAlchemy bootstrap and cross-cutting infrastructure. No domain models, no domain logic. |
| `records.py` | Release, Instance | Collection catalog: metadata, condition (`is_playable`), retirement state. Upsert (for sync), the playable pool, search/browse. |
| `moods.py` | Mood, Style Affinity, Mood Description | The five moods, their editable descriptions and style→mood affinity map, and each mood's soft time-of-day default. |
| `sessions.py` | Session, Play | A sitting and the plays logged into it. Release-level recency and same-day / same-session exclusion reads. |
| `recommendations.py` | Recommendation | **Facade.** Produces, persists, and reads a session's recommendations (Section 7). |
| `picker.py` | Candidate, Mood (engine view) | **Pure, swappable engine.** Fit filter + recency-weighted draw. The one public surface that takes no `Session`. |
| `sync.py` | SyncRun | Background Discogs sync: fetch, upsert via `records`, cache art, flag retirements, report progress. (Unmapped styles are derived on demand, not flagged here — see Section 5.) |
| `app.py` | — | Flask app factory, request session lifecycle, routes/blueprints, rendering. Promotes to `app/` if it strains. |

## 5. Data model

Tables, grouped by owning component. Foreign keys across components are
string-named and resolve against the single `Base` metadata in `infra.py`.

**`records.py`**

- `release` — `discogs_release_id`, `artist`, `title`, `year`, `notes`,
  `styles` (JSON list of style strings), `cover_path`. Recency is reasoned at
  this level, but not stored here (see `play`).
- `instance` — `release_id` (FK), `discogs_instance_id`, `is_playable` (bool,
  FR-13), `retirement_status` (`active` | `pending_retirement` | `retired`,
  FR-2a). Condition and plays attach to the instance, not the release.

Styles are a JSON list rather than a relational table: the affinity map is
keyed by style string, so a join table earns nothing.

**`moods.py`**

- `mood` — `name`, `description` (editable, FR-15), `time_default_start`
  (soft pre-select window). Seeded on first run (FR-17).
- `style_affinity` — the nested `{style: {mood: affinity∈[0,1]}}` map, stored as
  JSON (FR-16). Validation is limited to "floats in [0,1]"; the single editor
  hand-edits it (RFC).

Styles absent from the affinity map (FR-18) are **derived on demand** (distinct
release styles minus affinity-map keys), not stored in a table.

**`sessions.py`**

- `session` — `mood` (name), `started_at`, `date` (local start date).
- `play` — `instance_id` (FK), `session_id` (FK), **`release_id`
  (denormalized)**, `played_on` (local date), `created_at`. The denormalized
  `release_id` lets `sessions` answer release-level recency from its own table
  without depending on `records`.

**`recommendations.py`**

- `recommendation` — `session_id` (FK), `instance_id` (FK), `generated_at`. A
  session's active batch is the rows with the greatest `generated_at`; all rows,
  active or superseded, count toward same-session exclusion.

**`sync.py`**

- `sync_run` — `started_at`, `finished_at`, `status`, counts, `error`. Drives
  progress polling and the "last synced" display; only the latest row is
  retained.

## 6. Public surfaces (sketch)

Illustrative, not exhaustive — enough to show the boundaries. All DB-touching
functions take a `Session`; all speak dataclasses.

```
# records.py
def playable_instances(db) -> list[Instance]        # is_playable, not retired
def search(db, query) -> list[Instance]             # off-recommendation logging
def set_playable(db, instance_id, playable: bool)   # FR-13
def upsert_release(db, dto); def upsert_instance(db, dto)   # used by sync
def mark_pending_retirement(db, instance_ids); def confirm_retirement(db, ids)

# moods.py
def get(db, name) -> Mood                            # description + affinity lookup
def default_for_time(now) -> str                     # time-of-day pre-select
def set_description(db, name, text)                  # FR-15
def affinity_map(db) -> dict; def set_affinity_map(db, mapping)   # FR-16

# sessions.py
def current(db, today) -> Session | None; def start(db, mood_name) -> Session
def log_play(db, session_id, instance_id, release_id, played_on) -> Play
def remove_play(db, play_id)                         # FR-12b, active session only
def last_played_by_release(db) -> dict[str, date]  # release_id → most recent play; drives both recency exclusion and staleness ranking
def instances_played_today(db, today) -> set[str]                  # FR-5
def session_exclusions(db, session_id) -> set[str]                 # FR-9

# picker.py  (pure — no Session)
def matching(candidates, mood) -> list[Candidate]    # fit filter (FR-8, FR-18)
def draw(matching, count, rng=None) -> list[str]     # weighted, no replacement
def pick(candidates, mood, count, rng=None) -> list[str]           # matching→draw

# recommendations.py  (facade)
def generate_for_session(db, session_id) -> RecommendationResult   # generate/regenerate
def active_batch(db, session_id) -> RecommendationResult
```

## 7. Recommendation flow

`recommendations.generate_for_session` drives both first-generate and regenerate
(FR-9); they need no special-casing between them. The view calls it and renders
the result. The facade:

1. loads the mood via `moods.get`;
2. reads `sessions.last_played_by_release` once (release_id → most recent play),
   and builds the eligible pool: `records.playable_instances`, minus releases
   whose last play is within the recency window (release-level recency, FR-4),
   minus instances in `sessions.instances_played_today` (FR-5) and
   `sessions.session_exclusions` (FR-9);
3. maps the survivors to `picker.Candidate` (instance_id, release_id, styles,
   and days-since-last-played derived from that same map — never-played
   releases rank as infinitely stale);
4. calls `picker.matching` then `picker.draw` for `count` ∈ [3, 5];
5. persists the drawn instances as a new `recommendation` batch (new rows,
   nothing overwritten);
6. on an empty result, distinguishes the FR-10 reason by comparing pool sizes:
   the fit-filtered pool (`picker.matching` over the recency-ignoring pool)
   versus the final pool tells "nothing fits this mood" apart from "everything
   that fits played recently." Rendering the message is the view's job;
   surfacing the reason is the facade's.

Composition happens in Python over public dataclass functions (Section 3), not
in one cross-table SQL query. At collection scale (hundreds to low thousands of
rows) that is a non-issue; a single-query optimization is deferred until it
measurably matters.

## 8. Relationship to the recommendation-engine RFC

The engine RFC's *idea* is intact — three responsibilities, a pure and
swappable engine, fit as a hard filter with recency driving a weighted draw. It
evolves in these mechanical ways:

- **The engine module is `picker.py`**, not `recommendation_engine/`, resolving
  the name collision with the `recommendations` facade. Swappability is
  unchanged: replace the module (and the facade's one import). No `Protocol` or
  ABC, per the RFC.
- **`pick()` splits into `matching()` + `draw()`** (with `pick` as the two
  composed), so the facade can run the fit filter alone for the FR-10 reason.
- **The RFC's "coordinator" is the `recommendations` facade**, and its "read
  model" (`eligible_candidates`) folds into that facade rather than standing as
  its own component — "candidate" is engine machinery, not product vocabulary.
- **The read model composes public functions in Python** instead of issuing one
  cross-table SQL query, because a facade may not reach into another component's
  private ORM tables.
- **`Play` carries a denormalized `release_id`** so release-level recency is a
  self-contained `sessions` query, keeping `sessions` a dependency-free leaf.

The engine RFC remains accurate on the algorithm itself and is not rewritten
here; these deltas are the reconciliation.

## 9. Discogs sync

A manual trigger (FR-1) spawns a `threading.Thread`. The thread owns its own
`Session` from `SessionLocal` for its lifetime — the one deliberate exception to
"the view owns the session," since it runs with no request context. It:

1. fetches the **full current collection** (the client library handles
   pagination and rate-limit backoff);
2. upserts release/instance metadata through `records`' public API;
3. downloads any missing or changed cover art to `DATA_DIR/covers`, recording
   the path on the release;
4. **diffs** the fetched instance set against local instances: those absent from
   Discogs are flagged `pending_retirement` (never deleted, FR-2a); reappearing
   ones flip back to `active`;
5. leaves styles absent from the affinity map to be surfaced for review (FR-18,
   derived, Section 5);
6. updates the `sync_run` row throughout for progress polling.

**Failure isolation (FR-2, FR-2a).** Metadata commit and retirement-flagging
happen only after a complete, successful fetch. A partial or failed fetch marks
`sync_run` failed and writes no retirements and no half-collection. A single
cover-art download failure is logged and skipped (the old file is kept); it does
not fail the sync. Sync never touches behavioral data — plays, condition flags,
and session history.

## 10. Web layer and htmx

Routes live in `app.py` (promoting to `app/` with blueprints if it strains).
The view opens a request-scoped session, commits/closes on teardown, and passes
only dataclasses to templates.

- **Home** — pre-selects the time-appropriate mood (`moods.default_for_time`);
  start or continue a session.
- **Session** — start (pick a mood → `recommendations.generate_for_session`);
  regenerate (htmx-swaps the picks panel); picks shown with local cover art
  (FR-6).
- **Play logging** — log an instance into the session (htmx-swaps the play
  list); browse/search the collection for off-recommendation plays (FR-12a);
  pick which instance when a release has several; remove a play from the active
  session (FR-12b).
- **Condition** — toggle `is_playable` (FR-13).
- **Settings** — recency window (FR-14); mood descriptions (FR-15); the affinity
  map as a validated JSON `<textarea>` (FR-16, no admin UI per the RFC); the
  flagged-styles review list (FR-18).
- **Sync** — trigger; a status endpoint htmx polls for progress; the
  retirement-confirmation list.

## 11. Time handling

`infra.today()` / `infra.now()` resolve against the `TZ` env var; recency,
"played today," day rollover, and mood pre-select all go through them. A
session's `date` is its local start date; the current session is today's open
one, otherwise the UI prompts a new one (a session rolls over at end of day).

## 12. Configuration and secrets

Environment only, never committed: `DISCOGS_TOKEN`, `DISCOGS_USERNAME`, `TZ`,
`DATA_DIR` (default `/data`), and the default recency window. `infra.py` reads
them and constructs the engine, sessionmaker, and `Base`.

## 13. Persistence details

- SQLite in WAL mode, one file under `DATA_DIR`.
- `Base.metadata.create_all()` runs on startup; `app.py` imports every component
  module first so their tables register against the shared metadata. Alembic is
  adopted before the first deployment that holds history worth keeping.
- The request-scoped session is created per request and torn down on response;
  the sync thread manages its own (Section 9).

## 14. Error handling

- Sync failures are isolated (Section 9): no silent retirements, no partial
  collection writes.
- FR-10 empty-pool cases surface a specific reason (Section 7) rather than an
  empty or padded set.
- Affinity-map edits validate "floats in [0,1]" before persisting (FR-16); a bad
  value only misfiles a record until corrected, per the RFC.
- WAL plus short transactions keep the sync thread and request handlers from
  blocking each other.

## 15. Testing strategy

- **`picker.py`** — the engine RFC's strategy, unchanged: `pytest` with a
  seeded/unseeded `random.Random`, including the statistical test that catches an
  inverted recency weight. `matching` and `draw` are tested independently.
- **`recommendations.py`** — the facade over a temporary SQLite DB with
  fixtures: generate, regenerate (FR-9), same-day/same-session exclusion, and
  both FR-10 reasons.
- **`sessions.py` / `records.py`** — recency, exclusion, retirement, and
  condition reads/writes against a temp DB.
- **`sync.py`** — mock the Discogs client; assert the diff/retirement logic and
  failure isolation (partial fetch writes nothing).
- **`app.py`** — Flask test-client smoke tests for the session, log-play, and
  sync flows.

## 16. Deployment

A `Dockerfile` builds the app image; `docker-compose.yml` mounts a named volume
at `DATA_DIR` (SQLite file plus `covers/`) and supplies env configuration. One
`docker compose up` runs it alongside other self-hosted services on the home
network. No authentication — the LAN is the trust boundary (NFR-2). No external
call anywhere except a user-triggered sync (NFR-4).
