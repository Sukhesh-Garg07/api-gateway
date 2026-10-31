from app.config import RouteLimit
from conftest import AUTH, make_client


def _metrics_text(client) -> str:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    return resp.text


def test_metrics_count_requests_by_route_and_status(client):
    client.get("/api/echo", headers=AUTH)
    client.get("/api/echo", headers=AUTH)
    client.get("/api/echo")  # 401, still counted
    text = _metrics_text(client)
    assert 'gateway_requests_total{route="/api/echo",status="200"} 2.0' in text
    assert 'gateway_requests_total{route="/api/echo",status="401"} 1.0' in text


def test_metrics_include_latency_histogram(client):
    client.get("/api/echo", headers=AUTH)
    text = _metrics_text(client)
    assert 'gateway_request_latency_seconds_bucket{le="0.005",route="/api/echo"}' in text
    assert 'gateway_request_latency_seconds_count{route="/api/echo"} 1.0' in text


def test_metrics_count_rate_limit_rejections():
    c = make_client(default_limit=RouteLimit(requests=1, window_seconds=60.0))
    c.get("/api/echo", headers=AUTH)
    c.get("/api/echo", headers=AUTH)  # 429
    c.get("/api/echo", headers=AUTH)  # 429
    text = _metrics_text(c)
    assert 'gateway_rate_limit_rejections_total{route="/api/echo"} 2.0' in text
    assert 'gateway_requests_total{route="/api/echo",status="429"} 2.0' in text
