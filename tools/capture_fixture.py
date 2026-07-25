"""Capture a real Discogs collection payload to a test fixture.

Run once, by hand, with DISCOGS_TOKEN and DISCOGS_USERNAME set. Read-only: it
walks the collection and writes what it sees. Everything downstream then builds
and tests offline against the file, which is what keeps the critical path off an
external service.

    python tools/capture_fixture.py

Writes tests/fixtures/collection.json.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import discogs_client

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "collection.json"

# Only the listing fields the sync actually reads. Capturing the
# whole payload would bloat the fixture and bury the shapes that matter.
KEEP_BASIC = ("id", "master_id", "title", "year", "formats", "artists", "cover_image", "styles")


def instance_payload(item) -> dict:
    data = item.data
    basic = data.get("basic_information", {})
    return {
        "instance_id": data.get("instance_id"),
        "id": data.get("id"),
        "basic_information": {k: basic.get(k) for k in KEEP_BASIC},
    }


def main() -> int:
    token = os.environ.get("DISCOGS_TOKEN")
    username = os.environ.get("DISCOGS_USERNAME")
    if not token or not username:
        print("Set DISCOGS_TOKEN and DISCOGS_USERNAME.", file=sys.stderr)
        return 1

    client = discogs_client.Client(
        "Rotation33/0.1 +https://github.com/benpencodes/rotation33", user_token=token
    )

    folder = client.user(username).collection_folders[0]  # folder 0 is "All"
    print(f"walking {folder.count} items...", file=sys.stderr)

    items = []
    for n, item in enumerate(folder.releases, 1):
        items.append(instance_payload(item))
        if n % 50 == 0:
            print(f"  {n}/{folder.count}", file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"items": items}, indent=2, sort_keys=True) + "\n")
    print(f"wrote {len(items)} items to {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
