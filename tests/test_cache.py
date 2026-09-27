"""Digest cache: single-flight rebuild, error handling, invalidation."""

from __future__ import annotations

import threading
import time

from image_resolver.cache import DigestCache


def test_single_flight_loader_runs_once():
    cache = DigestCache()
    calls: list[int] = []
    start = threading.Event()

    def loader():
        calls.append(1)
        time.sleep(0.05)
        return {"v": 1}

    results: list[dict] = []

    def worker():
        start.wait()
        results.append(cache.get_or_load("k", loader))

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()
    assert calls == [1]
    assert results == [{"v": 1}] * 12
    assert len(cache) == 1


def test_cached_value_does_not_call_loader_again():
    cache = DigestCache()
    assert cache.get_or_load("k", lambda: "first") == "first"

    def must_not_run():
        raise AssertionError("loader must not run on cache hit")

    assert cache.get_or_load("k", must_not_run) == "first"


def test_loader_error_propagates_and_is_not_cached():
    cache = DigestCache()
    release = threading.Event()
    leader_started = threading.Event()

    def failing_loader():
        leader_started.set()
        release.wait(timeout=5)
        raise ValueError("boom")

    errors: list[str] = []

    def worker(index: int):
        if index != 0:
            leader_started.wait(timeout=5)
            time.sleep(0.02)
        try:
            cache.get_or_load("k", failing_loader)
        except ValueError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    release.set()
    for t in threads:
        t.join()

    assert errors == ["boom"] * 6
    assert "k" not in cache
    # A retry after failure succeeds.
    assert cache.get_or_load("k", lambda: "recovered") == "recovered"


def test_invalidate_one_and_all():
    cache = DigestCache()
    cache.get_or_load("a", lambda: 1)
    cache.get_or_load("b", lambda: 2)
    cache.invalidate("a")
    assert "a" not in cache and "b" in cache
    cache.invalidate()
    assert len(cache) == 0


def test_invalidation_during_inflight_load_is_not_published():
    cache = DigestCache()
    entered = threading.Event()

    def slow_loader():
        entered.set()
        time.sleep(0.1)
        return "stale"

    thread = threading.Thread(target=lambda: cache.get_or_load("k", slow_loader))
    thread.start()
    entered.wait()
    cache.invalidate("k")
    thread.join()
    assert "k" not in cache

    reloads: list[int] = []
    assert cache.get_or_load("k", lambda: (reloads.append(1), "fresh")[1]) == "fresh"
    assert reloads == [1]


def test_invalidate_all_during_inflight_load():
    cache = DigestCache()
    entered = threading.Event()

    thread = threading.Thread(
        target=lambda: cache.get_or_load(
            "k", lambda: (entered.set(), time.sleep(0.1), "v")[2]
        )
    )
    thread.start()
    entered.wait()
    cache.invalidate()
    thread.join()
    assert "k" not in cache
