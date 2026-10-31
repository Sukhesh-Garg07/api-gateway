# Mini API Gateway

A small API gateway in FastAPI: API-key authentication, per-key and per-route
rate limiting (token bucket **and** sliding window), Prometheus metrics, and
structured request logging, in front of any HTTP upstream.

This grew out of a standalone "distributed rate limiter with Redis" project.
The limiter is still the interesting part; now it sits inside a gateway that
also handles auth, proxying, headers, and observability, which is where rate
limiting actually earns its keep.

## Features

- **API-key auth** -- clients send `X-API-Key`; the gateway stores only
  SHA-256 hashes of secrets and compares with `hmac.compare_digest`.
  Missing/invalid key: `401`.
- **Two rate-limit strategies**, switchable by config:
  - *Token bucket* -- a bucket of N tokens refilling steadily; allows bursts
    up to the bucket size.
  - *Sliding window* -- at most N requests in any trailing window; no burst
    credit, stricter and easier to reason about.
- **Two storage backends with identical semantics**: Redis (state updated
  atomically via Lua scripts, correct across gateway replicas) and in-memory
  (per-process, zero dependencies). Redis is used automatically when
  `GATEWAY_REDIS_URL` points at a reachable server; otherwise the gateway
  falls back to memory and logs a warning.
- **Per-route overrides** -- e.g. `/api/search` can have a tighter limit than
  the default; limits are tracked per (api key, route) pair.
- **Standards-shaped rejections** -- `429` with `Retry-After` plus
  `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers
  (the success path carries the `X-RateLimit-*` headers too).
- **Prometheus metrics** at `/metrics`: request counts by route and status,
  rate-limit rejections by route, and a latency histogram.
- **Structured access log** -- one JSON line per request with key id, method,
  route, status, and latency in ms. (The key *id*, never the secret.)
- **Bundled demo upstream** so the whole stack runs standalone.

## Architecture

```
client ──X-API-Key──▶ ┌────────────── gateway :8000 ──────────────┐
                      │  auth (sha256 + compare_digest)           │
                      │  rate limit check ──▶ limiter ──▶ Redis   │
                      │        │ 429 + Retry-After   or in-memory │
                      │        ▼ allowed                          │
                      │  proxy (shared pooled httpx client)       │
                      └──────────────┬────────────────────────────┘
                                     ▼
                             upstream service :9000
                      (bundled demo: /echo /search /items /slow)

        /health and /metrics live on the gateway itself, no auth needed.
        Every request is counted in Prometheus metrics and the JSON access log.
```

Request flow: authenticate -> resolve the effective limit for (key, route)
-> ask the limiter -> reject with 429 or proxy to the upstream and stream the
response back with rate-limit headers attached.

## Quick start

### Without Redis (in-memory limiter)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn upstream.main:app --port 9000 &          # demo upstream
uvicorn app.main:app --port 8000                 # gateway

curl -H "X-API-Key: demo-key" http://127.0.0.1:8000/api/echo
```

With no keys configured, a demo key is active (`demo-key`, key id `demo`)
and the gateway logs a warning saying so.

### With Redis

```bash
redis-server &
GATEWAY_REDIS_URL=redis://127.0.0.1:6379/0 uvicorn app.main:app --port 8000
curl http://127.0.0.1:8000/health   # "limiter_backend": "redis"
```

### Docker Compose (redis + upstream + gateway)

```bash
docker compose up --build
curl -H "X-API-Key: demo-key" http://127.0.0.1:8000/api/search?q=test
```

## API keys

Keys are configured by id, secrets are only ever stored hashed:

```bash
# env form: "key-id=secret" pairs (hashed in memory at startup)
GATEWAY_API_KEYS="billing=supersecret,partner=anothersecret"

# file form: JSON of pre-hashed keys, hashed at rest
python scripts/make_key.py            # generates a secret + its sha256 hash
echo '{"billing": "<hash from make_key.py>"}' > keys.json
GATEWAY_API_KEYS_FILE=keys.json
```

The gateway forwards the authenticated **key id** to the upstream as
`X-Api-Key-Id` (handy for upstream logging/billing); the secret itself is
stripped from the proxied request.

## Configuration

All via environment variables (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `GATEWAY_UPSTREAM_URL` | `http://127.0.0.1:9000` | upstream base URL |
| `GATEWAY_REDIS_URL` | unset | redis URL; unset/unreachable = in-memory limiter |
| `GATEWAY_API_KEYS` | unset | `key-id=secret,...` pairs |
| `GATEWAY_API_KEYS_FILE` | unset | JSON file of `key-id -> sha256(secret)` |
| `GATEWAY_RATE_LIMIT_ENABLED` | `true` | master switch |
| `GATEWAY_RATE_LIMIT_STRATEGY` | `token_bucket` | or `sliding_window` |
| `GATEWAY_RATE_LIMIT_REQUESTS` | `100` | default limit per key+route |
| `GATEWAY_RATE_LIMIT_WINDOW` | `60` | window seconds for the default limit |
| `GATEWAY_ROUTE_LIMITS` | unset | JSON, e.g. `{"/api/search": {"requests": 10, "window": 60}}` |

Route overrides match exactly first, then by longest prefix.

## What a rejection looks like

With a limit of 3 requests/minute, the 4th request gets:

```
HTTP/1.1 429 Too Many Requests
x-ratelimit-limit: 3
x-ratelimit-remaining: 0
x-ratelimit-reset: 1793481526
retry-after: 20

{"detail": "rate limit exceeded"}
```

## Metrics

`GET /metrics` exposes, among the usual process stats:

```
gateway_requests_total{route="/api/echo",status="200"} 69.0
gateway_requests_total{route="/api/echo",status="429"} 491.0
gateway_rate_limit_rejections_total{route="/api/echo"} 491.0
gateway_request_latency_seconds_bucket{le="0.05",route="/api/echo"} ...
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

27 tests, all passing: auth (401 cases, per-key identity), both limiter
algorithms against **both** backends (the Redis path runs on fakeredis with
Lua enabled, so the exact production Lua scripts are exercised), per-route
overrides, per-key isolation, 429 behaviour and headers, window expiry and
token refill, and the metrics endpoint.

## Load test

`loadtest/loadtest.py` is a small asyncio client that hammers one route with
a fixed worker pool and reports percentiles from the latencies it actually
observed. Reproduce with:

```bash
python loadtest/loadtest.py --duration 10 --concurrency 25
```

### Measured results

Context, so the numbers mean something: gateway and upstream each ran as a
**single uvicorn process** on a shared 4-vCPU sandbox VM (load average ~2
from unrelated parallel work during the runs), in-memory limiter, route
`/api/echo`, 25 concurrent workers for 10 s per run. Treat the absolute
numbers as "this contended box", and the *comparison* as the result. For
reference, the same load aimed straight at the upstream (no gateway) gave
51.1 req/s, so the sandbox itself is the ceiling here, not the gateway.

| Scenario | Throughput | p50 | p95 | p99 | Outcomes |
|---|---|---|---|---|---|
| rate limiting **off** | 35.4 req/s | 520 ms | 1604 ms | 2878 ms | 386 x 200 |
| token bucket on, generous limit (100k/min) | 31.2 req/s | 574 ms | 1809 ms | 2582 ms | 330 x 200, 0 rejected |
| sliding window on, generous limit (100k/min) | 31.4 req/s | 597 ms | 1902 ms | 2508 ms | 320 x 200, 0 rejected |
| token bucket, tight limit (60/min per key) | 55.0 req/s | 206 ms | 1661 ms | 2573 ms | 69 x 200, **491 x 429** |

Takeaways:

- Turning the limiter on costs roughly **10% throughput** on this box
  (31 vs 35 req/s); the two strategies are indistinguishable within noise.
- The tight-limit run is faster overall because rejections skip the upstream
  hop: exactly 60 requests were admitted (plus ~9 refilled tokens over the
  run) and the other 491 were rejected cheaply at the gateway. That is the
  point of a gateway limiter -- overload is shed before it reaches the
  backend.

## Design trade-offs

- **Token bucket vs sliding window**: token bucket permits short bursts
  (friendlier for real clients, one Lua script, O(1) state per key). Sliding
  window is a hard ceiling over any trailing interval but stores one sorted
  set member per request, so it costs more memory at high limits. The
  gateway makes it a config choice rather than a religion.
- **Redis vs memory**: Redis makes limits *global* across replicas and
  survives restarts; the atomic Lua scripts avoid race conditions between
  concurrent checks. Memory is per-process -- with 3 replicas your effective
  limit is 3x the configured one. It exists so the gateway is runnable and
  testable anywhere, and the fallback is loud (health endpoint + warning
  log), never silent.
- **Hashed keys, id forwarded**: storing hashes means a config/file leak
  does not leak usable keys. Forwarding only the key id keeps the upstream
  able to attribute traffic without ever seeing secrets.
- **Shared pooled upstream client**: one `httpx.AsyncClient` with keep-alive
  for all proxied traffic. (An early version created a client per request;
  it was measurably awful under load and is the kind of bug a load test
  exists to catch.)
- **Fail-open vs fail-closed on limiter errors**: if Redis dies mid-run the
  Redis client raises rather than silently admitting everything; the
  *startup* fallback to memory is explicit and visible in `/health`.

## Next steps

- Key rotation and per-key limit tiers (free/pro) in the keys file.
- Distributed tracing headers / request ids propagated to the upstream.
- A `Retry-After`-aware sample client and a Grafana dashboard for the
  Prometheus metrics.
- Streaming proxy support (SSE / chunked responses) and websocket proxying.
- Load-test on an idle multi-core box with multiple gateway workers to get
  numbers that reflect the code rather than the sandbox.

## Layout

```
app/         gateway: config, auth, ratelimit (both strategies/backends),
             metrics, main (proxy + endpoints)
upstream/    tiny demo upstream the gateway proxies to
tests/       pytest suite (auth, limiters, gateway behaviour, metrics)
loadtest/    asyncio load tester used for the numbers above
scripts/     make_key.py -- generate a key + stored hash
```
