"""Gateway configuration, loaded from environment variables.

API keys are only ever stored as SHA-256 hashes. Raw secrets arrive via the
GATEWAY_API_KEYS env var (hashed in memory at startup) or via a JSON file
(GATEWAY_API_KEYS_FILE) that already contains hashes, so nothing secret is
kept at rest inside the process or the repo.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RouteLimit:
    requests: int
    window_seconds: float


DEFAULT_LIMIT = RouteLimit(requests=100, window_seconds=60.0)

# Built-in fallback so the gateway runs standalone for demos. The startup log
# prints a loud warning whenever this is in effect.
DEMO_KEY_ID = "demo"
DEMO_KEY_SECRET = "demo-key"


@dataclass
class Settings:
    upstream_url: str = "http://127.0.0.1:9000"
    redis_url: str | None = None
    rate_limit_enabled: bool = True
    strategy: str = "token_bucket"  # or "sliding_window"
    default_limit: RouteLimit = DEFAULT_LIMIT
    route_limits: dict[str, RouteLimit] = field(default_factory=dict)
    # sha256 hex digest of the secret -> key id
    api_key_hashes: dict[str, str] = field(default_factory=dict)
    using_demo_key: bool = False

    def limit_for(self, route: str) -> RouteLimit:
        """Route override wins: exact match, then longest prefix, else default."""
        if route in self.route_limits:
            return self.route_limits[route]
        best: str | None = None
        for prefix in self.route_limits:
            if route.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is not None:
            return self.route_limits[best]
        return self.default_limit

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.upstream_url = os.getenv("GATEWAY_UPSTREAM_URL", s.upstream_url)
        s.redis_url = os.getenv("GATEWAY_REDIS_URL") or None
        s.rate_limit_enabled = os.getenv("GATEWAY_RATE_LIMIT_ENABLED", "true").lower() not in (
            "0",
            "false",
            "no",
        )
        s.strategy = os.getenv("GATEWAY_RATE_LIMIT_STRATEGY", s.strategy)
        if s.strategy not in ("token_bucket", "sliding_window"):
            raise ValueError(f"unknown rate limit strategy: {s.strategy}")

        s.default_limit = RouteLimit(
            requests=int(os.getenv("GATEWAY_RATE_LIMIT_REQUESTS", str(DEFAULT_LIMIT.requests))),
            window_seconds=float(
                os.getenv("GATEWAY_RATE_LIMIT_WINDOW", str(DEFAULT_LIMIT.window_seconds))
            ),
        )

        raw_routes = os.getenv("GATEWAY_ROUTE_LIMITS")
        if raw_routes:
            parsed = json.loads(raw_routes)
            s.route_limits = {
                route: RouteLimit(
                    requests=int(cfg["requests"]), window_seconds=float(cfg["window"])
                )
                for route, cfg in parsed.items()
            }

        s.api_key_hashes = _load_key_hashes()
        if not s.api_key_hashes:
            s.api_key_hashes = {hash_key(DEMO_KEY_SECRET): DEMO_KEY_ID}
            s.using_demo_key = True
        return s


def _load_key_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}

    # File form: {"key-id": "<sha256 hex of secret>", ...} -- hashed at rest.
    path = os.getenv("GATEWAY_API_KEYS_FILE")
    if path:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for key_id, digest in data.items():
            hashes[digest.strip().lower()] = key_id

    # Env form: "key-id=secret,key-id-2=secret-2" -- hashed here, never stored raw.
    raw = os.getenv("GATEWAY_API_KEYS")
    if raw:
        for pair in raw.split(","):
            pair = pair.strip()
            if not pair:
                continue
            key_id, sep, secret = pair.partition("=")
            if not sep:
                raise ValueError("GATEWAY_API_KEYS entries must look like key-id=secret")
            hashes[hash_key(secret.strip())] = key_id.strip()

    return hashes
