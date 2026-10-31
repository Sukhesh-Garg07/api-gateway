"""Prometheus metrics, on a private registry so tests can build many apps."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest


class GatewayMetrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "gateway_requests_total",
            "Requests handled by the gateway, by route and response status.",
            ["route", "status"],
            registry=self.registry,
        )
        self.rejections = Counter(
            "gateway_rate_limit_rejections_total",
            "Requests rejected with 429 by the rate limiter, by route.",
            ["route"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "gateway_request_latency_seconds",
            "Gateway request latency in seconds, by route.",
            ["route"],
            registry=self.registry,
        )

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
