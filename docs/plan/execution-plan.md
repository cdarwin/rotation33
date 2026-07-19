# Rotation33 Technical Execution Plan

**Date:** 2026-07-19

**Status:** Draft. The architecture it builds on is settled and merged in
[PR #1](https://github.com/benpencodes/rotation33/pull/1).

**Related documents:** [`mvp.md`](/docs/prd/mvp.md),
[`technical-architecture.md`](/docs/rfc/technical-architecture.md),
[`recommendation-engine-design.md`](/docs/rfc/recommendation-engine-design.md),
[`discogs-collection-shape.md`](/docs/research/discogs-collection-shape.md)

## 1. Scope

This is the build plan for the MVP: how the architecture in
[`technical-architecture.md`](/docs/rfc/technical-architecture.md) gets turned
into running code, in what order, with what proof at each step.

It covers sequencing and parallelism, the exit criteria for each phase, the test
and CI harness, and a requirements traceability matrix showing every FR and NFR
lands somewhere. It does not re-specify component interfaces; the architecture
RFC owns those and stays the source of truth.

Section 3 records eight design issues found while reading the RFC against what it
would take to implement it. All eight are now resolved in the RFC itself.

## 2. Ground rules

- **The architecture RFC is the contract.** If the build disagrees with it, the
  RFC gets amended in a PR, not silently diverged from. Section 3 is the first
  application of that rule.
- **A phase is done when its tests pass, not when its code exists.** Exit
  criteria below are written as observable facts, not as "implemented X."
- **No phase depends on a live Discogs token except Phase 5.** Everything else
  builds against the captured fixture from Phase 1b. This is deliberate: it keeps
  the critical path off an external service and makes the suite runnable in CI.
- **Vertical slices over horizontal layers.** The milestone after Phase 4 is a
  real recommendation from a real collection, drawn through the real facade, with
  no UI. Getting there early is worth more than a polished template.

## 3. Design issues settled

These came out of reading the RFC against what it would take to implement it.
D1, D2, D5, and D8 were defects rather than preferences, and D8 was a
contradiction that would have stalled Phase 5.

All eight are resolved in the architecture RFC. They are kept here as the record
of what changed and why: the RFC states the decisions, and this section is where
the reasoning behind them lives.

### D1. The `datetime.min` staleness sentinel is timezone-fragile

Architecture §5.4 step 3 computes `now - recency.get(r.id, datetime.min)`. Two
problems.

`infra.now()` is described (§2) as timezone-configurable via `TZ`. If it returns
an aware datetime, `now - datetime.min` raises `TypeError: can't subtract
offset-naive and offset-aware datetimes`, and it raises on the never-played path
only, which is the common path on a fresh collection. If it returns naive local
time the expression works, but nothing in the RFC pins that down.

Second, this resolution traded a clear sentinel for arithmetic that hides one.
The original review comment was about the `.get(..., ...)` default being
unreadable, not about `timedelta.max` being wrong. A branch fixes the readability
without introducing date arithmetic:

```python
NEVER = timedelta.max
staleness = now - last if (last := recency.get(r.id)) else NEVER
```

`timedelta.max` is only ever sorted on, never added to a datetime, so it is safe
in `draw`. **Resolved:** revert to the explicit sentinel, and state in §2
that `infra.now()` returns naive local time in the configured zone.

### D2. The sync lock leaks on crash, and `sync_run` orphans on restart

Architecture §9 has the trigger thread release the module lock "when the thread
finishes." If the thread raises before that point, the lock is held for the life
of the process and sync is permanently dead until a restart, with no user-visible
explanation.

Separately, the lock guards concurrency but not state. A process killed mid-sync
leaves a `sync_run` row at `running` forever, so the UI polls a progress bar that
never completes.

**Resolved:** release the lock in a `finally` inside the thread body, and
add a startup reconciliation in the app factory that marks any `running`
`sync_run` as failed. Both are a few lines; neither is optional.

### D3. Alembic "before first real data" is already too late

Architecture §2 and §7 defer migrations until "the first deployment that holds
history worth keeping." The first sync produces real data on day one, and play
history is the one thing in this system that cannot be regenerated: records can
be re-synced from Discogs, plays cannot be recovered from anywhere.

**Resolved:** baseline Alembic in Phase 0, alongside the first table. The
cost is one `alembic init` and a generated revision per schema change. The cost
of retrofitting once there is a month of listening history is a hand-written
migration under pressure.

### D4. Views are told not to orchestrate, then told to orchestrate

Architecture §3 says the view layer "hands off to a facade rather than accreting
orchestration logic." Then §5.3 has the view join `sessions.plays` to `records`
to build the session log, and §10 has the settings view diff `records.styles`
against `moods.affinity_map` for the FR-18 review list. Both are the view
composing across two components.

This is fine in practice and the rule is what is wrong. **Resolved:**
restate the rule as "a view may join across components for display, but may not
implement a rule." Anything that decides something goes in a facade. Under that
wording both examples are legal and the boundary stays meaningful.

### D5. Auto-commit on teardown commits failed requests

Architecture §10 says the view "commits and closes on teardown." A teardown
handler runs after an exception too, so a request that raised halfway through a
multi-step write commits the half it finished.

**Resolved:** commit explicitly in the view on the success path; the
teardown handler rolls back if the session is still dirty, then closes. Flask's
`teardown_appcontext` receives the exception, so the check is direct.

### D6. Store the pressing release id unconditionally

Architecture §7 says the ORM "may also stash the pressing `release_id` per
instance for a view-on-Discogs link." The PRD (§7) and RFC (§8) both accept a
known risk: Discogs master reassignment can re-key an album on a later sync and
split its recency history.

That pressing id is the only durable handle for reconciling such a split after
the fact. It is one integer, free from the listing payload.

**Resolved:** make it a required column, not an optional one, and say why.
It is cheap insurance against the one accepted data-loss risk in the design.

### D7. NFR-3 is one sentence and a real workstream

Responsive across phone, tablet, and desktop (NFR-3) appears as a parenthetical
in §10. With no build step and no framework, that is hand-written CSS across
three breakpoints for every screen, and it is the phase most likely to overrun.

**Resolved:** the approach is settled in Phase 0 rather than discovered in Phase
6: a single hand-written stylesheet, mobile-first, CSS custom properties for the
palette, flexbox and grid only, no preprocessor. It is budgeted per screen inside
Phase 6 rather than treated as free.

### D8. Progress reporting contradicts the single-transaction rule

This one would have blocked Phase 5.

Architecture §9 step 1 advances `sync_run.processed` per page so the htmx bar can
poll it. §9 failure isolation says the commit happens "only after a complete,
successful fetch," and §12 says "the sync thread commits once at the end."

Both cannot hold on one session. If the sync runs in a single transaction, the
progress updates are uncommitted and invisible to the polling request, which
reads on a different connection. The progress bar sits at zero until the sync
finishes, then jumps to complete. If progress is committed as it goes, the sync
is no longer one transaction and partial state can survive a failure, which is
what the isolation rule exists to prevent.

**Resolved:** split them. The sync thread holds two sessions: a data
session that stays open across the whole fetch and commits once at the end, and a
short-lived progress session that opens, updates `sync_run`, commits, and closes
per page. `sync_run` is metadata about the run, not collection data, so
committing it early violates nothing. Write this into §9 explicitly, because the
naive implementation is the broken one.

## 4. Sequencing

### Dependency graph

```
infra ───┬── records ──┬── recommendations ── app
         ├── moods ────┤
         └── sessions ─┘
                        picker (no deps) ──┘
records ── sync ────────────────────────────┘
```

The critical path is `infra -> records -> recommendations -> app`. Everything
else hangs off it and can move in parallel.

### Strategy

Two independent leaves carry most of the project's risk, and neither is on the
critical path:

- **`picker`** is the algorithm risk. It imports nothing, touches no database,
  and takes no clock, so it can be built and proven to completion on day one.
- **Discogs sync** is the external risk, and the research doc flags two edges as
  unconfirmed against live data (`master_id: 0`, and a single release listing
  both Vinyl and CD).

Retire both early. Build `picker` first because it is pure and fast, and capture
a real collection payload to a fixture at the same time. The fixture turns the
external risk into a file, which then lets every later phase, including sync
itself, be built and tested offline.

The middle phases are plumbing over a settled interface. They are low-risk and
mostly mechanical, which is why the plan spends its early effort elsewhere.

### Parallelism

| Wave | Runs concurrently |
|---|---|
| 1 | Phase 0 (scaffolding) |
| 2 | Phase 1 (`picker`), Phase 1b (fixture capture) |
| 3 | Phase 2 (`records`), Phase 3 (`moods`, `sessions`) |
| 4 | Phase 4 (`recommendations`) |
| 5 | Phase 5 (`sync`), Phase 6a (templates and CSS shell) |
| 6 | Phase 6b (screens), Phase 7 (Docker) |
| 7 | Phase 8 (hardening) |

Phase 3's two modules are independent of each other and of `records`; all three
only need `infra`.

## 5. Phases

Sizes are relative: S is a sitting, M is a day, L is multiple days.

### Phase 0: Scaffolding (M)

Everything that makes later phases boring.

- `pyproject.toml`: Flask, SQLAlchemy, python3-discogs-client, Alembic, gunicorn;
  dev extras pytest, pytest-cov, ruff.
- `ruff` configured for lint and format. One command, `ruff check --fix && ruff
  format`, no second formatter.
- `infra.py`: `Base`, `engine`, `SessionLocal`, env config
  (`DISCOGS_TOKEN`, `DISCOGS_USERNAME`, `TZ`, `DATA_DIR`), `now()`. SQLite
  pragmas applied per connection: `journal_mode=WAL`, `busy_timeout=5000`,
  `foreign_keys=ON`.
- Alembic baselined (D3), with `env.py` importing every component so autogenerate
  sees the full metadata.
- `pytest` harness: a `tmp_path` SQLite fixture giving each test a fresh schema,
  and a `db` session fixture. This fixture is used by every phase after this one,
  so it is worth getting right now.
- CI: GitHub Actions running ruff and pytest on push. No Discogs token in CI.
- `Dockerfile` and `docker-compose.yml` in skeleton form, building and starting a
  hello-world Flask app against a named volume. Standing this up now means Phase
  7 is a change rather than a discovery.
- CSS approach decided per D7.

**Exit:** `pytest` passes with zero tests, `ruff check` is clean, `docker compose
up` serves a page, `alembic upgrade head` creates an empty database, and CI is
green on a pull request.

### Phase 1: `picker` (M)

The pure engine. No `db`, no clock, no imports from other components.

- `Candidate` and `Affinity` dataclasses.
- `matching`: best-style-wins fit (FR-8), unmapped styles eligible everywhere
  (FR-18).
- `draw`: rank by staleness descending, linear rank weighting, weighted sampling
  without replacement via pop-and-redraw, `rng` injected and defaulting to a
  module `Random()`.

Tests are the deliverable here as much as the code, per the engine RFC:

- Deterministic: seeded RNG, exact ids from a fixed pool.
- Invariants: never exceeds `count`, never duplicates, `[]` on empty pool, fewer
  than `count` on a thin pool without raising, never returns a candidate that
  failed `matching`, a candidate whose only style is unmapped always survives
  `matching`.
- Statistical: a never-played release and one played yesterday, both fitting;
  several hundred unseeded draws; assert the never-played one wins meaningfully
  more often (>2x). This is the only test that catches an inverted weight.

**Exit:** all three test classes pass. `picker.py` imports nothing from the
project. NFR-5 is demonstrably satisfied: the module's only coupling is its two
function signatures.

### Phase 1b: Discogs fixture capture (S, parallel with Phase 1)

Run a read-only collection walk against the real account and serialize the raw
listing payload to `tests/fixtures/collection.json`. Hand-extend it with the two
shapes the research doc could not confirm: a `master_id: 0` item, and a release
listing both `Vinyl` and `CD` formats.

Sanitize before committing: the payload carries no secrets, but confirm no token
or username is embedded.

**Exit:** the fixture exists, is committed, and a smoke test loads it and asserts
it contains at least one multi-instance master, one no-master item, one non-vinyl
item, and one multi-format item. Phase 5 is now buildable without a token.

### Phase 2: `records` (M)

First component with storage, so it also establishes the `_Mapper` pattern that
every other component copies. Worth reviewing carefully for that reason.

- Private `_ReleaseRow` and `_InstanceRow`, private `_Mapper`, public functions
  per §5.1.
- Namespaced ids: `m<master_id>`, falling back to `r<release_id>` when
  `master_id in (0, None)`.
- Pressing release id stored per instance (D6).
- `styles` as a JSON column, not a join table.
- `reconcile_retirements` as a whole-collection set difference.

Tests: `recommendable` excludes retired and non-playable but keeps pending;
`browse` and `search` do not filter on playability (FR-13a); search is
case-insensitive across artist and title; `upsert` never touches `is_playable` or
`retirement_status` on an existing row (FR-2); `reconcile_retirements` flags
absent instances pending and flips reappeared ones back to active (FR-2a);
`set_playable` raises `UnknownInstance` on a bad id.

**Exit:** the above pass, and the `_Mapper` boundary holds: no ORM row type is
reachable from outside the module.

### Phase 3: `moods` and `sessions` (M, two parallel tracks)

**`moods`.** Five moods as a code constant with name and time window. Persisted
overrides only, defaults from code, no seeding step (FR-17). Built-in affinity
map covering the styles named in PRD §7. `for_time` tiling the full 24 hours with
After Dark wrapping past midnight. `set_affinity_map` validating floats in
`[0, 1]` and known mood names at the write boundary.

Tests: `for_time` across all 24 hours including the post-midnight wrap; a read
with no override returns the code default; an override wins; invalid affinity
values and unknown mood names are both rejected loudly.

**`sessions`.** `Session` and `Play`, uuid4 ids minted by the write functions,
`Play` carrying a denormalized `release_id`.

Tests: `start` always creates a new session and the prior one stops being
`current`; `current` returns `None` only on a virgin database; `latest_plays`
returns one entry per release, the most recent; `remove_play` refuses a play from
a non-current session and immediately shrinks `latest_plays` (FR-12b); a retired
instance's plays still contribute to release recency (FR-2a).

**Exit:** both suites pass. FR-17 is satisfied with no seed script anywhere in
the repo.

### Phase 4: `recommendations` facade (M)

Where the system becomes real. Wires all four dependencies.

- `generate` per §5.4: affinity lookup, pool build, candidate mapping, `matching`,
  recency and session exclusion, `draw`, reason selection, batch persistence with
  `position`.
- Never-played staleness handled per D1.
- `active` re-hydrating the latest batch by `position`, dropping releases that
  have since vanished.
- `window` and `set_window` with a 3-day code default (FR-14).

Tests over a temporary database: generate produces up to five; regenerate
excludes releases already shown or played this session (FR-9); the recency window
excludes at 2d23h and admits at 3d1h; draw order survives the persist and
re-hydrate round trip; a thin pool yields one or two picks with no
`EmptyReason`; all three `EmptyReason` values are reachable and correctly
distinguished; an unknown `session_id` raises rather than returning empty; a
release retired between `generate` and `active` is dropped from the re-hydrated
batch.

**Exit:** the suite passes, and a scripted end-to-end run over the Phase 1b
fixture data produces a real recommendation from a real collection. This is the
project's first genuine milestone; everything before it is scaffolding and
everything after it is delivery.

### Phase 5: `sync` (L)

Highest-variance phase, which is why the fixture exists.

- Trigger acquiring a non-blocking module `threading.Lock`, released in a
  `finally` (D2).
- Startup reconciliation marking orphaned `running` runs as failed (D2).
- Two sessions in the thread: long-lived data session committing once at the end,
  short-lived progress session committing `sync_run` per page (D8).
- Vinyl filter: keep an instance if any format name is `Vinyl`.
- Grouping by resolved release identity, with the no-master fallback.
- Cover art downloaded to `DATA_DIR/covers`, named by release id, when the file
  is missing or `cover_source_url` changed. A single failure is logged and
  skipped, never fatal.
- `reconcile_retirements` called once, inside the data transaction, only on a
  complete fetch.

Tests, all against a mocked client fed by the fixture: the vinyl filter drops the
CD and keeps both vinyl pressings of the shared master; a multi-format
Vinyl-plus-CD release is kept; `master_id: 0` falls back to `r<id>`; the
retirement diff flags absent and un-flags reappeared; the lock guard means a
second concurrent trigger no-ops; a mid-fetch exception leaves the collection
untouched and marks the run failed; a crash-simulated `running` row is reconciled
on startup; sync never modifies plays, condition flags, or session history.

**Exit:** the suite passes, then one live sync against the real collection
succeeds and confirms the two edges the research doc flagged as unproven. Update
the research doc with what the live run showed.

### Phase 6: Web layer (L)

Split into a shell and the screens.

**6a: shell.** App factory, request session lifecycle per D5, base template, the
stylesheet decided in Phase 0, htmx wired, static cover serving from `DATA_DIR`.

**6b: screens.**

| Screen | Requirements |
|---|---|
| Home | FR-3 mood pre-select, FR-19 empty-collection prompt, current session |
| Session | FR-4 picks with FR-6 artwork, FR-9 regenerate via htmx swap, FR-10 explained empty |
| Log play | FR-11, FR-12, FR-12a browse and search with instance choice, FR-12b remove |
| Condition | FR-13 playable toggle |
| Settings | FR-14 window, FR-15 descriptions, FR-16 affinity map textarea, FR-18 review list |
| Sync | FR-1 trigger, htmx progress poll, FR-2a retirement confirmation list |

Responsive work (NFR-3) is a task per screen, not a pass at the end.

Tests: Flask test-client smoke coverage of the session, log-play, and sync flows.
Not exhaustive; the logic is tested underneath, and these guard the wiring.

**Exit:** every FR is reachable through the UI, and every screen is usable at
phone, tablet, and desktop widths.

### Phase 7: Deployment (S, parallel with Phase 6b)

Fill in the Phase 0 skeleton: real image, gunicorn with `-w 1` and threads,
named volume at `DATA_DIR`, env configuration, `alembic upgrade head` on start.

**Exit:** running on the LAN from a clean `docker compose up`, reachable from a
phone, data surviving a container restart.

### Phase 8: Hardening (M)

- One full week of real daily use, which is the only test that finds what the
  suite cannot.
- Coverage review, filling gaps the phase suites left.
- A second live sync exercising a real retirement.
- README updated from "design phase" to real setup instructions.
- Backup story documented: the SQLite file and `covers/` on one volume, so a
  volume snapshot is the whole backup.

## 6. Testing and CI

- **`pytest`, stdlib `random`, no `hypothesis`.** Consistent with the engine RFC.
- **A fresh SQLite database per test** via the Phase 0 fixture. No shared state,
  no ordering dependency, no mocking of the ORM. The database is fast enough to
  be real.
- **Discogs is mocked everywhere except one live check in Phase 5.** The fixture
  is the contract.
- **CI runs ruff and pytest on every pull request** with no secrets configured,
  which structurally enforces the offline-testability rule.
- **Coverage is a signal, not a gate.** The interesting assertions here are
  behavioral, and a percentage will not tell you whether the inverted-weight test
  exists.

## 7. Requirements traceability

Every requirement maps to a phase. Multiple phases mean logic and UI are split.

| Req | Phase | Req | Phase |
|---|---|---|---|
| FR-1 | 5, 6b | FR-12a | 2, 6b |
| FR-2 | 5 | FR-12b | 3, 6b |
| FR-2a | 2, 5, 6b | FR-13 | 2, 6b |
| FR-3 | 3, 6b | FR-13a | 2, 6b |
| FR-4 | 4 | FR-14 | 4, 6b |
| FR-5 | 4 | FR-15 | 3, 6b |
| FR-6 | 2, 5, 6b | FR-16 | 3, 6b |
| FR-7 | 1 | FR-17 | 3 |
| FR-8 | 1 | FR-18 | 1, 2, 6b |
| FR-9 | 4 | FR-19 | 6b |
| FR-10 | 4, 6b | NFR-1 | 0, 7 |
| FR-11 | 3, 6b | NFR-2 | 7 |
| FR-12 | 6b | NFR-3 | 6a, 6b |
| | | NFR-4 | 2, 5, 7 |
| | | NFR-5 | 1 |

NFR-2 is a decision, not a task: nothing is built, and the compose file binds to
the LAN interface only.

## 8. Risks

| Risk | Likelihood | Impact | Response |
|---|---|---|---|
| Live Discogs data breaks the sync mapping | Medium | High | Fixture in Phase 1b; live confirmation gated as Phase 5 exit, not discovered in Phase 8 |
| Sync regresses to one session, killing the progress bar | Low | Medium | D8 settled in the RFC; the Phase 5 suite asserts progress is readable from another connection mid-fetch |
| Responsive CSS overruns Phase 6 | Medium | Medium | D7: approach decided in Phase 0, styling budgeted per screen |
| Discogs master reassignment splits recency history | Low | High | Accepted in PRD and RFC; D6 keeps the pressing id so a future reconciliation is possible |
| SQLite write contention under sync | Low | Low | WAL plus `busy_timeout`, single writer, small write window; single worker enforced in compose |
| Affinity map defaults produce poor early recommendations | Medium | Low | FR-16 makes it editable; Phase 8 real use is when it gets tuned |

## 9. What this plan does not do

- No pool-exhaustion fallback. A thin pool returns fewer picks, per the engine
  RFC's deferrals.
- No `Protocol` or ABC for the engine. One implementation, module boundary is the
  contract.
- No scheduled sync, no Discogs write-back, no auth, no multi-user. All PRD
  non-goals.
- No admin UI beyond the FR-16 textarea.
- No pruning of recommendation batch rows. Acceptable at single-user scale, per
  architecture §7.
