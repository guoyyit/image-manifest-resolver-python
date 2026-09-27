"""Digest chain verification: mismatch, unknown algorithm, missing targets."""

from __future__ import annotations

import pytest

from image_resolver.digest import compute_digest, verify_digest
from image_resolver.errors import (
    DigestMismatchError,
    MalformedDigestError,
    UnknownAlgorithmError,
)
from image_resolver.models import Descriptor, ImageReference, Manifest, Platform
from tests.conftest import make_manifest, publish_manifest, publish_index


def test_compute_and_verify_roundtrip():
    digest = compute_digest({"hello": "world"})
    assert digest.startswith("sha256:")
    verify_digest({"hello": "world"}, digest)


def test_canonical_encoding_is_key_order_independent():
    assert compute_digest({"a": 1, "b": 2}) == compute_digest({"b": 2, "a": 1})


@pytest.mark.parametrize("bad", ["md5:deadbeef", "sha1:abcd", "frobnicate:zz"])
def test_unknown_algorithm_rejected(bad):
    with pytest.raises(UnknownAlgorithmError):
        verify_digest({"a": 1}, bad)


@pytest.mark.parametrize("bad", ["no-colon", "", ":", "sha256:", "sha256::", "a b:c"])
def test_malformed_digest_rejected(bad):
    with pytest.raises(MalformedDigestError):
        verify_digest({"a": 1}, bad)


def test_digest_mismatch_is_distinct_error():
    good = compute_digest({"a": 1})
    with pytest.raises(DigestMismatchError) as exc:
        verify_digest({"a": 2}, good)
    assert "expected" in str(exc.value) and "computed" in str(exc.value)


def test_publish_rejects_unknown_algorithm(resolver):
    bad = Manifest(
        media_type="m",
        config=Descriptor("md5:00000000000000000000000000000000", "c", 1),
        layers=(),
    )
    with pytest.raises(UnknownAlgorithmError):
        resolver.publish(ImageReference(name="app", tag="bad"), bad, {})


def test_publish_rejects_tampered_layer(resolver):
    manifest, blobs = make_manifest(b"cfg", (b"layer",))
    layer_digest = manifest.layers[0].digest
    blobs[layer_digest] = b"tampered-layer"
    with pytest.raises(DigestMismatchError):
        resolver.publish(ImageReference(name="app", tag="bad"), manifest, blobs)


def test_publish_rejects_missing_blob(resolver, store):
    manifest, _ = make_manifest(b"cfg", (b"layer",))
    with pytest.raises(Exception) as exc:
        resolver.publish(ImageReference(name="app", tag="bad"), manifest, {})
    # Distinct "referenced object missing" error type.
    from image_resolver.errors import ReferencedObjectMissingError

    assert isinstance(exc.value, ReferencedObjectMissingError)
    assert store.current_version() == 0  # nothing committed


def test_publish_index_rejects_child_digest_mismatch(resolver, store):
    manifest, blobs = make_manifest(b"cfg", (b"layer",))
    wrong_descriptor = Descriptor(
        digest="sha256:" + "f" * 64, media_type=manifest.media_type, size=1,
        platform=Platform("linux", "amd64"),
    )
    from image_resolver.models import ImageIndex

    index = ImageIndex(media_type="idx", manifests=(wrong_descriptor,))
    with pytest.raises(Exception) as exc:
        resolver.publish(
            ImageReference(name="app", tag="bad"),
            index,
            blobs,
            objects={compute_digest(manifest): manifest},
        )
    from image_resolver.errors import ReferencedObjectMissingError

    assert isinstance(exc.value, ReferencedObjectMissingError)
    assert store.current_version() == 0


def test_resolve_verifies_all_layers(resolver, store):
    manifest, blobs = make_manifest(b"cfg", (b"layer-a", b"layer-b"))
    digest = publish_manifest(resolver, "app", "v1", manifest, blobs)
    result = resolver.resolve(
        ImageReference(name="app", tag="v1"), Platform("linux", "amd64")
    )
    assert result.manifest_digest == digest
    assert result.layer_digests == tuple(
        compute_digest(b) for b in (b"layer-a", b"layer-b")
    )


def test_index_resolution_verifies_chosen_child(resolver):
    m_amd, b_amd = make_manifest(b"amd", (b"la",))
    m_arm, b_arm = make_manifest(b"arm", (b"lb",))
    publish_index(
        resolver,
        "multi",
        "latest",
        [
            (m_amd, Platform("linux", "amd64"), b_amd),
            (m_arm, Platform("linux", "arm64"), b_arm),
        ],
    )
    chosen = resolver.resolve(
        ImageReference(name="multi", tag="latest"), Platform("linux", "arm64")
    )
    assert chosen.manifest_digest == compute_digest(m_arm)
    assert chosen.layer_digests == (compute_digest(b"lb"),)
