from app.config import RouteLimit
from conftest import AUTH, make_client


def test_success_response_carries_rate_limit_headers(client):
    resp = client.get("/api/echo", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["x-ratelimit-limit"] == "100"
    assert resp.headers["x-ratelimit-remaining"] == "99"
    assert int(resp.headers["x-ratelimit-reset"]) > 0


def test_proxy_passes_body_and_query(client):
    resp = client.post("/api/echo?x=1", headers=AUTH, content="hello gateway")
    assert resp.status_code == 200
    body = resp.json()
    assert body["body"] == "hello gateway"
    assert body["query"] == {"x": "1"}


def test_429_after_limit_with_headers():
    c = make_client(default_limit=RouteLimit(requests=3, window_seconds=60.0))
    for _ in range(3):
        assert c.get("/api/echo", headers=AUTH).status_code == 200
    resp = c.get("/api/echo", headers=AUTH)
    assert resp.status_code == 429
    assert resp.json()["detail"] == "rate limit exceeded"
    assert int(resp.headers["retry-after"]) >= 1
    assert resp.headers["x-ratelimit-limit"] == "3"
    assert resp.headers["x-ratelimit-remaining"] == "0"
    assert int(resp.headers["x-ratelimit-reset"]) > 0


def test_per_route_override_is_tighter_than_default():
    c = make_client(
        default_limit=RouteLimit(requests=100, window_seconds=60.0),
        route_limits={"/api/search": RouteLimit(requests=2, window_seconds=60.0)},
    )
    assert c.get("/api/search?q=a", headers=AUTH).status_code == 200
    assert c.get("/api/search?q=a", headers=AUTH).status_code == 200
    assert c.get("/api/search?q=a", headers=AUTH).status_code == 429
    # the default budget on other routes is untouched
    assert c.get("/api/echo", headers=AUTH).status_code == 200


def test_limits_are_per_key():
    from app.config import hash_key

    c = make_client(
        default_limit=RouteLimit(requests=1, window_seconds=60.0),
        api_key_hashes={hash_key("k1"): "one", hash_key("k2"): "two"},
    )
    assert c.get("/api/echo", headers={"X-API-Key": "k1"}).status_code == 200
    assert c.get("/api/echo", headers={"X-API-Key": "k1"}).status_code == 429
    assert c.get("/api/echo", headers={"X-API-Key": "k2"}).status_code == 200


def test_rate_limiting_can_be_disabled():
    c = make_client(
        rate_limit_enabled=False,
        default_limit=RouteLimit(requests=1, window_seconds=60.0),
    )
    for _ in range(5):
        resp = c.get("/api/echo", headers=AUTH)
        assert resp.status_code == 200
        assert "x-ratelimit-limit" not in resp.headers


def test_sliding_window_strategy_end_to_end():
    c = make_client(
        strategy="sliding_window",
        default_limit=RouteLimit(requests=2, window_seconds=60.0),
    )
    assert c.get("/api/echo", headers=AUTH).status_code == 200
    assert c.get("/api/echo", headers=AUTH).status_code == 200
    resp = c.get("/api/echo", headers=AUTH)
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


def test_health_reports_strategy_and_backend(client):
    body = client.get("/health").json()
    assert body["strategy"] == "token_bucket"
    assert body["limiter_backend"] == "memory"  # no redis configured in tests
