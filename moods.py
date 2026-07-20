"""The five fixed moods, their editable descriptions, and the style affinity map.

Mood identity is code, not data (architecture RFC section 5.2). The name and the
time window of each of the five are constants below; only the description
(FR-15) and the affinity map (FR-16) are editable, and each is persisted as an
*override* of a code-level default.

That is what makes FR-17 true without a seeding step: a read returns the
persisted override if one exists, otherwise the built-in default. A fresh
database is already fully configured, first sync onwards, and a future "reset to
defaults" is a delete rather than a re-seed. There is deliberately no seed
script anywhere in this repository, and a test asserts as much.

Conventions are the ones `records` set down: private ORM rows that never leave
the module, a session-bound `_Mapper` that is the only code touching a table,
rules and validation in the public functions, and nothing commits.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time

from sqlalchemy import JSON, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

import infra


class UnknownMood(Exception):
    """Raised when a mood name is not one of the fixed five.

    A loud failure on a typo is the whole point (RFC section 5.2): a misspelled
    mood in a submitted affinity map would otherwise persist happily and then
    silently match nothing forever.
    """


class InvalidAffinity(Exception):
    """Raised when a submitted affinity map is malformed or out of range."""


# --- Domain ----------------------------------------------------------------


@dataclass(frozen=True)
class TimeWindow:
    """A soft time-of-day window, half-open: start inclusive, end exclusive.

    `start > end` means the window wraps past midnight, which is how After Dark
    (18:00 onward) covers 00:00 to 06:00 as well.
    """

    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


@dataclass(frozen=True)
class Mood:
    name: str  # one of the fixed five
    description: str  # editable (FR-15), defaulted from code
    window: TimeWindow  # soft pre-select window; code constant


@dataclass(frozen=True)
class Affinity:
    """Everything needed to judge fit for one mood.

    `mapped_styles` is every style appearing anywhere in the map, not just the
    ones this mood weights. The distinction is load-bearing: `picker` treats an
    *unmapped* style as eligible for every mood (FR-18), so it has to be able to
    tell "no one mapped this style" from "mapped, but not to this mood".
    """

    weights: Mapping[str, float]  # this mood's style -> affinity
    mapped_styles: frozenset[str]  # every style anywhere in the map


# --- The five moods (code constants, RFC section 5.2) ----------------------
#
# In the order the PRD's day-arc runs: ease in, bear down, lift, ease out, and
# optionally clock off. `choices` preserves this order for the start picker.

FIRST_LIGHT = "First Light"
HEADS_DOWN = "Heads Down"
PEAK = "Peak"
GOLDEN_HOUR = "Golden Hour"
AFTER_DARK = "After Dark"

MOODS: tuple[Mood, ...] = (
    Mood(
        name=FIRST_LIGHT,
        description=(
            "Coming online. Low-stakes and familiar, easy on the ears but not "
            "half-asleep: gentle acoustic and roots, warm soul, mellow pop, and "
            "records you know cold."
        ),
        window=TimeWindow(time(6), time(9)),
    ),
    Mood(
        name=HEADS_DOWN,
        description=(
            "For concentration, so instrumental or near-wordless only. Jazz, "
            "film and game scores, and instrumental prog: holds the room "
            "without ever asking for attention."
        ),
        window=TimeWindow(time(9), time(12)),
    ),
    Mood(
        name=PEAK,
        description=(
            "The midday high, where drive matters and genre does not. Funk that "
            "moves, loud when it is earned, fist-in-the-air rock, rowdy roots. "
            "If it raises the pulse, it fits."
        ),
        window=TimeWindow(time(12), time(15)),
    ),
    Mood(
        name=GOLDEN_HOUR,
        description=(
            "The day easing out: warm, nostalgic, sunset-lit. Yacht rock, soft "
            "rock, and the wistful end of the catalog. The comedown rather than "
            "the crash."
        ),
        window=TimeWindow(time(15), time(18)),
    ),
    Mood(
        name=AFTER_DARK,
        description=(
            "Off the clock, no work agenda, so the moodier corners open up: "
            "post-punk, late-night jazz, brooding singer-songwriter. The "
            "optional one; some days it is never picked, and that is fine."
        ),
        # Wraps past midnight. These five windows tile the whole 24 hours, so
        # `for_time` returns a mood for every clock reading.
        window=TimeWindow(time(18), time(6)),
    ),
)

NAMES: tuple[str, ...] = tuple(m.name for m in MOODS)

_BY_NAME: dict[str, Mood] = {m.name: m for m in MOODS}


# --- Built-in affinity map (FR-17) ----------------------------------------
#
# Keyed by Discogs style, mapping to the moods that style suits (PRD section 7:
# "a Style Affinity maps a Discogs style tag to the Mood or Moods it suits"). A
# style may suit more than one mood, with different strengths.
#
# Style-keyed rather than mood-keyed because that is the shape both consumers
# want: the FR-18 review list diffs `records.styles` against these keys, and
# `mapped_styles` is exactly this dict's key set.

DEFAULT_AFFINITY_MAP: dict[str, dict[str, float]] = {
    # The mellow core First Light and Golden Hour share.
    "Folk": {FIRST_LIGHT: 1.0, GOLDEN_HOUR: 0.7},
    "Folk Rock": {FIRST_LIGHT: 1.0, GOLDEN_HOUR: 0.8},
    "Country": {FIRST_LIGHT: 0.9, GOLDEN_HOUR: 0.7},
    "Acoustic": {FIRST_LIGHT: 1.0, GOLDEN_HOUR: 0.7},
    "Soft Rock": {FIRST_LIGHT: 0.7, GOLDEN_HOUR: 1.0},
    "Yacht Rock": {FIRST_LIGHT: 0.5, GOLDEN_HOUR: 1.0},
    # Leaning First Light: warm soul and mellow pop.
    "Soul": {FIRST_LIGHT: 1.0, GOLDEN_HOUR: 0.5},
    "Rhythm & Blues": {FIRST_LIGHT: 0.8, GOLDEN_HOUR: 0.5},
    "Pop Rock": {FIRST_LIGHT: 0.8, GOLDEN_HOUR: 0.6},
    # Leaning Golden Hour: nostalgic classic rock.
    "Classic Rock": {GOLDEN_HOUR: 1.0, PEAK: 0.7},
    # Heads Down takes the wordless styles.
    "Cool Jazz": {HEADS_DOWN: 1.0, AFTER_DARK: 0.6},
    "Hard Bop": {HEADS_DOWN: 1.0, AFTER_DARK: 0.6},  # after-hours Art Blakey
    "Jazz-Funk": {HEADS_DOWN: 0.7, PEAK: 0.7},
    "Soundtrack": {HEADS_DOWN: 1.0},
    "Score": {HEADS_DOWN: 1.0},
    "Ambient": {HEADS_DOWN: 1.0, AFTER_DARK: 0.6},
    "Modern Classical": {HEADS_DOWN: 1.0},
    "Prog Rock": {HEADS_DOWN: 0.8, PEAK: 0.5},
    # Peak takes anything with drive.
    "Funk": {PEAK: 1.0},
    "P.Funk": {PEAK: 1.0},
    "Disco": {PEAK: 1.0},
    "Hard Rock": {PEAK: 1.0},
    "Arena Rock": {PEAK: 1.0, GOLDEN_HOUR: 0.5},
    "Punk": {PEAK: 1.0},
    "Hardcore": {PEAK: 1.0},
    "Hip Hop": {PEAK: 1.0},
    "House": {PEAK: 1.0},
    "Bluegrass": {PEAK: 0.8, FIRST_LIGHT: 0.6},
    "Outlaw Country": {PEAK: 0.8, GOLDEN_HOUR: 0.5},
    # After Dark takes the moodier styles.
    "Post-Punk": {AFTER_DARK: 1.0},
    "Indie Rock": {AFTER_DARK: 1.0},
    "New Wave": {AFTER_DARK: 1.0},
    "Synth-pop": {AFTER_DARK: 1.0},
    "Post Bop": {AFTER_DARK: 0.9, HEADS_DOWN: 0.7},  # late-night jazz
    "Art Rock": {AFTER_DARK: 1.0, HEADS_DOWN: 0.5},
    "Psychedelic Rock": {AFTER_DARK: 0.8, GOLDEN_HOUR: 0.6},
}


# --- Storage (RFC section 7) ----------------------------------------------
#
# Overrides only. Mood identity and windows are code, not rows, and the FR-18
# unmapped-style set is derived on demand rather than stored.


class _DescriptionRow(infra.Base):
    """One row per *edited* mood. An unedited mood has no row at all."""

    __tablename__ = "mood_description"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str]


class _AffinityMapRow(infra.Base):
    """The whole affinity map as one JSON document, in a single row.

    One document rather than a style-per-row table because it is only ever read
    and written whole (FR-16 edits it as a single validated blob), and because
    "no row" has to keep meaning "use the code default".
    """

    __tablename__ = "affinity_map"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)


_SINGLETON = 1


class _Mapper:
    """The only code in this module that touches the tables."""

    def __init__(self, db: Session):
        self._db = db

    # --- transforms --------------------------------------------------------

    @classmethod
    def _mood(cls, base: Mood, description: str | None) -> Mood:
        """Merge a code constant with its persisted override, if any."""
        if description is None:
            return base
        return Mood(name=base.name, description=description, window=base.window)

    # --- reads -------------------------------------------------------------

    def descriptions(self) -> dict[str, str]:
        rows = self._db.scalars(select(_DescriptionRow))
        return {row.name: row.description for row in rows}

    def description(self, name: str) -> str | None:
        row = self._db.get(_DescriptionRow, name)
        return row.description if row else None

    def affinity_map(self) -> dict[str, dict[str, float]] | None:
        """The persisted override, or None when nothing has been edited."""
        row = self._db.get(_AffinityMapRow, _SINGLETON)
        return row.mapping if row else None

    # --- writes ------------------------------------------------------------

    def set_description(self, name: str, text: str) -> None:
        row = self._db.get(_DescriptionRow, name) or _DescriptionRow(name=name)
        row.description = text
        self._db.add(row)

    def set_affinity_map(self, mapping: dict[str, dict[str, float]]) -> None:
        row = self._db.get(_AffinityMapRow, _SINGLETON) or _AffinityMapRow(id=_SINGLETON)
        # Rebuilt rather than mutated in place: SQLAlchemy does not track
        # mutation inside a plain JSON column, so an in-place edit would not be
        # flushed.
        row.mapping = {style: dict(weights) for style, weights in mapping.items()}
        self._db.add(row)


# --- Public surface (architecture RFC section 5.2) -------------------------


def choices(db: Session) -> list[Mood]:
    """The five moods, descriptions merged with any overrides, in day order."""
    overrides = _Mapper(db).descriptions()
    return [_Mapper._mood(m, overrides.get(m.name)) for m in MOODS]


def get(db: Session, name: str) -> Mood:
    """One mood for display. Raises rather than returning None: the five are known."""
    base = _BY_NAME.get(name)
    if base is None:
        raise UnknownMood(name)
    return _Mapper._mood(base, _Mapper(db).description(name))


def affinity(db: Session, name: str) -> Affinity:
    """This mood's fit input, for the facade-to-`picker` handoff.

    The fit *rules* (best-style-wins FR-8, unmapped-eligible FR-18) live in
    `picker`; this only supplies the weights and the mapped-style set.
    """
    if name not in _BY_NAME:
        raise UnknownMood(name)
    mapping = affinity_map(db)
    return Affinity(
        weights={style: weights[name] for style, weights in mapping.items() if name in weights},
        # Every style anywhere in the map, not just this mood's. See Affinity.
        mapped_styles=frozenset(mapping),
    )


def for_time(now: datetime) -> str:
    """The time-appropriate mood to pre-select (FR-3). No storage, so no `db`.

    The five windows tile the whole 24 hours, After Dark wrapping past midnight
    to cover 00:00 to 06:00, so this returns a mood for every clock reading.
    """
    t = now.time()
    for mood in MOODS:
        if mood.window.contains(t):
            return mood.name
    # Unreachable while the windows tile the day; loud if that ever stops holding.
    raise AssertionError(f"no mood window covers {t}")


def affinity_map(db: Session) -> dict[str, dict[str, float]]:
    """The whole editable map: the persisted override, else the code default.

    Keyed by style; the keys are what the view diffs against `records.styles`
    for the FR-18 review list. Always a fresh dict, so a caller cannot mutate
    the module constant.
    """
    stored = _Mapper(db).affinity_map()
    source = DEFAULT_AFFINITY_MAP if stored is None else stored
    return {style: dict(weights) for style, weights in source.items()}


def set_description(db: Session, name: str, text: str) -> None:
    """Persist an override of a mood's built-in description (FR-15)."""
    if name not in _BY_NAME:
        raise UnknownMood(name)
    _Mapper(db).set_description(name, text)


def set_affinity_map(db: Session, mapping: Mapping[str, Mapping[str, float]]) -> None:
    """Replace the whole style-to-mood affinity map (FR-16).

    Validated at the write boundary (RFC sections 5.2 and 12): every mood name
    must be one of the five and every affinity a number in [0, 1]. A typo has to
    fail here and loudly, because the alternative is a map that persists fine
    and then silently never matches anything.
    """
    clean: dict[str, dict[str, float]] = {}
    for style, weights in mapping.items():
        if not isinstance(style, str) or not style:
            raise InvalidAffinity(f"style keys must be non-empty strings, got {style!r}")
        if not isinstance(weights, Mapping):
            raise InvalidAffinity(f"{style!r} must map to mood -> affinity, got {weights!r}")
        row: dict[str, float] = {}
        for mood_name, value in weights.items():
            if mood_name not in _BY_NAME:
                raise UnknownMood(mood_name)
            # bool is an int subclass and is never a meaningful affinity.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidAffinity(f"{style!r}/{mood_name!r} affinity must be a number")
            if not 0.0 <= value <= 1.0:
                raise InvalidAffinity(f"{style!r}/{mood_name!r} affinity {value} is outside [0, 1]")
            row[mood_name] = float(value)
        clean[style] = row
    _Mapper(db).set_affinity_map(clean)
