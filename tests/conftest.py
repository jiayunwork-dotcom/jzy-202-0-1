from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rccp.config import Settings
from rccp.main import create_app

WEEKS = 20


@pytest.fixture
def settings(tmp_path):
    return Settings(weeks=WEEKS, db_path=str(tmp_path / "rccp.db"))


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
