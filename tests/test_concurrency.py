"""Concurrent updates must be atomic; readers see only complete versions."""

from __future__ import annotations

import threading

import pytest

from imr.digest import compute_digest
from imr.models import Platform
from imr.store import BatchUpdate, Snapshot

from conftest import build_image, commit_images

AMD = Platform("linux", "amd64")
ARM = Platform("linux", "arm64")


def _version_update(service, platform, tag):
    img = build_image(platform, [f"layer-{tag}".encode()])
    blobs = {img.config_digest: img.config_raw, **img.layer_blobs,
             img.manifest_digest: img.manifest_raw}
    return img, BatchUpdate(
        put_blobs=blobs,
        put_manifests={img.manifest_digest: service.build_manifest(
            img.manifest_digest, img.manifest_raw)},
        tag_updates={f"app:{tag}": img.manifest_digest},
    )


def test_concurrent_commits_all_apply_atomically_and_in_order(service):
    n = 20
    errors: list[BaseException] = []

    def worker(i: int):
        try:
            _version_update(service, AMD, f"t{i}")
            img, upd = _version_update(service, AMD, f"t{i}")
            service.commit(upd)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    snap = service.snapshot()
    # Exactly n versions applied, each numbered once, no gaps.
    assert snap.version == n
    for i in range(n):
        digest = snap.tags[f"app:t{i}"]
        obj = snap.manifests[digest]
        # Every published object has a complete, consistent blob chain.
        assert obj.layers
        for layer in obj.layers:
            assert layer.digest in snap.blobs
        assert obj.config_digest in snap.blobs


def test_concurrent_readers_never_observe_mixed_state(service):
    # Seed initial complete state.
    img0, upd0 = _version_update(service, AMD, "v0")
    service.commit(upd0)

    stop = threading.Event()
    bad: list[str] = []

    def reader():
        while not stop.is_set():
            snap = service.snapshot()
            for tag, target in snap.tags.items():
                obj = snap.manifests.get(target)
                if obj is None:
                    bad.append(f"tag {tag} points to missing object")
                    continue
                for layer in obj.layers:
                    if layer.digest not in snap.blobs:
                        bad.append("layer missing in observed snapshot")
                if obj.config_digest not in snap.blobs:
                    bad.append("config missing in observed snapshot")

    readers = [threading.Thread(target=reader) for _ in range(6)]
    for t in readers:
        t.start()

    for i in range(1, 25):
        _, upd = _version_update(service, AMD if i % 2 else ARM, f"v{i}")
        service.commit(upd)

    stop.set()
    for t in readers:
        t.join()
    assert not bad, bad[:3]


def test_failed_commit_under_concurrency_leaves_no_partial_state(service):
    img_ok, upd_ok = _version_update(service, AMD, "ok")
    service.commit(upd_ok)
    version_before = service.version

    barrier = threading.Barrier(4)

    def good(i):
        barrier.wait()
        _, upd = _version_update(service, AMD, f"g{i}")
        service.commit(upd)

    def bad():
        barrier.wait()
        # Invalid update: tagged target missing + bad blob digest.
        try:
            service.commit(BatchUpdate(
                put_blobs={"sha256:" + "1" * 64: b"wrong-bytes"},
                tag_updates={"app:broken": "sha256:" + "9" * 64},
            ))
        except Exception:
            pass

    threads = [threading.Thread(target=good, args=(i,)) for i in range(3)]
    threads.append(threading.Thread(target=bad))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = service.snapshot()
    # 3 good commits applied; broken tag never appears; bad blob never stored.
    assert snap.version == version_before + 3
    assert "app:broken" not in snap.tags
    assert "sha256:" + "1" * 64 not in snap.blobs


def test_snapshot_mappings_are_immutable_to_consumers(service):
    _, upd = _version_update(service, AMD, "v0")
    service.commit(upd)
    snap = service.snapshot()
    with pytest.raises(TypeError):
        snap.tags["hacked"] = "sha256:bad"  # type: ignore[index]
    with pytest.raises((TypeError, AttributeError)):
        snap.blobs.clear()  # type: ignore[attr-defined]
    with pytest.raises((TypeError, AttributeError)):
        snap.manifests.pop(next(iter(snap.manifests)))  # type: ignore[attr-defined]
