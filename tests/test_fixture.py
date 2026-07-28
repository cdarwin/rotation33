"""Guards the captured Discogs fixture.

The fixture is the contract that lets every later phase, sync included, be built
and tested with no Discogs token. If it silently lost
one of the shapes below, the phase that depends on that shape would go green
while testing nothing. These assertions are cheap and they fail loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "collection.json"


@pytest.fixture(scope="module")
def items() -> list[dict]:
    return json.loads(FIXTURE.read_text())["items"]


def formats(item: dict) -> set[str]:
    return {f["name"] for f in item["basic_information"].get("formats") or []}


def release_key(item: dict) -> str:
    """The identity rule, restated here so the fixture test does
    not depend on `records` (which does not exist until Phase 2)."""
    basic = item["basic_information"]
    master_id = basic.get("master_id")
    return f"m{master_id}" if master_id else f"r{basic['id']}"


def test_fixture_is_not_trivially_small(items):
    assert len(items) > 50


def test_contains_a_multi_instance_master(items):
    """Several owned pressings of one album: the grouping case."""
    counts: dict[str, int] = {}
    for item in items:
        counts[release_key(item)] = counts.get(release_key(item), 0) + 1
    assert any(n > 1 for n in counts.values())


def test_contains_a_no_master_item(items):
    """Confirmed against live data: `master_id` is literally 0, not absent."""
    no_master = [i for i in items if i["basic_information"].get("master_id") == 0]
    assert no_master
    assert all(release_key(i).startswith("r") for i in no_master)


def test_contains_a_non_vinyl_item(items):
    """Something the vinyl filter must drop, or the filter tests prove nothing."""
    assert any("Vinyl" not in formats(i) for i in items)


def test_contains_a_multi_format_vinyl_plus_cd_item(items):
    """The `any(format == "Vinyl")` rule must keep an LP+CD edition.

    Hand-added: the live collection contains no such item, so this shape would
    otherwise go untested.
    """
    assert any({"Vinyl", "CD"} <= formats(i) for i in items)


def test_contains_an_item_with_no_styles(items):
    """Naturally present, and it reaches `matching` as a candidate that fits
    nowhere by weight but is eligible everywhere by the unmapped rule."""
    assert any(not i["basic_information"].get("styles") for i in items)


def test_every_item_has_the_fields_sync_reads(items):
    for item in items:
        assert item["instance_id"], item
        basic = item["basic_information"]
        assert basic.get("id"), item
        assert basic.get("title"), item
        assert basic.get("formats"), item


def test_instance_ids_are_unique(items):
    ids = [i["instance_id"] for i in items]
    assert len(ids) == len(set(ids))


def test_no_credentials_are_embedded(items):
    """The payload carries no secrets, but confirm rather than assume."""
    raw = FIXTURE.read_text().lower()
    for marker in ("token", "secret", "authorization", "oauth", "password"):
        assert marker not in raw
