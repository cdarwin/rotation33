"""Flask test-client smoke coverage of the screen wiring.

The logic underneath is tested in each component's own suite; these guard that
the views reach it, that the two htmx interactions swap the right fragment, and
that every screen is reachable through the UI. Data is seeded through the real write
path so a view reads what a real sync or session would have written.
"""

from __future__ import annotations

import json

import pytest

import app as app_module
import infra
import moods
import recommendations
import records
import schema
import sessions
import sync


def _seed_collection(n: int = 3) -> list[str]:
    """A few releases whose single style is unmapped, so they fit every mood and
    `generate` always returns picks regardless of the seeded session's mood."""
    ids = []
    with infra.SessionLocal.begin() as db:
        for i in range(n):
            rid = records.release_id(2000 + i, 6000 + i)
            ids.append(rid)
            records.upsert(
                db,
                records.Release(
                    id=rid,
                    artist=f"Artist {i}",
                    title=f"Album {i}",
                    styles=["Unmapped Style"],
                    cover_url=None,
                    year=2020 + i,
                    instances=[
                        records.Instance(
                            id=f"inst{i}",
                            is_playable=True,
                            retirement_status=records.RetirementStatus.ACTIVE,
                            pressing_release_id=6000 + i,
                        )
                    ],
                ),
            )
    return ids


@pytest.fixture
def client(data_dir):
    engine = infra.init_engine()
    schema.metadata.create_all(engine)
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


# --- Home -------------------------------------------------------------------


def test_home_prompts_a_sync_when_the_collection_is_empty(client):
    page = client.get("/")
    assert page.status_code == 200
    assert b"collection is empty" in page.data.lower() or b"Run a sync" in page.data


def test_home_offers_the_moods_with_one_preselected(client):
    _seed_collection()
    page = client.get("/")
    assert page.status_code == 200
    for name in moods.NAMES:
        assert name.encode() in page.data
    assert b"checked" in page.data  # a mood is pre-selected


# --- The session workspace --------------------------------------------------


def test_start_session_then_see_picks(client):
    _seed_collection()
    start = client.post("/session/start", data={"mood": moods.NAMES[0]})
    assert start.status_code == 302

    page = client.get("/session")
    assert page.status_code == 200
    assert b"pick-card" in page.data
    assert b"Log something else" in page.data  # search-to-log folded in
    assert b"Logged this session" in page.data  # the session log lives here too


def test_home_redirects_to_the_workspace_when_a_session_is_active(client):
    _seed_collection()
    client.post("/session/start", data={"mood": moods.NAMES[0]})

    home = client.get("/")
    assert home.status_code == 302
    assert home.headers["Location"].endswith("/session")


def test_new_query_forces_the_start_form_despite_an_active_session(client):
    _seed_collection()
    client.post("/session/start", data={"mood": moods.NAMES[0]})

    page = client.get("/?new=1")
    assert page.status_code == 200
    assert b"Start a session" in page.data  # the mood picker, not a redirect


def test_regenerate_returns_only_the_picks_fragment(client):
    _seed_collection()
    client.post("/session/start", data={"mood": moods.NAMES[0]})

    fragment = client.post("/session/regenerate")
    assert fragment.status_code == 200
    # A fragment for the htmx swap, not a whole page.
    assert b"<nav" not in fragment.data
    assert b"pick-card" in fragment.data or b"empty-reason" in fragment.data


def test_regenerate_keeps_the_pinned_picks(client):
    _seed_collection(8)
    client.post("/session/start", data={"mood": moods.NAMES[0]})
    with infra.SessionLocal() as db:
        sid = sessions.current(db).id
        pinned = recommendations.active(db, sid).releases[0].id

    fragment = client.post("/session/regenerate", data={"keep": [pinned]})
    assert pinned.encode() in fragment.data  # the pin survived
    with infra.SessionLocal() as db:
        assert pinned in {r.id for r in recommendations.active(db, sid).releases}


def test_start_session_rejects_an_unknown_mood(client):
    _seed_collection()
    response = client.post("/session/start", data={"mood": "Nonsense"}, follow_redirects=True)
    assert response.status_code == 200
    assert b"Pick a mood" in response.data


# --- Logging inside the workspace -------------------------------------------


def test_log_a_pick_then_remove_it(client):
    _seed_collection()
    client.post("/session/start", data={"mood": moods.NAMES[0]})

    logged = client.post(
        "/session/log", data={"instance_id": "inst0", "release_id": "m2000"}, follow_redirects=True
    )
    assert logged.status_code == 200

    with infra.SessionLocal() as db:
        plays = sessions.plays(db, sessions.current(db).id)
        assert len(plays) == 1
        play_id = plays[0].id

    client.post(f"/session/remove/{play_id}", follow_redirects=True)
    with infra.SessionLocal() as db:
        assert sessions.plays(db, sessions.current(db).id) == []


def test_a_logged_pick_leaves_the_recommendations(client):
    _seed_collection(8)
    client.post("/session/start", data={"mood": moods.NAMES[0]})
    with infra.SessionLocal() as db:
        sid = sessions.current(db).id
        pick = recommendations.active(db, sid).releases[0]

    client.post("/session/log", data={"instance_id": pick.instances[0].id, "release_id": pick.id})

    page = client.get("/session")
    # The played pick is gone from the recommendations panel (which ends where
    # the search section begins) and now shows under the session log instead.
    picks_html = page.data.split(b"Log something else")[0]
    assert pick.id.encode() not in picks_html


def _search_section(data: bytes) -> bytes:
    """The 'Log something else' panel only, between it and the session log, so an
    album title that also happens to be a current recommendation is not counted."""
    return data.split(b"Log something else", 1)[1].split(b"Logged this session", 1)[0]


def test_search_shows_results_only_after_a_query(client):
    _seed_collection()
    client.post("/session/start", data={"mood": moods.NAMES[0]})

    bare = client.get("/session")
    assert b"album-row" not in _search_section(bare.data)  # no default full list

    searched = client.get("/session?q=Album 1")
    section = _search_section(searched.data)
    assert b"Album 1" in section
    assert b"Album 2" not in section


def test_the_old_log_screen_is_gone(client):
    assert client.get("/log").status_code == 404


# --- Condition --------------------------------------------------------------


def test_toggle_playable(client):
    _seed_collection()
    client.post("/condition/toggle", data={"instance_id": "inst0"})  # no "playable" => off
    with infra.SessionLocal() as db:
        inst = next(i for i in records.get(db, "m2000").instances if i.id == "inst0")
        assert inst.is_playable is False

    client.post("/condition/toggle", data={"instance_id": "inst0", "playable": "on"})
    with infra.SessionLocal() as db:
        inst = next(i for i in records.get(db, "m2000").instances if i.id == "inst0")
        assert inst.is_playable is True


# --- Settings ---------------------------------------------------------------


def test_set_recency_window(client):
    client.post("/settings/window", data={"days": "7"})
    with infra.SessionLocal() as db:
        assert recommendations.window(db).days == 7


def test_reject_a_nonsense_window(client):
    response = client.post("/settings/window", data={"days": "-3"}, follow_redirects=True)
    assert b"whole number of days" in response.data


def test_edit_a_mood_description(client):
    client.post(f"/settings/mood/{moods.NAMES[0]}", data={"description": "brand new words"})
    with infra.SessionLocal() as db:
        assert moods.get(db, moods.NAMES[0]).description == "brand new words"


def test_save_a_valid_affinity_map(client):
    payload = {"Cool Jazz": {moods.NAMES[0]: 0.9}}
    response = client.post(
        "/settings/affinity", data={"affinity": json.dumps(payload)}, follow_redirects=True
    )
    assert b"saved" in response.data.lower()
    with infra.SessionLocal() as db:
        assert moods.affinity_map(db) == payload


def test_reject_invalid_affinity_json(client):
    response = client.post(
        "/settings/affinity", data={"affinity": "{not json"}, follow_redirects=True
    )
    assert b"not valid JSON" in response.data


def test_reject_an_unknown_mood_in_the_affinity_map(client):
    bad = {"Cool Jazz": {"No Such Mood": 0.5}}
    response = client.post(
        "/settings/affinity", data={"affinity": json.dumps(bad)}, follow_redirects=True
    )
    assert b"rejected" in response.data


def test_settings_lists_unmapped_styles(client):
    _seed_collection()  # every seeded release carries "Unmapped Style"
    page = client.get("/settings")
    assert b"Unmapped Style" in page.data  # the unclassified-styles list


# --- Sync -------------------------------------------------------------------


def test_sync_page_shows_never_synced_before_a_run(client):
    page = client.get("/sync")
    assert page.status_code == 200
    assert b"Never synced" in page.data


def test_trigger_reports_when_a_sync_is_already_running(client, monkeypatch):
    monkeypatch.setattr(sync, "trigger", lambda: False)
    response = client.post("/sync/trigger", follow_redirects=True)
    assert b"already running" in response.data


def test_trigger_starts_a_sync(client, monkeypatch):
    started = {}

    def fake_trigger():
        started["yes"] = True
        return True

    monkeypatch.setattr(sync, "trigger", fake_trigger)
    response = client.post("/sync/trigger", follow_redirects=True)
    assert response.status_code == 200
    assert started.get("yes")


def test_progress_partial_reflects_a_running_row(client):
    with infra.SessionLocal.begin() as db:
        db.add(
            sync._SyncRunRow(
                status=sync.SyncStatus.RUNNING, total=100, processed=40, started_at=infra.now()
            )
        )
    partial = client.get("/sync/progress")
    assert partial.status_code == 200
    assert b"40 of 100" in partial.data
    assert b"hx-get" in partial.data  # keeps polling while running


def test_confirm_a_retirement(client):
    _seed_collection()
    # Make inst1 pending by reconciling against a present-set that omits it.
    with infra.SessionLocal.begin() as db:
        records.reconcile_retirements(db, {"inst0", "inst2"})

    page = client.get("/sync")
    assert b"Confirm retirements" in page.data

    client.post("/sync/retire", data={"instance_id": "inst1"}, follow_redirects=True)
    with infra.SessionLocal() as db:
        inst = next(i for i in records.get(db, "m2001").instances if i.id == "inst1")
        assert inst.retirement_status is records.RetirementStatus.RETIRED


# --- Cover serving ----------------------------------------------------------


def test_cover_is_served_from_the_data_volume(client, data_dir):
    (data_dir / "covers" / "m2000.jpg").write_bytes(b"IMGDATA")
    response = client.get("/covers/m2000.jpg")
    assert response.status_code == 200
    assert response.data == b"IMGDATA"


def test_a_missing_cover_is_a_404(client):
    assert client.get("/covers/nope.jpg").status_code == 404


# --- A sold copy is never offered ------------------------------------------


def test_a_retired_copy_is_not_offered_in_the_copy_picker(client):
    """The picker iterated the whole aggregate and pre-selected loop.first, so a
    copy confirmed as sold could be the default choice for a new play."""
    with infra.SessionLocal.begin() as db:
        records.upsert(
            db,
            records.Release(
                id="m9001",
                artist="Two Copies",
                title="Same Album",
                styles=["Unmapped Style"],
                cover_url=None,
                year=2001,
                instances=[
                    records.Instance(
                        id="sold",
                        is_playable=True,
                        retirement_status=records.RetirementStatus.ACTIVE,
                        pressing_release_id=1,
                        description="Sold Pressing",
                    ),
                    records.Instance(
                        id="kept",
                        is_playable=True,
                        retirement_status=records.RetirementStatus.ACTIVE,
                        pressing_release_id=2,
                        description="Kept Pressing",
                    ),
                ],
            ),
        )
    with infra.SessionLocal.begin() as db:
        records.confirm_retirement(db, ["sold"])

    client.post("/session/start", data={"mood": moods.PEAK})
    page = client.get("/session", query_string={"q": "Same Album"})

    # One owned copy left, so the picker collapses to a single hidden input.
    assert b'value="kept"' in page.data
    assert b'value="sold"' not in page.data
    assert b"Sold Pressing" not in page.data


def test_a_retired_copy_is_not_listed_on_the_condition_screen(client):
    _seed_collection(2)
    with infra.SessionLocal.begin() as db:
        records.confirm_retirement(db, ["inst0"])

    page = client.get("/condition")

    assert b'value="inst0"' not in page.data
    assert b'value="inst1"' in page.data


# --- The empty-result explanation reaches the screen ------------------------


def test_an_empty_result_renders_its_explanation(client):
    """`active` used to hardcode reason=None, so both call sites passed None and
    _EMPTY_MESSAGES was unreachable and no explanation ever rendered."""
    _seed_collection(1)
    client.post("/session/start", data={"mood": moods.PEAK})

    fragment = client.post("/session/regenerate")

    assert fragment.status_code == 200
    assert b"already seen everything" in fragment.data


def test_the_explanation_survives_a_page_reload(client):
    _seed_collection(1)
    client.post("/session/start", data={"mood": moods.PEAK})
    client.post("/session/regenerate")

    page = client.get("/session")

    assert b"already seen everything" in page.data
