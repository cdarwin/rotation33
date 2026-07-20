"""Phase 3: the five moods, their descriptions, and the affinity map.

The assertions that matter here are the FR-17 one (defaults come from code, so
there is no seeding step and no seed script), the FR-3 one (`for_time` answers
for every hour of the clock, including the post-midnight wrap), and the FR-16
write-boundary validation that makes a typo fail loudly.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import DeclarativeBase

import moods
from moods import Affinity, InvalidAffinity, Mood, UnknownMood

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- The fixed five --------------------------------------------------------


def test_there_are_exactly_five_moods():
    assert moods.NAMES == (
        "First Light",
        "Heads Down",
        "Peak",
        "Golden Hour",
        "After Dark",
    )


def test_choices_returns_the_five_in_day_order(db):
    assert [m.name for m in moods.choices(db)] == list(moods.NAMES)


def test_get_raises_on_an_unknown_mood(db):
    with pytest.raises(UnknownMood):
        moods.get(db, "Brunch")


# --- for_time (FR-3) -------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        # The post-midnight wrap: After Dark covers 00:00 to 06:00.
        *[(h, "After Dark") for h in range(0, 6)],
        *[(h, "First Light") for h in range(6, 9)],
        *[(h, "Heads Down") for h in range(9, 12)],
        *[(h, "Peak") for h in range(12, 15)],
        *[(h, "Golden Hour") for h in range(15, 18)],
        *[(h, "After Dark") for h in range(18, 24)],
    ],
)
def test_for_time_covers_every_hour_of_the_clock(hour, expected):
    assert moods.for_time(datetime(2026, 7, 19, hour, 30)) == expected


def test_for_time_never_leaves_a_gap_across_the_whole_day():
    """Every minute of the 24 hours resolves, or the windows have stopped tiling."""
    for minute in range(24 * 60):
        at = datetime(2026, 7, 19, minute // 60, minute % 60)
        assert moods.for_time(at) in moods.NAMES


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 7, 19, 0, 0), "After Dark"),  # exactly midnight
        (datetime(2026, 7, 19, 5, 59), "After Dark"),  # last minute before dawn
        (datetime(2026, 7, 19, 6, 0), "First Light"),  # windows are start-inclusive
        (datetime(2026, 7, 19, 17, 59), "Golden Hour"),
        (datetime(2026, 7, 19, 18, 0), "After Dark"),  # and end-exclusive
        (datetime(2026, 7, 19, 23, 59), "After Dark"),
    ],
)
def test_for_time_boundaries_are_half_open(at, expected):
    assert moods.for_time(at) == expected


def test_for_time_takes_no_database():
    """It reads no storage, so it must not ask for a session (RFC section 3)."""
    import inspect

    assert "db" not in inspect.signature(moods.for_time).parameters


# --- Defaults with no seeding (FR-17) --------------------------------------


def test_descriptions_default_from_code_on_a_virgin_database(db):
    for mood in moods.choices(db):
        assert mood.description
        assert mood.description == moods._BY_NAME[mood.name].description


def test_affinity_map_defaults_from_code_on_a_virgin_database(db):
    assert moods.affinity_map(db) == moods.DEFAULT_AFFINITY_MAP


def test_affinity_is_usable_before_anything_is_ever_written(db):
    """FR-17: sensible recommendations immediately, with no manual setup."""
    got = moods.affinity(db, "Peak")
    assert got.weights["Funk"] == 1.0
    assert "Post-Punk" in got.mapped_styles  # mapped, just not to Peak
    assert "Post-Punk" not in got.weights


def test_no_seed_script_exists_anywhere(db):
    """FR-17 is satisfied by code defaults, so seeding must not exist to drift from.

    A seed step would reintroduce exactly the failure mode the design removes: a
    database whose defaults are a snapshot of an older constant.
    """
    skip = {".git", ".venv", "node_modules", "__pycache__"}
    sources = [p for p in REPO_ROOT.rglob("*.py") if not any(part in skip for part in p.parts)]
    assert [p for p in sources if "seed" in p.name.lower()] == []
    offenders = [
        p for p in sources if p != Path(__file__) and "def seed" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- The built-in map covers the PRD's styles ------------------------------


def test_every_mood_has_at_least_one_default_style(db):
    for name in moods.NAMES:
        assert moods.affinity(db, name).weights, f"{name} would match nothing"


def test_default_affinities_are_all_within_range():
    for style, weights in moods.DEFAULT_AFFINITY_MAP.items():
        for name, value in weights.items():
            assert name in moods.NAMES, style
            assert 0.0 <= value <= 1.0, (style, name)


def test_a_style_may_suit_more_than_one_mood():
    """PRD section 7: First Light and Golden Hour share the mellow core."""
    assert moods.DEFAULT_AFFINITY_MAP["Folk Rock"].keys() >= {"First Light", "Golden Hour"}


def test_mapped_styles_is_every_style_anywhere_in_the_map(db):
    """Not just this mood's, which is what makes unmapped-means-eligible work."""
    everything = frozenset(moods.affinity_map(db))
    for name in moods.NAMES:
        assert moods.affinity(db, name).mapped_styles == everything


# --- Overrides win (FR-15, FR-16) -----------------------------------------


def test_set_description_overrides_the_code_default(db):
    moods.set_description(db, "Peak", "Loud, please.")
    assert moods.get(db, "Peak").description == "Loud, please."
    assert [m.description for m in moods.choices(db) if m.name == "Peak"] == ["Loud, please."]


def test_set_description_leaves_the_other_moods_on_their_defaults(db):
    moods.set_description(db, "Peak", "Loud, please.")
    assert moods.get(db, "First Light").description == moods._BY_NAME["First Light"].description


def test_set_description_is_idempotent_and_replaces(db):
    moods.set_description(db, "Peak", "First.")
    moods.set_description(db, "Peak", "Second.")
    assert moods.get(db, "Peak").description == "Second."


def test_set_description_does_not_change_the_window(db):
    before = moods.get(db, "Peak").window
    moods.set_description(db, "Peak", "Loud, please.")
    assert moods.get(db, "Peak").window == before


def test_set_description_raises_on_an_unknown_mood(db):
    with pytest.raises(UnknownMood):
        moods.set_description(db, "Brunch", "Eggs.")


def test_set_affinity_map_replaces_the_whole_map(db):
    moods.set_affinity_map(db, {"Zydeco": {"Peak": 0.5}})
    assert moods.affinity_map(db) == {"Zydeco": {"Peak": 0.5}}
    assert moods.affinity(db, "Peak").weights == {"Zydeco": 0.5}
    assert "Funk" not in moods.affinity(db, "Peak").mapped_styles


def test_set_affinity_map_can_be_written_twice(db):
    moods.set_affinity_map(db, {"Zydeco": {"Peak": 0.5}})
    moods.set_affinity_map(db, {"Klezmer": {"After Dark": 1.0}})
    assert moods.affinity_map(db) == {"Klezmer": {"After Dark": 1.0}}


def test_an_empty_submitted_map_is_an_override_not_a_reset_to_defaults(db):
    """ "No styles mapped" is a legitimate state and must not fall back to code."""
    moods.set_affinity_map(db, {})
    assert moods.affinity_map(db) == {}
    assert moods.affinity(db, "Peak").mapped_styles == frozenset()


# --- Write-boundary validation (FR-16, RFC section 12) --------------------


def test_set_affinity_map_rejects_an_unknown_mood_name(db):
    """A typo must fail loudly rather than persist and silently never match."""
    with pytest.raises(UnknownMood):
        moods.set_affinity_map(db, {"Funk": {"Pekk": 1.0}})


@pytest.mark.parametrize("value", [-0.1, 1.1, 2, -1])
def test_set_affinity_map_rejects_an_out_of_range_affinity(db, value):
    with pytest.raises(InvalidAffinity):
        moods.set_affinity_map(db, {"Funk": {"Peak": value}})


@pytest.mark.parametrize("value", ["1.0", None, True, [1.0]])
def test_set_affinity_map_rejects_a_non_numeric_affinity(db, value):
    with pytest.raises(InvalidAffinity):
        moods.set_affinity_map(db, {"Funk": {"Peak": value}})


def test_set_affinity_map_rejects_a_style_that_does_not_map_to_a_dict(db):
    with pytest.raises(InvalidAffinity):
        moods.set_affinity_map(db, {"Funk": 1.0})


def test_set_affinity_map_rejects_an_empty_style_key(db):
    with pytest.raises(InvalidAffinity):
        moods.set_affinity_map(db, {"": {"Peak": 1.0}})


def test_a_rejected_map_leaves_the_previous_one_intact(db):
    moods.set_affinity_map(db, {"Zydeco": {"Peak": 0.5}})
    with pytest.raises(UnknownMood):
        moods.set_affinity_map(db, {"Funk": {"Pekk": 1.0}})
    assert moods.affinity_map(db) == {"Zydeco": {"Peak": 0.5}}


def test_integer_affinities_are_accepted_and_stored_as_floats(db):
    moods.set_affinity_map(db, {"Funk": {"Peak": 1}, "Folk": {"Peak": 0}})
    stored = moods.affinity_map(db)
    assert stored == {"Funk": {"Peak": 1.0}, "Folk": {"Peak": 0.0}}
    assert all(isinstance(v, float) for w in stored.values() for v in w.values())


def test_affinity_raises_on_an_unknown_mood(db):
    with pytest.raises(UnknownMood):
        moods.affinity(db, "Brunch")


# --- The _Mapper boundary --------------------------------------------------


def test_no_orm_row_type_is_reachable_from_outside_the_module():
    leaked = [
        name
        for name in dir(moods)
        if not name.startswith("_")
        and isinstance(getattr(moods, name), type)
        and issubclass(getattr(moods, name), DeclarativeBase)
    ]
    assert leaked == []


def test_public_functions_return_only_dataclasses(db):
    assert all(dataclasses.is_dataclass(m) for m in moods.choices(db))
    assert dataclasses.is_dataclass(moods.get(db, "Peak"))
    got = moods.affinity(db, "Peak")
    assert dataclasses.is_dataclass(got)
    assert dataclasses.is_dataclass(moods.get(db, "Peak").window)


def test_domain_models_are_frozen(db):
    mood = moods.get(db, "Peak")
    with pytest.raises(dataclasses.FrozenInstanceError):
        mood.description = "mutated"
    with pytest.raises(dataclasses.FrozenInstanceError):
        mood.window.start = None
    with pytest.raises(dataclasses.FrozenInstanceError):
        moods.affinity(db, "Peak").weights = {}


def test_the_returned_map_cannot_mutate_the_module_constant(db):
    got = moods.affinity_map(db)
    got["Funk"]["Peak"] = 0.0
    got["Invented"] = {}
    assert moods.affinity_map(db) == moods.DEFAULT_AFFINITY_MAP
    assert moods.DEFAULT_AFFINITY_MAP["Funk"]["Peak"] == 1.0


def test_moods_imports_no_other_component():
    source = (REPO_ROOT / "moods.py").read_text(encoding="utf-8")
    for component in ("records", "sessions", "picker", "recommendations"):
        assert f"import {component}" not in source


def test_domain_types_are_what_the_rfc_names():
    assert {f.name for f in dataclasses.fields(Mood)} == {"name", "description", "window"}
    assert {f.name for f in dataclasses.fields(Affinity)} == {"weights", "mapped_styles"}
