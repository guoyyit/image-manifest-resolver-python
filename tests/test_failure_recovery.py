"""Mid-update failures roll back; service keeps serving the last good state."""

from __future__ import annotations

import pytest

from image_resolver.digest import compute_digest
from image_resolver.errors import (
    DigestMismatchError,
    ReferencedObjectMissingError,
    UnknownAlgorithmError,
)
from image_resolver.models import ImageReference, Platform
from tests.conftest import make_manifest, publish_manifest, publish_index


def _resolve(resolver):
    return resolver.resolve(
        ImageReference(name="app", tag="v1"), Platform("linux", "amd64")
    )


def test_service_still_serves_after_bad_publishes(resolver, store):
    manifest, blobs = make_manifest(b"good-cfg", (b"good-layer",))
    good_digest = publish_manifest(resolver, "app", "v1", manifest, blobs)
    assert _resolve(resolver).manifest_digest == good_digest
    good_version = store.current_version()

    # 1. Unknown algorithm.
    bad_alg = make_manifest(b"x")[0]
    object.__setattr__(
        bad_alg, "config",
        type(bad_alg.config)(digest="md5:abcd", media_type="c", size=1),
    )
    with pytest.raises(UnknownAlgorithmError):
        resolver.publish(ImageReference(name="app", tag="v1"), bad_alg, {})

    # 2. Tampered blob.
    tampered, tampered_blobs = make_manifest(b"y", (b"z",))
    tampered_blobs[tampered.layers[0].digest] = b"not-z"
    with pytest.raises(DigestMismatchError):
        resolver.publish(ImageReference(name="app", tag="v1"), tampered, tampered_blobs)

    # 3. Missing blob target.
    missing, _ = make_manifest(b"w", (b"q",))
    with pytest.raises(ReferencedObjectMissingError):
        resolver.publish(ImageReference(name="app", tag="v1"), missing, {})

    assert store.current_version() == good_version
    assert _resolve(resolver).manifest_digest == good_digest


def test_failed_index_publish_keeps_old_index(resolver, store):
    m_amd, b_amd = make_manifest(b"amd", (b"la",))
    m_arm, b_arm = make_manifest(b"arm", (b"lb",))
    publish_index(
        resolver, "multi", "latest",
        [
            (m_amd, Platform("linux", "amd64"), b_amd),
            (m_arm, Platform("linux", "arm64"), b_arm),
        ],
    )
    version = store.current_version()
    arm_result = resolver.resolve(
        ImageReference(name="multi", tag="latest"), Platform("linux", "arm64")
    )
    assert arm_result.manifest_digest == compute_digest(m_arm)

    # Publish a broken replacement index referencing a missing child.
    from image_resolver.models import Descriptor, ImageIndex

    broken = ImageIndex(
        media_type="idx",
        manifests=(
            Descriptor(
                digest="sha256:" + "0" * 64,
                media_type="m",
                size=1,
                platform=Platform("linux", "amd64"),
            ),
        ),
    )
    with pytest.raises(ReferencedObjectMissingError):
        resolver.publish(ImageReference(name="multi", tag="latest"), broken, {})

    assert store.current_version() == version
    still = resolver.resolve(
        ImageReference(name="multi", tag="latest"), Platform("linux", "arm64")
    )
    assert still.manifest_digest == compute_digest(m_arm)


def test_consecutive_publishes_succeed_after_failure(resolver, store):
    m1, b1 = make_manifest(b"1", (b"l1",))
    publish_manifest(resolver, "app", "v1", m1, b1)
    bad, _ = make_manifest(b"bad")
    object.__setattr__(
        bad, "config",
        type(bad.config)(digest="sha256:" + "1" * 64, media_type="c", size=1),
    )
    with pytest.raises(Exception):
        resolver.publish(ImageReference(name="app", tag="vbad"), bad, {})

    m2, b2 = make_manifest(b"2", (b"l2",))
    d2 = publish_manifest(resolver, "app", "v2", m2, b2)
    assert resolver.resolve(
        ImageReference(name="app", tag="v2"), Platform("linux", "amd64")
    ).manifest_digest == d2
