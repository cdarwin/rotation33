"""An end-to-end run over the real captured collection: Phase 4's exit criterion.

Loads the 75-item Discogs fixture, maps it into `records` exactly the way `sync`
will (the vinyl filter and the `m<master_id>` / `r<release_id>` identity rule),
starts a session with a mood, and generates a recommendation. No Discogs token
and no network: the fixture is the contract that keeps the critical path offline
(execution plan section 2).

This is deliberately not a preview of `sync`. It skips cover art, progress
reporting, retirement reconciliation and failure isolation, all of which are
Phase 5's actual work. What it proves is the narrower and more valuable thing:
that the four components underneath the facade compose into a real
recommendation from a real collection.

    python tools/e2e_demo.py [mood]

`tests/test_recommendations_e2e.py` runs the same functions, so CI keeps this
honest rather than letting it rot into a script nobody executes.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import infra  # noqa: E402
import moods  # noqa: E402
import recommendations  # noqa: E402
import records  # noqa: E402
import schema  # noqa: E402
import sessions  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "collection.json"


def is_vinyl(item: dict) -> bool:
    """The RFC section 9 step 2 rule: kept if *any* format is Vinyl.

    `any` rather than `all` so an LP-plus-CD edition is kept rather than dropped
    for the CD it also ships.
    """
    formats = item["basic_information"].get("formats") or []
    return any(f.get("name") == "Vinyl" for f in formats)


def artist_of(basic: dict) -> str:
    """Discogs splits a credit across artists with `join` words between them."""
    parts = []
    for artist in basic.get("artists") or []:
        parts.append(artist.get("anv") or artist.get("name") or "")
        if artist.get("join"):
            parts.append(artist["join"])
    return " ".join(p for p in parts if p).strip() or "Unknown Artist"


def load(db, items: list[dict]) -> int:
    """Group vinyl instances into releases and upsert them. Returns the count.

    Grouping calls `records.release_id` rather than restating the rule, because
    two definitions of album identity would eventually drift and re-key the
    whole collection.
    """
    grouped: dict[str, dict] = {}
    for item in items:
        if not is_vinyl(item):
            continue
        basic = item["basic_information"]
        rid = records.release_id(basic.get("master_id"), basic["id"])
        release = grouped.get(rid)
        if release is None:
            release = grouped[rid] = records.Release(
                id=rid,
                artist=artist_of(basic),
                title=basic.get("title") or "Untitled",
                styles=list(basic.get("styles") or []),
                cover_url=None,
                year=basic.get("year") or None,
                instances=[],
            )
        release.instances.append(
            records.Instance(
                id=str(item["instance_id"]),
                is_playable=True,
                retirement_status=records.RetirementStatus.ACTIVE,
                pressing_release_id=basic["id"],
                description=", ".join(
                    d for f in basic.get("formats") or [] for d in (f.get("descriptions") or [])
                )
                or None,
            )
        )

    for release in grouped.values():
        records.upsert(db, release)
    db.flush()
    return len(grouped)


def run(db, mood: str, now: datetime) -> tuple[int, recommendations.RecommendationResult]:
    """Load, start a session, generate. The whole path in three calls."""
    items = json.loads(FIXTURE.read_text())["items"]
    total = load(db, items)
    session = sessions.start(db, mood, now)
    db.flush()
    return total, recommendations.generate(db, session.id, now)


def main() -> int:
    now = infra.now()
    mood = sys.argv[1] if len(sys.argv) > 1 else moods.for_time(now)

    # An in-memory database: the demo is a read of the fixture, not something
    # that should leave a file behind on the real data volume.
    engine = infra.init_engine("sqlite://")
    schema.metadata.create_all(engine)
    db = infra.SessionLocal()
    try:
        albums, result = run(db, mood, now)
    finally:
        db.close()
        engine.dispose()

    print(f"Collection: {albums} vinyl albums from {FIXTURE.name}")
    print(f"Mood:       {mood}")
    print(f"Window:     {recommendations.DEFAULT_WINDOW_DAYS} days")
    print()

    if not result.releases:
        print(f"No recommendations: {result.reason.value}")
        return 0

    print(f"{len(result.releases)} recommendations, in draw order:")
    for n, release in enumerate(result.releases, 1):
        year = f" ({release.year})" if release.year else ""
        styles = ", ".join(release.styles) or "no styles listed"
        print(f"  {n}. {release.artist} - {release.title}{year}")
        print(f"     {styles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
