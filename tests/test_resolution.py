"""End-to-end resolution and digest-chain integrity."""

from __future__ import annotations

import json

import pytest

from imr.digest import compute_digest
from imr.errors import (
    BlobNotFound,
    DigestMismatch,
    DigestUnsupportedAlgorithm,
    ManifestInvalid,
    PlatformNotFound,
)
from imr.models import Platform
from imr.store import BatchUpdate

from conftest import (
    OCI_INDEX,
    OCI_MANIFEST,
    build_image,
    build_index,
    commit_images,
)

AMD = Platform("linux", "amd64")
ARM64 = Platform("linux", "arm64", "v8")
WIN = Platform("windows", "amd64")


def _two_arch(service):
    amd = build_image(AMD, [b"amd-layer-1", b"amd-layer-2"])
    arm = build_image(ARM64, [b"arm-layer"])
    root, _ = commit_images(service, [arm, amd], tag="multi:v1")
    return amd, arm, root


def test_resolve_multiarch_exact_platform(service):
    amd, arm, _ = _two_arch(service)
    result = service.resolve("multi:v1", AMD)
    assert result.manifest_digest == amd.manifest_digest
    assert result.layer_digests == tuple(amd.layer_digests)
    assert result.matched_by == "exact"
    assert result.index_digest is not None
    assert result.platform == AMD

    result_arm = service.resolve("multi:v1", ARM64)
    assert result_arm.manifest_digest == arm.manifest_digest
    assert result_arm.layer_digests == tuple(arm.layer_digests)


def test_resolve_uses_fallback_when_no_exact_match(service):
    _two_arch(service)
    result = service.resolve("multi:v1", Platform("darwin", "arm64"))
    # default fallback order: linux/amd64 before linux/arm64
    assert result.platform == AMD
    assert result.matched_by.startswith("linux-") or "fallback" in result.matched_by or result.matched_by == "linux-amd64"
    assert result.matched_by == "linux-amd64"


def test_no_candidate_and_no_fallback_match_errors(service):
    _two_arch(service)
    from imr.platform import PlatformSelector
    service.selector.fallback = []
    with pytest.raises(PlatformNotFound):
        service.resolve("multi:v1", Platform("freebsd", "arm64"))


def test_resolve_single_manifest_by_tag(service):
    img = build_image(AMD, [b"only-layer"])
    commit_images(service, [img], tag="single:v1", index=False)
    result = service.resolve("single:v1", AMD)
    assert result.manifest_digest == img.manifest_digest
    assert result.layer_digests == tuple(img.layer_digests)
    assert result.index_digest is None
    assert result.matched_by == "direct"


def test_resolve_by_digest(service):
    amd, arm, root = _two_arch(service)
    result = service.resolve(f"multi@{root}", AMD)
    assert result.manifest_digest == amd.manifest_digest
    # direct manifest digest also works
    result2 = service.resolve(f"multi@{arm.manifest_digest}")
    assert result2.manifest_digest == arm.manifest_digest


def test_unknown_tag_errors_distinguishably(service):
    with pytest.raises(BlobNotFound) as exc:
        service.resolve("missing:latest", AMD)
    assert exc.value.code == "blob_not_found"


def test_missing_layer_blob_rejected_at_commit(service):
    img = build_image(AMD, [b"layer"])
    blobs = {
        img.config_digest: img.config_raw,
        img.manifest_digest: img.manifest_raw,
    }  # deliberately omit layer blob
    objects = {img.manifest_digest: service.build_manifest(
        img.manifest_digest, img.manifest_raw)}
    with pytest.raises(BlobNotFound):
        service.commit(BatchUpdate(
            put_blobs=blobs, put_manifests=objects,
            tag_updates={"img:v1": img.manifest_digest}))
    # nothing committed
    assert service.version == 0
    assert service.snapshot().tags == {}


def test_tampered_manifest_payload_raises_digest_mismatch(service):
    img = build_image(AMD, [b"layer"])
    tampered = img.manifest_raw + b" "
    with pytest.raises(DigestMismatch):
        service.commit(BatchUpdate(
            put_blobs={img.config_digest: img.config_raw,
                       **img.layer_blobs,
                       img.manifest_digest: tampered},
            put_manifests={img.manifest_digest: service.build_manifest(
                img.manifest_digest, tampered)}))
    assert service.version == 0


def test_layer_digest_mismatch_inside_manifest_is_rejected(service):
    # Manifest JSON references a layer digest that does not exist.
    img = build_image(AMD, [b"layer"])
    broken_doc = json.loads(img.manifest_raw)
    broken_doc["layers"][0]["digest"] = "sha256:" + "f" * 64
    broken_raw = json.dumps(broken_doc).encode()
    broken_digest = compute_digest(broken_raw)
    with pytest.raises(BlobNotFound):
        service.commit(BatchUpdate(
            put_blobs={img.config_digest: img.config_raw,
                       **img.layer_blobs,
                       broken_digest: broken_raw},
            put_manifests={broken_digest: service.build_manifest(
                broken_digest, broken_raw)},
            tag_updates={"img:v1": broken_digest}))


def test_unknown_algorithm_in_manifest_is_rejected(service):
    img = build_image(AMD, [b"layer"])
    broken_doc = json.loads(img.manifest_raw)
    broken_doc["layers"][0]["digest"] = "md5:" + "0" * 32
    broken_raw = json.dumps(broken_doc).encode()
    broken_digest = compute_digest(broken_raw)
    with pytest.raises(DigestUnsupportedAlgorithm):
        service.commit(BatchUpdate(
            put_blobs={img.config_digest: img.config_raw,
                       **img.layer_blobs,
                       broken_digest: broken_raw},
            put_manifests={broken_digest: service.build_manifest(
                broken_digest, broken_raw)}))


def test_index_referencing_missing_manifest_rejected(service):
    img = build_image(AMD, [b"layer"])
    ghost = build_image(ARM64, [b"ghost"])  # will not be uploaded
    index_digest, index_raw = build_index([img, ghost])
    with pytest.raises(BlobNotFound):
        service.commit(BatchUpdate(
            put_blobs={img.config_digest: img.config_raw,
                       **img.layer_blobs,
                       img.manifest_digest: img.manifest_raw,
                       index_digest: index_raw},
            put_manifests={
                img.manifest_digest: service.build_manifest(
                    img.manifest_digest, img.manifest_raw),
                index_digest: service.build_index(index_digest, index_raw),
            },
            tag_updates={"multi:v1": index_digest}))


def test_tampered_index_payload_raises_digest_mismatch(service):
    img = build_image(AMD, [b"layer"])
    index_digest, index_raw = build_index([img])
    with pytest.raises(DigestMismatch):
        service.commit(BatchUpdate(
            put_blobs={index_digest: index_raw + b" "},
            put_manifests={index_digest: service.build_index(
                index_digest, index_raw + b" ")},
        ))


def test_malformed_json_manifest_rejected(service):
    raw = b"{not json"
    d = compute_digest(raw)
    with pytest.raises(ManifestInvalid):
        service.build_manifest(d, raw)


def test_unsupported_media_type_rejected(service):
    doc = json.dumps({"schemaVersion": 2,
                      "mediaType": "application/binary",
                      "config": {}, "layers": []}).encode()
    d = compute_digest(doc)
    with pytest.raises(ManifestInvalid):
        service.build_manifest(d, doc)


def test_failed_update_keeps_previous_state_serviceable(service):
    img = build_image(AMD, [b"layer"])
    commit_images(service, [img], tag="img:v1", index=False)
    service.resolve("img:v1", AMD)  # works
    version_before = service.version

    # Attempt a second commit whose manifest references a layer blob that
    # is absent from both the store and the update batch.
    other = build_image(Platform("linux", "arm64"), [b"arm-layer"])
    # drop layer blobs on purpose
    with pytest.raises(BlobNotFound):
        service.commit(BatchUpdate(
            put_blobs={other.config_digest: other.config_raw,
                      other.manifest_digest: other.manifest_raw},
            put_manifests={other.manifest_digest: service.build_manifest(
                other.manifest_digest, other.manifest_raw)},
            tag_updates={"img:v2": other.manifest_digest},
        ))

    # No partial write: version unchanged, v1 still fully resolvable.
    assert service.version == version_before
    assert service.store.resolve_tag("img", "v2") is None
    result = service.resolve("img:v1", AMD)
    assert result.layer_digests == tuple(img.layer_digests)


def test_optimistic_concurrency_conflict_is_distinguishable(service):
    img = build_image(AMD, [b"layer"])
    commit_images(service, [img], tag="img:v1", index=False)
    from imr.errors import UpdateConflict
    with pytest.raises(UpdateConflict) as exc:
        service.commit(BatchUpdate(expected_version=99))
    assert exc.value.code == "update_conflict"
    assert service.version == 1


def test_resolution_is_reproducible(service):
    _two_arch(service)
    first = service.resolve("multi:v1", Platform("darwin", "arm64"))
    for _ in range(10):
        again = service.resolve("multi:v1", Platform("darwin", "arm64"))
        assert again.manifest_digest == first.manifest_digest
        assert again.matched_by == first.matched_by
