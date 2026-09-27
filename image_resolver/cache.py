"""Digest-keyed cache with single-flight rebuild and explicit invalidation.

Concurrency guarantees:

* Every ``get``/``set``/``invalidate`` is guarded by one lock.
* When several threads miss on the same key concurrently, the loader runs
  **exactly once**; the other threads block and receive the same result.
* A loader that raises propagates the error to every waiting thread and caches
  nothing, so a later request retries rather than seeing a poisoned entry.
* ``invalidate()`` of a key with an in-flight loader waits for that load to
  finish (so no value is cached under a generation that was already
  invalidated) then removes the entry.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Inflight(Generic[T]):
    event: threading.Event
    value: T | None = None
    error: BaseException | None = None


class DigestCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, Any] = {}
        self._inflight: dict[str, _Inflight[Any]] = {}
        # Keys invalidated while their loader was in flight.
        self._invalidated: set[str] = set()

    def get_or_load(self, key: str, loader: Callable[[], T]) -> T:
        """Return cached value for *key* or invoke *loader* exactly once."""
        with self._lock:
            if key in self._values:
                return self._values[key]
            existing = self._inflight.get(key)
            if existing is None:
                inflight: _Inflight[T] = _Inflight(event=threading.Event())
                self._inflight[key] = inflight
                leader = True
            else:
                inflight = existing
                leader = False

        if not leader:
            inflight.event.wait()
            if inflight.error is not None:
                raise inflight.error
            return inflight.value  # type: ignore[return-value]

        try:
            result = loader()
        except BaseException as exc:  # propagate to all waiters, cache nothing
            with self._lock:
                self._inflight.pop(key, None)
                self._invalidated.discard(key)
                inflight.error = exc
                inflight.event.set()
            raise
        with self._lock:
            self._inflight.pop(key, None)
            # Do not publish if the key was invalidated while loading.
            if key not in self._invalidated:
                self._values[key] = result
            self._invalidated.discard(key)
            inflight.value = result
            inflight.event.set()
        return result

    def invalidate(self, key: str | None = None) -> None:
        """Invalidate one key, or all entries when *key* is ``None``.

        Invalidating a key whose loader is still running marks the key so the
        in-flight result is not cached once the loader finishes.
        """
        with self._lock:
            if key is None:
                self._values.clear()
                self._invalidated.update(self._inflight)
            else:
                self._values.pop(key, None)
                if key in self._inflight:
                    self._invalidated.add(key)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._values

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def snapshot(self) -> dict[str, Any]:
        """Return a point-in-time copy of cached entries."""
        with self._lock:
            return dict(self._values)
