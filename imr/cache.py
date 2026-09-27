"""Snapshot-versioned resolution cache with concurrent rebuild coalescing.

Keys are content-based (root digest + platform), and every entry records the
store ``version`` under which it was produced. An entry is only served while
its version equals the store's current version, so a commit invalidates all
derived results atomically ("no stale results after update"). Concurrent
misses for the same key share a single rebuild (single-flight): the loader
— and therefore any side effect — runs at most once per generation.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class _Pending(Generic[T]):
    __slots__ = ("event", "value", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: T | None = None
        self.error: BaseException | None = None


class ResolutionCache(Generic[T]):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[object, tuple[int, T]] = {}
        self._inflight: dict[tuple[object, int], _Pending[T]] = {}

    def get_or_load(
        self,
        key: object,
        version: int,
        loader: Callable[[], T],
    ) -> T:
        """Return a current cached value or run one loader for all waiters."""
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and hit[0] == version:
                return hit[1]

            inflight_key = (key, version)
            pending = self._inflight.get(inflight_key)
            leader: bool
            if pending is None:
                pending = _Pending()
                self._inflight[inflight_key] = pending
                leader = True
            else:
                leader = False

        if not leader:
            pending.event.wait()
            if pending.error is not None:
                raise pending.error
            return pending.value  # type: ignore[return-value]

        # Leader performs the single rebuild.
        try:
            value = loader()
        except BaseException as exc:
            with self._lock:
                self._inflight.pop((key, version), None)
            pending.error = exc
            pending.event.set()
            raise
        with self._lock:
            self._inflight.pop((key, version), None)
            self._data[key] = (version, value)
        pending.value = value
        pending.event.set()
        return value

    def invalidate(self, key: object) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        """Invalidate all entries (called after an atomic commit)."""
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
