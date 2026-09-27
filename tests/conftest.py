"""Shared fixtures: in-memory OCI content builders and a ready service."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from imr.digest import compute_digest
from imr.models import Platform
from imr.platform import DEFAULT_FALLBACK, FallbackStep, PlatformSelector
from imr.service import ResolverService
from imr.store import BatchUpdate

OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"


def digest(data: bytes, algo: str = "sha256") -> str:
    return compute_digest(data, algo)


@dataclass
class BuiltImage:
    manifest_digest: str
    manifest_raw: bytes
    config_digest: str
    config_raw: bytes
    layer_digests: list[str] = field(default_factory=list)
    layer_blobs: dict[str, bytes] = field(default_factory=dict)
    config_blob: dict[str, bytes] = field(default_factory=dict)
    platform: Platform | None = None


def build_image(platform: Platform | None, layer_payloads: list[bytes], *,
                corrupt: bool = False) -> BuiltImage:
    config_raw = json.dumps(
        {"os": platform.os if platform else "linux",
         "architecture": platform.architecture if platform else "amd64"}
        if platform else {}
    ).encode()
    config_digest = digest(config_raw)

    layer_blobs: dict[str, bytes] = {}
    layer_digests: list[str] = []
    for payload in layer_payloads:
        d = digest(payload)
        layer_blobs[d] = payload
        layer_digests.append(d)

    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {"mediaType": OCI_CONFIG, "digest": config_digest,
                   "size": len(config_raw)},
        "layers": [
            {"mediaType": OCI_LAYER, "digest": d, "size": len(layer_blobs[d])}
            for d in layer_digests
        ],
    }
    raw = json.dumps(manifest).encode()
    md = digest(raw)
    if corrupt:
        # Tamper with payload but keep the honest digest key mismatch test
        # handled separately; here just produce inconsistent JSON.
        broken = json.loads(raw)
        broken["layers"][0]["digest"] = "sha256:" + "f" * 64
        raw = json.dumps(broken).encode()
    return BuiltImage(
        manifest_digest=md, manifest_raw=raw,
        config_digest=config_digest, config_raw=config_raw,
        layer_digests=layer_digests, layer_blobs=layer_blobs,
        platform=platform,
    )


def build_index(entries: list[BuiltImage]) -> tuple[str, bytes]:
    doc = {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": [
            {
                "mediaType": OCI_MANIFEST,
                "digest": e.manifest_digest,
                "size": len(e.manifest_raw),
                "platform": {
                    "os": e.platform.os,
                    "architecture": e.platform.architecture,
                    **({"variant": e.platform.variant} if e.platform and e.platform.variant else {}),
                },
            }
            for e in entries
        ],
    }
    raw = json.dumps(doc).encode()
    return digest(raw), raw


def default_selector() -> PlatformSelector:
    return PlatformSelector(fallback=list(DEFAULT_FALLBACK))


@pytest.fixture
def service() -> ResolverService:
    return ResolverService(selector=default_selector())


def commit_images(service: ResolverService, images: list[BuiltImage],
                  *, tag: str | None = None,
                  index: bool = True) -> tuple[str | None, str | None]:
    blobs: dict[str, bytes] = {}
    objects = {}
    for img in images:
        blobs[img.config_digest] = img.config_raw
        blobs.update(img.layer_blobs)
        blobs[img.manifest_digest] = img.manifest_raw
        objects[img.manifest_digest] = service.build_manifest(
            img.manifest_digest, img.manifest_raw
        )
    index_digest = None
    if index and len(images) > 1:
        index_digest, index_raw = build_index(images)
        blobs[index_digest] = index_raw
        objects[index_digest] = service.build_index(index_digest, index_raw)
    elif images:
        index_digest = images[0].manifest_digest
    tags = {}
    if tag:
        tags[tag] = index_digest
    service.commit(BatchUpdate(put_blobs=blobs, put_manifests=objects,
                               tag_updates=tags))
    root = index_digest
    return (root, images[0].manifest_digest if images else None)
