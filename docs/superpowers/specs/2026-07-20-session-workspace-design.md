# Session Workspace — Design Spec

**Date:** 2026-07-20

**Status:** Approved design, pre-implementation.

**Builds on:** the web layer (Phase 6), stacked on `phase-6-web`.

**Related documents:** [`mvp.md`](/docs/prd/mvp.md),
[`technical-architecture.md`](/docs/rfc/technical-architecture.md),
[`recommendation-engine-design.md`](/docs/rfc/recommendation-engine-design.md)

## 1. Motivation

Real use of the Phase 6 UI surfaced four things, none of which need an
architectural change; the data model already tracks everything the UI should
show. The session already persists its recommendation batch and its plays, and
the facade can already re-read the active batch. The gaps are that the UI does
not surface that state, and that regenerate is all-or-nothing.

The feedback, and its disposition:

1. Triggering a sync gave no on-screen sign of progress or completion. **A bug.**
2. A recommendation could not be logged from the recommendations screen; it
   meant navigating to a separate Log screen and finding the copy by hand.
3. After navigating away there was no way back to the current recommendations;
   Home only offered to start a new session. The recommendations and the plays
   logged should persist and stay visible until a new session is explicitly
   started.
4. Regenerate replaced the whole list. Some picks are worth keeping while the
   rest are reshuffled.

These cohere into one idea: **the session is a persistent workspace** holding the
recommendations (pinnable, loggable) and the plays logged into it, until a new
session is explicitly started. A separate Log screen then has no reason to
exist, because logging always targets the current session, which is now the
screen you are already on.

## 2. Scope

In scope:

- Fix the sync completion signal.
- Extend the `recommendations` facade so regenerate can keep chosen picks.
- Make `/session` the persistent workspace: recommendations, an on-demand
  search-to-log, and the session's logged plays.
- Log a recommendation in place, choosing the copy inline when more than one is
  owned.
- Remove the dedicated Log screen.

Out of scope:

- No schema change and no migration. Pinning is ephemeral (Section 5).
- No change to `records`, `moods`, `sessions`, `picker`, or `sync`'s storage.
- The Condition, Settings, and Sync screens are unchanged except that "Log"
  leaves the navigation.

## 3. Sync completion signal (bug fix)

**The bug.** `trigger()` acquires the run lock, spawns the background thread,
and redirects to `/sync`. The `sync_run` row that drives the progress poll is
created inside the thread, in `_run`, *after* a network round-trip to fetch the
folder. So `/sync` renders before the row exists, `sync.latest()` returns the
previous run (nothing, on a first sync), the status partial shows "never synced"
and carries no polling trigger, and the page never learns the sync ran. The sync
itself completes correctly in the background.

**The fix.** Create the `sync_run` row synchronously in `trigger()`, before
spawning the thread:

- `trigger(client=None)` acquires the lock, calls `_open_run` to insert a
  `running` row with `total = 0` (the real count is not known until the network
  call), spawns the thread with the new `run_id`, and returns.
- The thread sets `total` from the collection once it has it, then walks as
  before.
- `_run(collection, run_id=None, fetch=...)` gains an optional `run_id`. When
  `None` (direct/test calls) it opens its own run as today; when supplied (from
  `trigger`) it uses it and sets `total` at the start.
- `_run_and_release(run_id, client)` marks the run failed as a backstop if
  `_make_collection` raises before `_run` can, and releases the lock in a
  `finally`. `_run` still marks its own failures; the double-mark is idempotent.

No schema change. The existing sync tests keep passing because `run_id` defaults
to `None`.

## 4. Facade: `generate` keeps pinned picks

The engine and persistence are unchanged. Only the facade's `generate` grows a
parameter.

```python
def generate(db, session_id, now, keep=frozenset(), rng=None) -> RecommendationResult
```

`keep` is a set of release ids the caller wants carried into the new batch. The
behaviour:

- The kept releases are those in `keep` that are still in the recommendable pool
  this generate builds; a pinned release that has since been retired or marked
  not-playable is dropped, exactly as `active` already drops a vanished pick.
- The draw fills the remaining slots. `count` is still 5; the draw requests
  `5 - len(kept)` and its exclusion set is the existing one (recency window,
  this session's played releases, this session's already-shown releases) plus
  the kept ids, so the unpinned slots get genuinely fresh picks and never a
  duplicate of a kept one.
- The new batch is the kept releases, in the order they were passed (which is
  their display order), followed by the newly drawn releases, persisted with
  `position` as today. This replaces the active batch.
- `reason` is set only when the whole result is empty. If `keep` is non-empty the
  result is non-empty and `reason` is `None`. The empty-reason selection is
  unchanged from the current three-way rule.

`keep` defaults to an empty set, so the existing first-generate and plain-
regenerate calls (and every existing test) behave exactly as before.

## 5. The session workspace

`/session` becomes the primary screen for an active session. Pinning is
ephemeral: the "keep" state lives only in the regenerate form submission, never
in the database, per the approved design.

### 5.1 Navigation and Home

- Navigation becomes **Home · Condition · Settings · Sync**. "Log" is removed.
- `GET /` (`home`):
  - No current session → the start-a-session form (mood pre-selected by
    `moods.for_time`), or the empty-collection prompt (FR-19) when there are no
    records.
  - A current session exists → redirect to `/session`.
  - `GET /?new=1` always shows the start form, even with a session active. This
    is the target of the workspace's "Start a new session" control, and the
    `new` flag is what prevents the redirect from looping.

### 5.2 The workspace, top to bottom

`GET /session` renders three sections for the current session:

1. **Recommendations.** The active batch (`recommendations.active`) minus any
   release already played this session (Section 6), each pick carrying a "keep"
   checkbox and a Log control. A single "Regenerate unpinned" button submits the
   checked release ids as `keep`. The FR-10 explained-empty message shows here
   when the batch is empty.
2. **Log something else.** A search box (`GET /session?q=…`) that renders results
   **only when a query is submitted** — there is no default full-collection
   list. Each result carries the same Log control as a pick. Logging preserves
   the query so several records can be logged from one search.
3. **Logged this session.** `sessions.plays` for the current session, joined to
   `records` for titles, each with a Remove control (FR-12b).

If there is no current session, `/session` redirects to `/` (which shows the
start form).

### 5.3 Routes

Removed: `GET /log`, `POST /log/play`, `POST /log/remove/<id>`.

Added or changed:

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Redirect to `/session` when a session is active (unless `?new=1`) |
| `/session` | GET | Workspace; `?q=` adds the search-results section |
| `/session/start` | POST | Start a session and generate (unchanged) |
| `/session/regenerate` | POST | Regenerate, carrying `keep` from the checked picks; htmx-swaps the recommendations panel |
| `/session/log` | POST | Log `release_id` + `instance_id` into the current session; redirect to `/session`, preserving `?q=` |
| `/session/remove/<play_id>` | POST | Remove a play from the current session; redirect to `/session` |

Regenerate stays htmx (it swaps only the recommendations panel). Search is a
plain GET that reloads the workspace with a results section; the recommendations
survive because they are server-side state (the active batch). Logging and
removing are plain post-and-redirect. This keeps htmx to the two interactions
that earn it, consistent with the Phase 6 decision.

### 5.4 Logging a pick, and the inline copy picker

A Log control posts `release_id` and `instance_id` to `/session/log`.

- A release with a single owned copy logs in one click; the copy's
  `instance_id` is a hidden field.
- A release with more than one owned copy reveals an inline copy picker (radio
  buttons over its instances) before confirming, so the choice never leaves the
  workspace. This is the same instance-choice logic the removed Log screen had,
  now rendered on the pick and the search-result rows.

Logging requires a current session; every path that offers a Log control is
already inside one, so this is not a reachable error state, but `/session/log`
still guards against a missing session defensively.

## 6. Logged picks leave the recommendations list

When a pick is logged it should move from "Recommendations" to "Logged this
session" immediately, without a regenerate. This is a **view-layer filter**, not
a change to the stored batch:

```
recommendations shown = active batch releases − releases played this session
```

The active batch is untouched. A release played this session is filtered out of
the displayed recommendations, and `generate`'s existing exclusion keeps it out
of the next regenerate too. No new state, no facade change beyond Section 4.

## 7. Data model

Unchanged. No new tables, no new columns, no migration. Pinning is ephemeral
form state (Section 5). The recommendation batch, its `position`, the plays, and
the `sync_run` row are exactly as they are today.

## 8. Revisions to the PRD

Recorded here in the spirit of architecture RFC §8, so the change is a stated
decision rather than a silent divergence:

- **FR-12a becomes search-only for logging.** The PRD says "browse or search the
  collection to find and log a specific record." Logging is now search-only: the
  workspace shows results on submit rather than a full list. Browsing the full
  collection still exists on the Condition screen, where seeing every record to
  toggle playability is the point, so `records.browse` and the full-list view
  are not lost — only removed from the logging path.
- The dedicated Log screen named implicitly by FR-11/FR-12 is folded into the
  session workspace. Every FR it carried (FR-11 log, FR-12a search + copy
  choice, FR-12b remove) remains reachable, now without leaving the session.

The architecture RFC §10 web-layer description should gain a sentence that
`/session` is the session workspace and that there is no separate log screen;
this lands with the implementation.

## 9. Testing

- **Facade (`generate` with `keep`).** Pinned releases survive a regenerate;
  unpinned slots are refilled with fresh draws up to five; a kept release that
  left the recommendable pool is dropped; `keep` never produces a duplicate; an
  empty `keep` reproduces today's behaviour.
- **Sync (regression guard for Section 3).** The `sync_run` row exists and reads
  `running` the instant `trigger()` returns, before the background thread has
  done its network fetch. The existing failure-isolation and lock tests still
  pass.
- **Web.** Logging a pick in place (single-copy one click, multi-copy via the
  inline picker); a logged pick leaves the recommendations and appears under
  "Logged this session"; search shows results only after submit and logs from a
  result; regenerate keeps checked picks and replaces the rest; Home redirects
  to `/session` with a session active and honours `?new=1`; the Log routes are
  gone (a request to `/log` is a 404).

## 10. What this does not do

- No persistent pins. Ticking a keep box and reloading without regenerating
  loses the ticks, by design; the recommendations themselves persist because the
  batch is stored.
- No auto-refill on log. Logging a pick shrinks the visible list; the freed slot
  is filled on the next regenerate, not automatically.
- No full-collection browse in the logging path (Section 8).
- No schema change.
