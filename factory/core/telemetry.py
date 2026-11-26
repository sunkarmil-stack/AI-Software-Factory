"""OpenTelemetry-compatible metrics with plane separation (REQ 054) and redaction (REQ 051).

Planes: factory (control plane health), delivery (delivery outcomes), product (product
agent health). Failure categories distinguish queue backlog, provider throttling, failed
validation, application errors and business exceptions.
"""
from __future__ import annotations

import logging

from .db import DB, j
from .evidence import redact
from .util import now

PLANES = {"factory", "delivery", "product"}
FAILURE_CATEGORIES = {"queue_backlog", "provider_throttled", "validation_failed", "application_error",
                      "business_exception", "policy_denied", "budget_exceeded", "tool_unavailable"}

log = logging.getLogger("factory")


class Telemetry:
    def __init__(self, db: DB):
        self.db = db

    def metric(self, plane: str, name: str, value: float = 1.0, **labels) -> None:
        assert plane in PLANES, plane
        if "category" in labels:
            assert labels["category"] in FAILURE_CATEGORIES, labels["category"]
        self.db.execute("INSERT INTO metrics(plane,name,labels,value,ts) VALUES(?,?,?,?,?)",
                        (plane, name, j(redact(labels)), value, now()))

    def log(self, msg: str, **fields) -> None:
        log.info("%s %s", redact(msg), j(redact(fields)))

    def summary(self) -> dict:
        out: dict = {p: {} for p in PLANES}
        for r in self.db.all("SELECT plane,name,labels,SUM(value) v,COUNT(*) n FROM metrics GROUP BY plane,name,labels"):
            out[r["plane"]].setdefault(r["name"], []).append({"labels": r["labels"], "value": r["v"]})
        return out
