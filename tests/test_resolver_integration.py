"""Integration: end-to-end resolution, cache freshness, concurrent publish/resolve."""

from __future__ import annotations

import threading

from image_resolver.digest import compute_digest
from image_resolver.errors import (
    ConcurrentUpdateError,
    NoPlatformMatchError,
    PolicyDeniedError,
)
from image_resolver.models import ImageReference, Platform
from image_resolver.policy import RuleSet
from image_resolver.resolver import Resolver
from tests.conftest import make_manifest, publish_manifest, publish_index

AMD64 = Platform("linux", "amd64")
ARM64 = Platform("linux", "arm64")


def test_single_manifest_resolution_returns_layers(resolver):
    manifest, blobs = make_manifest(b"cfg", (b"l1", b"l2"))
    digest = publish_manifest(resolver, "app", "v1", manifest, blobs)
    result = resolver.resolve(ImageReference(name="app", tag="v1"), AMD64)
    assert result.manifest_digest == digest
    assert result.source == "manifest"
    assert result.layer_digests == (compute_digest(b"l1"), compute_digest(b"l2"))


def test_multiarch_index_selects_matching_manifest(resolver):
    m_amd, b_amd = make_manifest(b"amd", (b"a",))
    m_arm, b_arm = make_manifest(b"arm", (b"b",))
    publish_index(
        resolver, "multi", "latest",
        [
            (m_amd, AMD64, b_amd),
            (m_arm, ARM64, b_arm),
        ],
    )
    r_amd = resolver.resolve(ImageReference(name="multi", tag="latest"), AMD64)
    r_arm = resolver.resolve(ImageReference(name="multi", tag="latest"), ARM64)
    assert r_amd.manifest_digest == compute_digest(m_amd)
    assert r_arm.manifest_digest == compute_digest(m_arm)
    assert r_amd.source == r_arm.source == "index"


def test_fallback_selection_through_index(resolver):
    m_armv7, blobs = make_manifest(b"arm", (b"l",))
    publish_index(
        resolver, "fb", "t",
        [(m_armv7, Platform("linux", "arm", "v7"), blobs)],
    )
    result = resolver.resolve(ImageReference(name="fb", tag="t"), ARM64)
    assert result.matched_platform.architecture == "arm"
    assert result.matched_platform.variant == "v7"


def test_no_matching_platform_raises(resolver):
    m_amd, b_amd = make_manifest(b"amd", (b"a",))
    publish_index(resolver, "m", "t", [(m_amd, AMD64, b_amd)])
    import pytest

    with pytest.raises(NoPlatformMatchError):
        resolver.resolve(ImageReference(name="m", tag="t"), Platform("linux", "s390x"))


def test_tag_rebind_never_returns_stale_cached_manifest(resolver, store):
    m1, b1 = make_manifest(b"c1", (b"l1",))
    d1 = publish_manifest(resolver, "app", "moving", m1, b1)
    first = resolver.resolve(ImageReference(name="app", tag="moving"), AMD64)
    assert first.manifest_digest == d1

    m2, b2 = make_manifest(b"c2", (b"l2",))
    d2 = publish_manifest(resolver, "app", "moving", m2, b2)
    assert d1 != d2

    second = resolver.resolve(ImageReference(name="app", tag="moving"), AMD64)
    assert second.manifest_digest == d2
    assert second.layer_digests == (compute_digest(b"l2"),)

    # The old digest remains content-addressable and unchanged.
    old = resolver.resolve(ImageReference(name="app", digest=d1), AMD64)
    assert old.manifest_digest == d1


def test_policy_change_applies_to_cached_results(store):
    from tests.conftest import make_manifest, publish_manifest

    permissive = Resolver(store, rule_set=RuleSet(default_decision="allow"))
    manifest, blobs = make_manifest(b"cfg", (b"l",))
    publish_manifest(permissive, "app", "v1", manifest, blobs)
    permissive.resolve(ImageReference(name="app", tag="v1"), AMD64)  # populate cache

    strict = Resolver(store, rule_set=RuleSet())  # default deny
    try:
        strict.resolve(ImageReference(name="app", tag="v1"), AMD64)
        raise AssertionError("expected policy denial")
    except PolicyDeniedError as exc:
        assert str(exc) == "denied by default policy"


def test_concurrent_publishes_are_serialised(resolver, store):
    errors: list[BaseException] = []

    def publish(index: int):
        try:
            manifest, blobs = make_manifest(f"c{index}".encode(), (f"l{index}".encode(),))
            resolver.publish(ImageReference(name="app", tag=f"v{index}"), manifest, blobs)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # _publish_lock serialises at resolver level -> all succeed.
    assert errors == []
    for i in range(10):
        result = resolver.resolve(ImageReference(name="app", tag=f"v{i}"), AMD64)
        assert result.layer_digests == (compute_digest(f"l{i}".encode()),)


def test_concurrent_resolve_and_publish_never_errors(resolver, store):
    manifest, blobs = make_manifest(b"c0", (b"l0",))
    publish_manifest(resolver, "app", "latest", manifest, blobs)

    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        while not stop.is_set():
            try:
                result = resolver.resolve(
                    ImageReference(name="app", tag="latest"), AMD64
                )
                assert result.layer_digests
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                return

    readers = [threading.Thread(target=reader) for _ in range(8)]
    for t in readers:
        t.start()
    for i in range(1, 40):
        m, b = make_manifest(f"c{i}".encode(), (f"l{i}".encode(),))
        resolver.publish(ImageReference(name="app", tag="latest"), m, b)
    stop.set()
    for t in readers:
        t.join(timeout=5)
    assert errors == [], errors

    final = resolver.resolve(ImageReference(name="app", tag="latest"), AMD64)
    assert final.layer_digests == (compute_digest(b"l39"),)


def test_digest_pinned_reference(resolver):
    manifest, blobs = make_manifest(b"cfg", (b"l",))
    digest = publish_manifest(resolver, "app", "v1", manifest, blobs)
    result = resolver.resolve(ImageReference(name="app", digest=digest), AMD64)
    assert result.manifest_digest == digest
