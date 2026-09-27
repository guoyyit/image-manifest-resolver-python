"""Cache: concurrent single-flight rebuild, version invalidation, no stale."""

from __future__ import annotations

import threading

from imr.cache import ResolutionCache
from imr.models import Platform

from conftest import build_image, commit_images

AMD = Platform("linux", "amd64")
ARM = Platform("linux", "arm64")


def test_concurrent_miss_runs_loader_once():
    cache: ResolutionCache[str] = ResolutionCache()
    counter = {"n": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def loader():
        with lock:
            counter["n"] += 1
        return "value"

    results: list[str] = []
    errors: list[BaseException] = []

    def worker():
        barrier.wait()  # align all callers before the single-flight window
        try:
            results.append(cache.get_or_load("k", 1, loader))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert counter["n"] == 1
    assert results == ["value"] * 8


def test_stale_entry_is_not_served_after_version_advance():
    cache: ResolutionCache[int] = ResolutionCache()
    assert cache.get_or_load("k", 1, lambda: 10) == 10
    assert cache.get_or_load("k", 1, lambda: 0) == 10  # cached
    assert cache.get_or_load("k", 2, lambda: 20) == 20  # rebuilt
    # old version entry is overwritten by the newer one
    assert cache.get_or_load("k", 2, lambda: 0) == 20
    assert len(cache) == 1


def test_clear_invalidates_everything():
    cache: ResolutionCache[int] = ResolutionCache()
    cache.get_or_load("a", 1, lambda: 1)
    cache.get_or_load("b", 1, lambda: 2)
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0
    calls = []
    cache.get_or_load("a", 1, lambda: calls.append(1) or 9)
    assert calls == [1]


def test_invalidate_single_key():
    cache: ResolutionCache[int] = ResolutionCache()
    cache.get_or_load("a", 1, lambda: 1)
    cache.invalidate("a")
    calls = []
    assert cache.get_or_load("a", 1, lambda: calls.append(1) or 2) == 2
    assert calls == [1]


def test_resolution_cache_invalidated_after_commit(service):
    img1 = build_image(AMD, [b"layer-v1"])
    commit_images(service, [img1], tag="app:latest", index=False)
    r1 = service.resolve("app:latest", AMD)
    assert r1.manifest_digest == img1.manifest_digest

    # Cache is warm now. Publish a new manifest under the same tag.
    img2 = build_image(AMD, [b"layer-v2"])
    commit_images(service, [img2], tag="app:latest", index=False)
    r2 = service.resolve("app:latest", AMD)
    assert r2.manifest_digest == img2.manifest_digest
    assert r2.manifest_digest != r1.manifest_digest


def test_concurrent_resolution_loads_each_key_once(service):
    amd = build_image(AMD, [b"a1", b"a2"])
    arm = build_image(ARM, [b"r1"])
    commit_images(service, [arm, amd], tag="multi:v1")

    results_amd: list[str] = []
    results_arm: list[str] = []

    def resolve(target, sink):
        sink.append(service.resolve("multi:v1", target).manifest_digest)

    threads = []
    for _ in range(10):
        threads.append(threading.Thread(target=resolve, args=(AMD, results_amd)))
        threads.append(threading.Thread(target=resolve, args=(ARM, results_arm)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(results_amd) == {amd.manifest_digest}
    assert set(results_arm) == {arm.manifest_digest}
    assert len(results_amd) == 10 and len(results_arm) == 10


def test_failing_loader_is_not_cached():
    cache: ResolutionCache[int] = ResolutionCache()
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ValueError("boom")

    for _ in range(3):
        try:
            cache.get_or_load("k", 1, bad)
        except ValueError:
            pass
    # Each caller attempts its own rebuild; failures are never cached.
    assert calls["n"] == 3

    # Once healthy, value is cached.
    assert cache.get_or_load("k", 1, lambda: 42) == 42
    assert cache.get_or_load("k", 1, bad) == 42
