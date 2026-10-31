from conftest import AUTH, make_client


def test_health_needs_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_missing_key_is_401(client):
    resp = client.get("/api/echo")
    assert resp.status_code == 401


def test_empty_key_is_401(client):
    resp = client.get("/api/echo", headers={"X-API-Key": ""})
    assert resp.status_code == 401


def test_wrong_key_is_401(client):
    resp = client.get("/api/echo", headers={"X-API-Key": "not-the-key"})
    assert resp.status_code == 401


def test_valid_key_proxies_to_upstream(client):
    resp = client.get("/api/echo", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["method"] == "GET"
    # gateway forwards the authenticated key id, not the secret
    assert body["api_key_id"] == "tester"


def test_key_id_is_per_key():
    from app.config import hash_key

    c = make_client(api_key_hashes={hash_key("alpha"): "team-a", hash_key("beta"): "team-b"})
    assert c.get("/api/echo", headers={"X-API-Key": "alpha"}).json()["api_key_id"] == "team-a"
    assert c.get("/api/echo", headers={"X-API-Key": "beta"}).json()["api_key_id"] == "team-b"
    assert c.get("/api/echo", headers={"X-API-Key": "gamma"}).status_code == 401
