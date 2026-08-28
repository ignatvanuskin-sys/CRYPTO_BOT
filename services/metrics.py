from __future__ import annotations

from collections import Counter
from threading import Lock


_COUNTERS: Counter[str] = Counter()
_LOCK = Lock()


def increment(name: str, value: int = 1) -> None:
    """Increment an in-memory metric; values reset when the process restarts."""
    with _LOCK:
        _COUNTERS[name] += value


def snapshot() -> dict[str, int]:
    with _LOCK:
        return dict(sorted(_COUNTERS.items()))


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
