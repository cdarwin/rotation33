# Discogs Collection Shape: Sync Findings

**Date:** 2026-07-17

**Status:** Research (spike-backed)

## Purpose

Resolve open questions for the technical-architecture RFC about how Rotation33
syncs a Discogs collection: what the `python3-discogs-client` library actually
hands us, and how that maps onto our domain model.

Domain mapping under test:

- Our **Release** is a Discogs **master release** (the album across pressings).
- Our **Instance** is a single owned physical copy, a Discogs **collection
  instance**.

Findings below come from a throwaway spike (`spike_discogs.py`, untracked) run
read-only against a real 73-item collection. Raw output is quoted as evidence.

## How the data is fetched

```python
client = discogs_client.Client("Rotation33Spike/0.1", user_token=TOKEN)
folder = client.user(USERNAME).collection_folders[0]  # folder 0 = "All"
items  = list(folder.releases)                         # paginated, lib backs off
```

Folder 0 ("All") aggregates every sub-folder; each item still reports its own
`folder_id`. The library paginates (about 50 per page) and handles rate-limit
backoff.

Each item is a collection instance exposing cheap, already-loaded attributes
(`id`, `instance_id`, `folder_id`, `rating`, `date_added`) plus
`item.release.data`, the Discogs `basic_information` dict, present in the listing
payload with no extra HTTP request.

## Findings

### 1. master_id is free: Release=master needs no per-item fetch

`basic_information` already contains `master_id` and `master_url`. Accessing the
lazy `item.release.master` returned the same id it already had, so the master
mapping costs nothing beyond the collection listing itself.

Also present for free in every item: `title`, `artists`, `labels`, `formats`,
`year`, `genres`, `styles`, `cover_image`, `thumb`.

Implication: sync can populate Release identity and album metadata directly from
the collection listing. No N+1 fetch per release.

### 2. Identity: Instance = instance_id, Release = master_id

- `item.instance_id` is the collection-unique per-copy holding id (around the
  2.1e9 range). It is the natural Instance primary key.
- `item.id` is the Discogs release id (a specific pressing), not stable across
  pressings of the same album.
- `master_id` groups pressings into one album. It is the correct Release
  identity.

Keying Release on the release `id` would wrongly split pressings of one album
into separate Releases. Key on `master_id`.

### 3. No-master fallback (edge case: indie self-release)

Release `37907304` ("Hookups and Heartaches", an indie self-release) has no
master:

```
master_id   = 0   ('master_id' key present: True)
master_url  = None
```

The `master_id` key is always present; its value is `0` when there is no master
(not absent). `master_url` is `None`.

Rule: treat `master_id in (0, None)` as "no master" and synthesize a Release
keyed on the release `id`. No key-presence check needed; normalize the value.

### 4. One album, multiple owned pressings (edge case: same master)

Two owned pressings of Johnny Blue Skies, "Mutiny After Midnight", collapse to a
single master:

```
release 36790993  instance 2124926800  Orange Translucent [Competition Orange]
release 36754618  instance 2124727382  Red Translucent
-> share one master_id? True  (4160569)
```

Distinct release ids, distinct instance ids, one `master_id`. In our model: one
Release, two Instances. The pressing-specific detail (color) lives in
`formats[].text`, available if Instances ever need a human-readable label.

The full-collection grouping by `master_id` caught this automatically, including
a third holding the owner did not expect (see below).

### 5. Format filter: Rotation33 is vinyl-only (edge case: mixed formats)

Discogs collections include CDs and cassettes. The Mutiny After Midnight master
turned out to have three owned instances, one of which is a CD:

```
master_id 4160569:
  release 36762976  instance 2124926875  formats=['CD']
  release 36754618  instance 2124727382  formats=['Vinyl']
  release 36790993  instance 2124926800  formats=['Vinyl']
```

Rotation33 is bound to the vinyl collection, so sync must filter on format.
`formats[].name` (`"Vinyl"`, `"CD"`, `"Cassette"`) is in the listing payload, so
the filter is cheap, no extra fetch.

Filter at the Instance level, not the master level:

- Keep an Instance if `any(f["name"] == "Vinyl" for f in basic_information["formats"])`.
- A Release exists if it has at least one kept (vinyl) Instance.

Filtering by master would either wrongly include the CD or wrongly drop the whole
album. Instance-level filtering keeps the two vinyl copies and drops the CD while
the Release (master `4160569`) survives.

Edge (not observed, cheap to handle): a single release can list multiple formats
(for example an LP-plus-CD box set, `["Vinyl", "CD"]`). The `any(... == "Vinyl")`
rule keeps those, which is the desired behavior for a vinyl collection.

## Sync design consequences

| Concern | Decision |
|---|---|
| Release identity | `master_id`; fall back to release `id` when `master_id in (0, None)` |
| Instance identity | `instance_id` |
| Album metadata | Read from `basic_information` (free); no per-release fetch |
| Multi-copy detection | Group Instances by resolved Release identity, not release `id` |
| Format scope | Keep Instance if any format name is `"Vinyl"`; Release exists if >= 1 vinyl Instance |
| Cost | Full sync is one paginated collection walk; no N+1 |

## Caveats

- "No extra fetch for master_id" was inferred from values matching, not from a
  request counter. Strong evidence, not instrumented proof. Add a
  `requests.Session` counter to the spike if the RFC wants it nailed down.
- The no-master (`master_id: 0`) shape is confirmed from a single real example.
- Multi-format-per-release (LP-plus-CD in one release) was not observed in this
  collection; the filter rule handles it but it is untested against live data.

## Reproducing

```
DISCOGS_TOKEN=... DISCOGS_USERNAME=... python spike_discogs.py
```

Spike script is untracked (gitignored). See the `EDGE CASES` and `SUMMARY` blocks
in its output.
