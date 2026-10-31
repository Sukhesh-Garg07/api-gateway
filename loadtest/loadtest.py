"""Async load tester for the gateway.

Hammers one gateway route with a fixed pool of concurrent workers for a
fixed duration and reports throughput and latency percentiles, computed
from the latencies actually observed in the run.

Usage:
    python loadtest/loadtest.py --duration 10 --concurrency 50 \
        --url http://127.0.0.1:8000/api/echo --key demo-key
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter

import httpx


def percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(round(pct / 100 * len(sorted_values) + 0.5)) - 1)
    return sorted_values[max(0, idx)]


async def worker(client: httpx.AsyncClient, url: str, key: str, deadline: float,
                 latencies: list[float], statuses: Counter) -> None:
    headers = {"X-API-Key": key} if key else {}
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            resp = await client.get(url, headers=headers)
            statuses[resp.status_code] += 1
        except httpx.HTTPError:
            statuses[-1] += 1
        latencies.append((time.perf_counter() - started) * 1000)


async def run(url: str, key: str, duration: float, concurrency: int) -> dict:
    latencies: list[float] = []
    statuses: Counter = Counter()
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2)
    async with httpx.AsyncClient(limits=limits, timeout=30.0, trust_env=False) as client:
        started = time.perf_counter()
        deadline = started + duration
        await asyncio.gather(
            *(worker(client, url, key, deadline, latencies, statuses) for _ in range(concurrency))
        )
        elapsed = time.perf_counter() - started
    latencies.sort()
    total = sum(statuses.values())
    return {
        "url": url,
        "duration_s": round(elapsed, 2),
        "concurrency": concurrency,
        "requests": total,
        "requests_per_second": round(total / elapsed, 1),
        "statuses": {str(k): v for k, v in sorted(statuses.items())},
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 2) if latencies else 0,
            "p50": round(percentile(latencies, 50), 2),
            "p95": round(percentile(latencies, 95), 2),
            "p99": round(percentile(latencies, 99), 2),
            "max": round(latencies[-1], 2) if latencies else 0,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="load test the gateway")
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/echo")
    parser.add_argument("--key", default="demo-key")
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=50)
    args = parser.parse_args()
    result = asyncio.run(run(args.url, args.key, args.duration, args.concurrency))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
