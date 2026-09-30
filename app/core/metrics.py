"""Small dependency-free Prometheus exposition for HTTP service health."""

import threading
import time
from collections import Counter, defaultdict

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: Counter[tuple[str, str, int]] = Counter()
        self.duration_seconds: defaultdict[tuple[str, str], float] = defaultdict(float)

    def record(self, method: str, path: str, status: int, duration: float) -> None:
        # Route templates are not reliably available in bare ASGI middleware;
        # use a bounded high-level path to avoid unbounded label cardinality.
        label = path if path in {"/health", "/db-health", "/metrics"} else "api"
        with self._lock:
            self.requests[(method, label, status)] += 1
            self.duration_seconds[(method, label)] += duration

    def render(self) -> str:
        with self._lock:
            lines = ["# HELP governix_http_requests_total Completed HTTP requests.", "# TYPE governix_http_requests_total counter"]
            lines += [f'governix_http_requests_total{{method="{m}",path="{p}",status="{s}"}} {n}' for (m, p, s), n in sorted(self.requests.items())]
            lines += ["# HELP governix_http_request_duration_seconds_total Total HTTP request duration.", "# TYPE governix_http_request_duration_seconds_total counter"]
            lines += [f'governix_http_request_duration_seconds_total{{method="{m}",path="{p}"}} {v:.6f}' for (m, p), v in sorted(self.duration_seconds.items())]
        return "\n".join(lines) + "\n"


class MetricsMiddleware:
    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app, self.metrics = app, metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started, status = time.perf_counter(), 500

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            self.metrics.record(scope["method"], scope["path"], status, time.perf_counter() - started)
