"""Shared helpers/fixtures for the resolver test-suite."""

from __future__ import annotations

import pytest

from image_resolver.digest import compute_digest
from image_resolver.models import (
    Descriptor,
    ImageIndex,
    Manifest,
    Platform,
)
from image_resolver.resolver import Resolver
from image_resolver.store import ContentStore


def make_manifest(config: bytes = b"{}", layers: tuple[bytes, ...] = ()):
    """Build a Manifest plus the blob map that satisfies its descriptors."""
    config_digest = compute_digest(config)
    descriptors: list[Descriptor] = []
    blobs: dict[str, bytes] = {config_digest: config}
    for layer in layers:
        digest = compute_digest(layer)
        blobs[digest] = layer
        descriptors.append(
            Descriptor(digest=digest, media_type="application/vnd.oci.image.layer.v1.tar", size=len(layer))
        )
    manifest = Manifest(
        media_type="application/vnd.oci.image.manifest.v1+json",
        config=Descriptor(
            digest=config_digest,
            media_type="application/vnd.oci.image.config.v1+json",
            size=len(config),
        ),
        layers=tuple(descriptors),
    )
    return manifest, blobs


def publish_manifest(resolver: Resolver, name: str, tag: str, manifest: Manifest, blobs: dict[str, bytes]) -> str:
    from image_resolver.models import ImageReference

    return resolver.publish(ImageReference(name=name, tag=tag), manifest, blobs)


def publish_index(
    resolver: Resolver,
    name: str,
    tag: str,
    children: list[tuple[Manifest, Platform, dict[str, bytes]]],
) -> str:
    """Publish (manifest, platform, child_blobs) children under one index."""
    from image_resolver.models import ImageReference

    descriptors: list[Descriptor] = []
    objects: dict[str, Manifest] = {}
    blobs: dict[str, bytes] = {}
    for manifest, platform, child_blobs in children:
        digest = compute_digest(manifest)
        objects[digest] = manifest
        blobs.update(child_blobs)
        descriptors.append(
            Descriptor(digest=digest, media_type=manifest.media_type, size=1, platform=platform)
        )
    index = ImageIndex(
        media_type="application/vnd.oci.image.index.v1+json",
        manifests=tuple(descriptors),
    )
    return resolver.publish(ImageReference(name=name, tag=tag), index, blobs, objects=objects)


@pytest.fixture
def store() -> ContentStore:
    return ContentStore()


@pytest.fixture
def resolver(store: ContentStore) -> Resolver:
    from image_resolver.policy import RuleSet

    return Resolver(store, rule_set=RuleSet(default_decision="allow"))
