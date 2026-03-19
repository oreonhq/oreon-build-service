"""Basic API tests. Endpoints that need a real DB are skipped unless OREON_TEST_DB=1."""
import os
import pytest
from fastapi.testclient import TestClient

from oreon_build.api.main import create_app

needs_db = pytest.mark.skipif(
    os.environ.get("OREON_TEST_DB") != "1",
    reason="Set OREON_TEST_DB=1 and have PostgreSQL to run DB tests",
)


@pytest.fixture
def client():
    return TestClient(create_app())


def test_app_creates(client):
    assert client is not None


def test_openapi(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    data = r.json()
    assert "openapi" in data
    assert "paths" in data


@needs_db
def test_releases_list(client):
    r = client.get("/api/releases")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@needs_db
def test_packages_list(client):
    r = client.get("/api/packages")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@needs_db
def test_workers_list(client):
    r = client.get("/api/workers")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@needs_db
def test_repo_status(client):
    r = client.get("/api/repos/status")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_login_requires_body(client):
    r = client.post("/api/auth/login", json={})
    assert r.status_code == 422
