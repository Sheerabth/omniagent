"""Prometheus metrics for the control plane HTTP surface."""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUEST_COUNT = Counter(
    "omniagent_http_requests_total",
    "HTTP requests by method, route template, and status code",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "omniagent_http_request_duration_seconds",
    "HTTP request latency by method and route template",
    ["method", "path"],
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
