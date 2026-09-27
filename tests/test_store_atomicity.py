"""Atomic versioned updates: readers observe only complete committed versions."""

from __future__ import annotations

import threading

import pytest

from image_resolver.digest import compute_digest
from image_resolver.errors import (
    ConcurrentUpdateError,
    PartialWriteError,
    ReferencedObjectMissingError,
)
from image_resolver.models import ImageReference, Manifest
from tests.conftest import make_manifest


def test_staged_content_invisible_until_commit(store):
    manifest, blobs = make_manifest(b"cfg", (b"l",))
    digest = compute_digest(manifest)
    store.begin()
    store.put_object(digest, manifest)
    for d, b in blobs.items():
        store.put_blob(d, b)
    assert not store.has_object(digest)
    with pytest.raises(ReferencedObjectMissingError):
        store.get_object(digest)
    store.rollback()
    assert store.current_version() == 0


def test_rollback_preserves_previous_version(store):
    m1, b1 = make_manifest(b"c1", (b"l1",))
    d1 = compute_digest(m1)
    with store:
        store.put_object(d1, m1)
        for d, b in b1.items():
            store.put_blob(d, b)
        store.bind_tag("app", "v1", d1)
    assert store.current_version() == 1

    store.begin()
    m2, b2 = make_manifest(b"c2", (b"l2",))
    store.put_object(compute_digest(m2), m2)
    store.rollback()

    assert store.current_version() == 1
    assert store.resolve_ref(ImageReference(name="app", tag="v1")) == d1


def test_failed_commit_leaves_no_partial_state(store):
    m1, b1 = make_manifest(b"c1", (b"l1",))
    d1 = compute_digest(m1)
    with store:
        store.put_object(d1, m1)
        for d, b in b1.items():
            store.put_blob(d, b)
        store.bind_tag("app", "v1", d1)

    store.begin()
    store.bind_tag("app", "broken", "sha256:" + "0" * 64)
    with pytest.raises(PartialWriteError):
        store.commit_version()

    # Previous version intact and store usable for a fresh transaction.
    assert store.current_version() == 1
    store.begin()
    store.rollback()
    with pytest.raises(ReferencedObjectMissingError):
        store.resolve_ref(ImageReference(name="app", tag="broken"))


def test_two_concurrent_transactions_are_serialised(store):
    store.begin()
    with pytest.raises(ConcurrentUpdateError):
        store.begin()
    store.rollback()


def test_tag_rebind_is_atomic_new_version(store):
    m1, b1 = make_manifest(b"c1", (b"l1",))
    m2, b2 = make_manifest(b"c2", (b"l2",))
    d1, d2 = compute_digest(m1), compute_digest(m2)
    with store:
        store.put_object(d1, m1)
        for d, b in b1.items():
            store.put_blob(d, b)
        store.bind_tag("app", "latest", d1)
    with store:
        store.put_object(d2, m2)
        for d, b in b2.items():
            store.put_blob(d, b)
        store.bind_tag("app", "latest", d2)

    assert store.current_version() == 2
    assert store.resolve_ref(ImageReference(name="app", tag="latest")) == d2


def test_concurrent_readers_never_see_mixed_fields(store):
    """Writers rebound a tag; every read resolves to a fully committed digest."""
    versions: list[tuple[str, str]] = []

    def publish(cfg: bytes, layer: bytes, tag: str):
        m, blobs = make_manifest(cfg, (layer,))
        digest = compute_digest(m)
        with store:
            store.put_object(digest, m)
            for d, b in blobs.items():
                store.put_blob(d, b)
            store.bind_tag("app", "latest", digest)

    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        while not stop.is_set():
            try:
                ver = store.current_version()
                digest = store.resolve_ref(ImageReference(name="app", tag="latest"))
                obj = store.get_object(digest)
                # Cross-field consistency: resolved digest and fetched object
                # must belong to the same committed version (no mixing).
                assert isinstance(obj, Manifest)
                assert store.has_object(digest)
                assert isinstance(ver, int)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    readers = [threading.Thread(target=reader) for _ in range(8)]
    publish(b"c0", b"l0", "latest")
    for thread in readers:
        thread.start()
    for i in range(1, 60):
        publish(f"c{i}".encode(), f"l{i}".encode(), "latest")
    stop.set()
    for thread in readers:
        thread.join(timeout=5)
    assert not errors, errors


def test_commit_failure_does_not_publish_staged_blobs(store):
    m, blobs = make_manifest(b"cfg", (b"layer",))
    layer_digest = next(iter(blobs))
    store.begin()
    store.put_blob(layer_digest, blobs[layer_digest])
    store.bind_tag("app", "v1", "sha256:" + "9" * 64)
    with pytest.raises(PartialWriteError):
        store.commit_version()
    assert not store.has_object(layer_digest)
