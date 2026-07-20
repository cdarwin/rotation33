"""Phase 0 web smoke: the factory builds and serves. Screens arrive in Phase 6."""

from __future__ import annotations

import pytest

import app as app_module
import infra
import schema
import sync


@pytest.fixture
def client(data_dir):
    # create_app reconciles orphaned sync runs at startup (RFC section 9), so the
    # schema has to exist first. Production runs `alembic upgrade head` before
    # the factory; the test builds the same schema on the app's own database.
    engine = infra.init_engine()
    schema.metadata.create_all(engine)
    application = app_module.create_app()
    application.config.update(TESTING=True)
    return application.test_client()


def test_factory_serves_a_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Rotation33" in response.data


def test_stylesheet_is_served(client):
    response = client.get("/static/style.css")
    assert response.status_code == 200
    assert b"--accent" in response.data


def test_factory_reconciles_a_run_left_running_by_a_crash(data_dir):
    engine = infra.init_engine()
    schema.metadata.create_all(engine)
    with infra.SessionLocal.begin() as s:
        s.add(
            sync._SyncRunRow(
                status=sync.SyncStatus.RUNNING,
                total=5,
                processed=1,
                started_at=infra.now(),
            )
        )

    app_module.create_app()  # reconciles orphaned runs at startup (RFC section 9)

    with infra.SessionLocal() as s:
        assert sync.latest(s).status is sync.SyncStatus.FAILED
