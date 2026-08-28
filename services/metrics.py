from __future__ import annotations

import atexit
import json
import os
import threading
import time
from collections import Counter
from pathlib import Path

_PERSIST_PATH = Path(os.getenv("METRICS_PATH", "metrics.json"))
_PERSIST_INTERVAL = float(os.getenv("METRICS_PERSIST_INTERVAL", "10"))  # seconds

_COUNTERS: Counter[str] = Counter()
_LOCK = threading.Lock()
_last_write = 0.0

# Load persisted metrics on import
try:
    if _PERSIST_PATH.exists():
        data = json.loads(_PERSIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            _COUNTERS.update({k: int(v) for k, v in data.items()})
except Exception:
    pass


def _do_write() -> None:
    try:
        _PERSIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PERSIST_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(_COUNTERS), ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PERSIST_PATH)
    except Exception:
        pass


def _persist_if_due() -> None:
    """Write at most once per _PERSIST_INTERVAL — cheap debounce, no blocking writes per increment."""
    global _last_write
    now = time.monotonic()
    if now - _last_write >= _PERSIST_INTERVAL:
        _last_write = now
        _do_write()


def _persist_now() -> None:
    global _last_write
    _last_write = time.monotonic()
    _do_write()


def increment(name: str, value: int = 1) -> None:
    """Increment in-memory metric; debounced flush to disk at most once per interval."""
    with _LOCK:
        _COUNTERS[name] += value
        _persist_if_due()


def snapshot() -> dict[str, int]:
    with _LOCK:
        return dict(sorted(_COUNTERS.items()))


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _persist_now()


def flush() -> None:
    """Force write (used on shutdown)."""
    with _LOCK:
        _persist_now()


atexit.register(flush)
