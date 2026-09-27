"""Core data models: references, platforms, manifests, indexes, resolution results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MediaType(str, Enum):
    SINGLE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
    IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"


@dataclass(frozen=True)
class Platform:
    os: str
    architecture: str
    variant: str | None = None
    os_version: str | None = None
    os_features: tuple[str, ...] = ()
    features: tuple[str, ...] = ()


@dataclass(frozen=True)
class ImageReference:
    """An image reference: registry/name with a tag or digest."""

    name: str
    tag: str | None = None
    digest: str | None = None
    registry: str = "docker.io"

    @property
    def canonical_name(self) -> str:
        """Fully-qualified, normalised repository name."""
        return f"{self.registry}/{self.name}"

    @property
    def is_digest_pinned(self) -> bool:
        return self.digest is not None


@dataclass(frozen=True)
class Descriptor:
    digest: str
    media_type: str
    size: int
    platform: Platform | None = None


@dataclass(frozen=True)
class Manifest:
    """Single image manifest: config descriptor + layer descriptors."""

    media_type: str
    config: Descriptor
    layers: tuple[Descriptor, ...]
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ImageIndex:
    """Multi-architecture index containing candidate manifests."""

    media_type: str
    manifests: tuple[Descriptor, ...]
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedImage:
    """Final resolution: the chosen manifest and its layer digests."""

    reference: ImageReference
    manifest_digest: str
    manifest: Manifest
    layer_digests: tuple[str, ...]
    matched_platform: Platform
    source: str  # "manifest" | "index"
    selection_reason: str


ContentObject = Manifest | ImageIndex
