import time

import fakeredis
import pytest

from app.ratelimit import SlidingWindowLimiter, TokenBucketLimiter

LIMITERS = [TokenBucketLimiter, SlidingWindowLimiter]


def _both_backends(cls):
    return pytest.mark.parametrize(
        "limiter", [cls(), cls(fakeredis.FakeRedis(decode_responses=True))], ids=["memory", "redis"]
    )


@_both_backends(TokenBucketLimiter)
def test_token_bucket_allows_up_to_limit_then_denies(limiter):
    results = [limiter.check("k", limit=3, window=60.0) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[0].remaining == 2
    assert results[2].remaining == 0
    denied = results[3]
    assert denied.remaining == 0
    assert denied.retry_after > 0
    assert denied.limit == 3


@_both_backends(SlidingWindowLimiter)
def test_sliding_window_allows_up_to_limit_then_denies(limiter):
    results = [limiter.check("k", limit=3, window=60.0) for _ in range(4)]
    assert [r.allowed for r in results] == [True, True, True, False]
    assert results[0].remaining == 2
    denied = results[3]
    assert denied.remaining == 0
    assert 0 < denied.retry_after <= 60.0


@pytest.mark.parametrize("cls", LIMITERS)
def test_limiters_are_per_key(cls):
    limiter = cls()
    for _ in range(2):
        assert limiter.check("alice", limit=2, window=60.0).allowed
    assert not limiter.check("alice", limit=2, window=60.0).allowed
    # a different key has its own budget
    assert limiter.check("bob", limit=2, window=60.0).allowed


@pytest.mark.parametrize("cls", LIMITERS)
def test_window_expiry_frees_capacity(cls):
    limiter = cls()
    assert limiter.check("k", limit=1, window=0.5).allowed
    assert not limiter.check("k", limit=1, window=0.5).allowed
    time.sleep(0.7)
    assert limiter.check("k", limit=1, window=0.5).allowed


def test_token_bucket_refills_gradually():
    limiter = TokenBucketLimiter()
    assert limiter.check("k", limit=2, window=1.0).allowed
    assert limiter.check("k", limit=2, window=1.0).allowed
    assert not limiter.check("k", limit=2, window=1.0).allowed
    time.sleep(0.6)  # ~1.2 tokens refilled at 2 tokens/sec
    assert limiter.check("k", limit=2, window=1.0).allowed


def test_backends_report_names():
    assert TokenBucketLimiter().backend == "memory"
    assert SlidingWindowLimiter(fakeredis.FakeRedis(decode_responses=True)).backend == "redis"
