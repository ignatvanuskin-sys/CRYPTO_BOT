from __future__ import annotations

from collections import Counter
from threading import Lock
import json
import os
from pathlib import Path

_PERSIST_PATH = Path(os.getenv("METRICS_PATH", "metrics.json"))

_COUNTERS: Counter[str] = Counter()
_LOCK = Lock()

# Load persisted metrics on import
try:
    if _PERSIST_PATH.exists():
        data = json.loads(_PERSIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _COUNTERS.update({k: int(v) for k, v in data.items()})
except Exception:
    pass


def _persist() -> None:
    try:
        # Write to temp then rename for atomicity
        tmp = _PERSIST_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(_COUNTERS), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PERSIST_PATH)
    except Exception:
        pass


def increment(name: str, value: int = 1) -> None:
    """Increment metric; persisted to file for survival across restarts."""
    with _LOCK:
        _COUNTERS[name] += value
        _persist()


def snapshot() -> dict[str, int]:
    with _LOCK:
        return dict(sorted(_COUNTERS.items()))


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _persist()
