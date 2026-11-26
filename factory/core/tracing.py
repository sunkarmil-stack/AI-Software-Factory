"""Minimal OpenTelemetry-compatible tracing with OTLP/HTTP JSON export.

The OTel SDK is not available in the reference build environment, so this module emits spans in
the OTLP JSON encoding (POST {endpoint}/v1/traces) and propagates W3C `traceparent` to the
enterprise AI gateway. In production it can be swapped for the OTel SDK without changing call
sites (`span(name, **attrs)`). Attribute values are redacted before export.
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import urllib.request
from contextlib import contextmanager

from .evidence import redact

_ctx: contextvars.ContextVar = contextvars.ContextVar("factory_span", default=None)
_corr: contextvars.ContextVar = contextvars.ContextVar("factory_correlation", default={})


@contextmanager
def correlate(**ids):
    """Correlation ids (run, actor, project) propagated as x-factory-* headers to the AI gateway."""
    token = _corr.set({**_corr.get(), **{k: v for k, v in ids.items() if v}})
    try:
        yield
    finally:
        _corr.reset(token)


def current_correlation() -> dict:
    return dict(_corr.get())


def _hex(n: int) -> str:
    return os.urandom(n).hex()


def current_traceparent() -> str | None:
    s = _ctx.get()
    return f"00-{s['traceId']}-{s['spanId']}-01" if s else None


class Tracer:
    def __init__(self, service: str = "ai-software-factory", endpoint: str = ""):
        self.service, self.endpoint = service, endpoint.rstrip("/")
        self.buffer: list[dict] = []
        self.lock = threading.Lock()

    @contextmanager
    def span(self, name: str, **attrs):
        parent = _ctx.get()
        s = {"traceId": parent["traceId"] if parent else _hex(16), "spanId": _hex(8),
             "parentSpanId": parent["spanId"] if parent else "", "name": name,
             "startTimeUnixNano": str(time.time_ns()), "attributes": attrs, "status": {"code": 1}}
        token = _ctx.set(s)
        try:
            yield s
        except BaseException as e:
            s["status"] = {"code": 2, "message": type(e).__name__}
            raise
        finally:
            _ctx.reset(token)
            s["endTimeUnixNano"] = str(time.time_ns())
            with self.lock:
                self.buffer.append(s)
                full = len(self.buffer) >= 200
            if full:
                self.flush()

    def _otlp(self, spans: list[dict]) -> dict:
        def kv(d):
            return [{"key": k, "value": {"stringValue": str(v)}} for k, v in redact(d).items()]
        return {"resourceSpans": [{"resource": {"attributes": kv({"service.name": self.service})},
                                   "scopeSpans": [{"scope": {"name": "factory"}, "spans": [
                                       {**{k: v for k, v in s.items() if k not in ("attributes", "parentSpanId")},
                                        **({"parentSpanId": s["parentSpanId"]} if s["parentSpanId"] else {}),
                                        "kind": 1, "attributes": kv(s["attributes"])} for s in spans]}]}]}

    def flush(self) -> int:
        with self.lock:
            spans, self.buffer = self.buffer, []
        if not spans or not self.endpoint:
            return 0
        req = urllib.request.Request(self.endpoint + "/v1/traces", data=json.dumps(self._otlp(spans)).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except OSError:
            with self.lock:   # keep for the next attempt; telemetry loss must not break delivery
                self.buffer = spans[-1000:] + self.buffer
            return 0
        return len(spans)


TRACER = Tracer()
