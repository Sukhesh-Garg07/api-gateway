import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import RouteLimit, Settings, hash_key
from app.main import create_app
from upstream.main import app as upstream_app

TEST_KEY = "test-secret"
AUTH = {"X-API-Key": TEST_KEY}


def make_settings(**overrides) -> Settings:
    settings = Settings(
        upstream_url="http://upstream.test",
        api_key_hashes={hash_key(TEST_KEY): "tester"},
        default_limit=RouteLimit(requests=100, window_seconds=60.0),
    )
    for name, value in overrides.items():
        setattr(settings, name, value)
    return settings


def make_client(**overrides) -> TestClient:
    transport = httpx.ASGITransport(app=upstream_app)
    return TestClient(create_app(make_settings(**overrides), upstream_transport=transport))


@pytest.fixture()
def client() -> TestClient:
    return make_client()
