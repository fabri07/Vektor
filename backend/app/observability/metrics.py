"""
Basic application metrics.

Uses simple counters stored in a dict for now.
Replace with Prometheus client if needed:
  pip install prometheus-fastapi-instrumentator
  from prometheus_fastapi_instrumentator import Instrumentator
  Instrumentator().instrument(app).expose(app)
"""

from collections import defaultdict
from typing import Any

_counters: dict[str, int] = defaultdict(int)


def increment(name: str, labels: dict[str, Any] | None = None) -> None:
    key = name
    if labels:
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        key = f"{name}{{{label_str}}}"
    _counters[key] += 1


def get_counters() -> dict[str, int]:
    return dict(_counters)
