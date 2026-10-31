"""Mini API gateway.

Proxies /api/* to an upstream service behind API-key auth and per-key,
per-route rate limiting. Own endpoints: /health and /metrics (no auth).

Run:  uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import json
import logging
import math
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .auth import KeyStore
from .config import Settings
from .metrics import GatewayMetrics
from .ratelimit import build_limiter

logger = logging.getLogger("gateway")
access_log = logging.getLogger("gateway.access")
# uvicorn configures only its own loggers; without this the access log's
# INFO records would be dropped by the default WARNING root level.
if not access_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    access_log.addHandler(_handler)
access_log.setLevel(logging.INFO)
access_log.propagate = False

HOP_BY_HOP = {"host", "content-length", "connection", "x-api-key"}


def _connect_redis(settings: Settings):
    """Return a Redis client if a URL is configured and reachable, else None."""
    if not settings.redis_url:
        return None
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1.0)
        client.ping()
        return client
    except Exception as exc:  # fall back to memory, but say so loudly
        logger.warning("redis at %s unreachable (%s); using in-memory limiter", settings.redis_url, exc)
        return None


def create_app(settings: Settings | None = None, upstream_transport=None) -> FastAPI:
    settings = settings or Settings.from_env()
    keystore = KeyStore(settings)
    limiter = build_limiter(settings.strategy, _connect_redis(settings))
    metrics = GatewayMetrics()

    app = FastAPI(title="mini api gateway", version="1.0.0")
    app.state.settings = settings
    app.state.limiter = limiter

    # One shared, pooled client for upstream calls. Creating an httpx client
    # per request would throw away keep-alive and dominate latency under load.
    _client: dict[str, httpx.AsyncClient] = {}

    def upstream_client() -> httpx.AsyncClient:
        if "client" not in _client:
            _client["client"] = httpx.AsyncClient(
                base_url=settings.upstream_url,
                transport=upstream_transport,
                timeout=10.0,
                trust_env=False,  # upstream is configured directly; env proxies must not apply
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            )
        return _client["client"]

    if settings.using_demo_key:
        logger.warning("no API keys configured; demo key 'demo-key' (key id 'demo') is active")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "upstream": settings.upstream_url,
            "rate_limit_enabled": settings.rate_limit_enabled,
            "strategy": settings.strategy,
            "limiter_backend": limiter.backend,
        }

    @app.get("/metrics")
    async def metrics_endpoint():
        body, content_type = metrics.render()
        return Response(content=body, media_type=content_type)

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy(path: str, request: Request):
        started = time.perf_counter()
        route = f"/api/{path}"
        status = 500
        key_id = "-"

        def finish(resp: Response) -> Response:
            latency = time.perf_counter() - started
            metrics.requests.labels(route=route, status=str(resp.status_code)).inc()
            metrics.latency.labels(route=route).observe(latency)
            access_log.info(
                json.dumps(
                    {
                        "key_id": key_id,
                        "method": request.method,
                        "route": route,
                        "status": resp.status_code,
                        "latency_ms": round(latency * 1000, 2),
                    }
                )
            )
            return resp

        # --- auth -----------------------------------------------------------
        key_id = keystore.authenticate(request.headers.get("x-api-key")) or "-"
        if key_id == "-":
            return finish(
                JSONResponse({"detail": "missing or invalid api key"}, status_code=401)
            )

        # --- rate limit -----------------------------------------------------
        extra_headers: dict[str, str] = {}
        if settings.rate_limit_enabled:
            limit = settings.limit_for(route)
            result = limiter.check(f"{key_id}:{route}", limit.requests, limit.window_seconds)
            extra_headers = {
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(max(0, result.remaining)),
                "X-RateLimit-Reset": str(int(time.time() + result.reset_after)),
            }
            if not result.allowed:
                metrics.rejections.labels(route=route).inc()
                extra_headers["Retry-After"] = str(max(1, math.ceil(result.retry_after)))
                return finish(
                    JSONResponse(
                        {"detail": "rate limit exceeded"},
                        status_code=429,
                        headers=extra_headers,
                    )
                )

        # --- proxy ----------------------------------------------------------
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
        }
        headers["x-api-key-id"] = key_id
        try:
            upstream_resp = await upstream_client().request(
                request.method,
                f"/{path}",
                params=request.query_params,
                content=body,
                headers=headers,
            )
            status = upstream_resp.status_code
            resp_headers = {
                k: v
                for k, v in upstream_resp.headers.items()
                if k.lower() not in HOP_BY_HOP
            }
            resp_headers.update(extra_headers)
            return finish(
                Response(
                    content=upstream_resp.content,
                    status_code=status,
                    headers=resp_headers,
                    media_type=upstream_resp.headers.get("content-type"),
                )
            )
        except httpx.HTTPError:
            return finish(
                JSONResponse(
                    {"detail": "upstream unavailable"},
                    status_code=502,
                    headers=extra_headers,
                )
            )

    return app


app = create_app()
