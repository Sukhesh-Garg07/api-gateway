"""Rate limiters: token bucket and sliding window, two storage backends.

Every limiter exposes the same check() contract and semantics:

- Redis backend: state lives in Redis, updated atomically with Lua scripts,
  so limits hold across gateway replicas.
- Memory backend: state lives in process dicts guarded by a lock. Same
  semantics, but limits are per-process (fine for a single replica or
  local development without a Redis server).

check() returns a LimitResult describing the decision plus the values the
gateway copies into the X-RateLimit-* / Retry-After response headers.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass


@dataclass
class LimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: float = 0.0  # seconds until one more request would be allowed
    reset_after: float = 0.0  # seconds until the limit is fully reset


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------

_TOKEN_BUCKET_LUA = """
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * refill)
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = (1 - tokens) / refill
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], ttl)
local reset = (capacity - tokens) / refill
return {allowed, math.floor(tokens), tostring(retry), tostring(reset)}
"""


class TokenBucketLimiter:
    """Bucket of `limit` tokens refilling steadily over `window` seconds."""

    name = "token_bucket"

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def check(self, key: str, limit: int, window: float) -> LimitResult:
        if self._redis is not None:
            return self._check_redis(key, limit, window)
        return self._check_memory(key, limit, window)

    def _check_redis(self, key: str, limit: int, window: float) -> LimitResult:
        refill = limit / window
        ttl = max(1, int(window * 2))
        allowed, remaining, retry, reset = self._redis.eval(
            _TOKEN_BUCKET_LUA, 1, f"tb:{key}", limit, refill, time.time(), ttl
        )
        return LimitResult(
            allowed=bool(int(allowed)),
            limit=limit,
            remaining=int(remaining),
            retry_after=float(retry),
            reset_after=float(reset),
        )

    def _check_memory(self, key: str, limit: int, window: float) -> LimitResult:
        refill = limit / window
        now = time.time()
        with self._lock:
            tokens, ts = self._buckets.get(key, (float(limit), now))
            tokens = min(float(limit), tokens + max(0.0, now - ts) * refill)
            if tokens >= 1:
                tokens -= 1
                allowed, retry = True, 0.0
            else:
                allowed, retry = False, (1 - tokens) / refill
            self._buckets[key] = (tokens, now)
            return LimitResult(
                allowed=allowed,
                limit=limit,
                remaining=int(tokens),
                retry_after=retry,
                reset_after=(limit - tokens) / refill,
            )


# ---------------------------------------------------------------------------
# Sliding window
# ---------------------------------------------------------------------------

_SLIDING_WINDOW_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
local allowed = 0
local retry = 0
if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[4])
  count = count + 1
  allowed = 1
else
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  if oldest[2] then retry = (tonumber(oldest[2]) + window) - now end
end
redis.call('EXPIRE', KEYS[1], math.ceil(window))
local remaining = limit - count
local reset = 0
if count > 0 then
  local newest = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
  if newest[2] then reset = (tonumber(newest[2]) + window) - now end
end
return {allowed, remaining, tostring(retry), tostring(reset)}
"""


class SlidingWindowLimiter:
    """At most `limit` requests in any trailing `window`-second interval."""

    name = "sliding_window"

    def __init__(self, redis_client=None):
        self._redis = redis_client
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}  # key -> sorted timestamps

    @property
    def backend(self) -> str:
        return "redis" if self._redis is not None else "memory"

    def check(self, key: str, limit: int, window: float) -> LimitResult:
        if self._redis is not None:
            return self._check_redis(key, limit, window)
        return self._check_memory(key, limit, window)

    def _check_redis(self, key: str, limit: int, window: float) -> LimitResult:
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex}"
        allowed, remaining, retry, reset = self._redis.eval(
            _SLIDING_WINDOW_LUA, 1, f"sw:{key}", now, window, limit, member
        )
        return LimitResult(
            allowed=bool(int(allowed)),
            limit=limit,
            remaining=int(remaining),
            retry_after=float(retry),
            reset_after=float(reset),
        )

    def _check_memory(self, key: str, limit: int, window: float) -> LimitResult:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if t > now - window]
            if len(hits) < limit:
                hits.append(now)
                allowed, retry = True, 0.0
            else:
                allowed = False
                retry = (hits[0] + window) - now if hits else 0.0
            self._hits[key] = hits
            reset = (hits[-1] + window) - now if hits else 0.0
            return LimitResult(
                allowed=allowed,
                limit=limit,
                remaining=limit - len(hits),
                retry_after=retry,
                reset_after=reset,
            )


def build_limiter(strategy: str, redis_client=None):
    if strategy == "token_bucket":
        return TokenBucketLimiter(redis_client)
    if strategy == "sliding_window":
        return SlidingWindowLimiter(redis_client)
    raise ValueError(f"unknown rate limit strategy: {strategy}")
