"""Phase 0 web smoke: the factory builds and serves. Screens arrive in Phase 6."""

from __future__ import annotations

import pytest

import app as app_module


@pytest.fixture
def client(data_dir):
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
