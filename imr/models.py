"""Core data models: references, platforms, manifests, indexes and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

DEFAULT_TAG = "latest"

INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)


class DescriptorKind(str, Enum):
    INDEX = "index"
    MANIFEST = "manifest"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ImageReference:
    """A parsed image reference.

    A digest reference keeps ``tag`` None; otherwise tag defaults to latest.
    """

    name: str
    tag: str | None = None
    digest: str | None = None

    def __str__(self) -> str:
        if self.digest is not None:
            base = self.name
            if self.tag is not None:
                base = f"{base}:{self.tag}"
            return f"{base}@{self.digest}"
        return f"{self.name}:{self.tag or DEFAULT_TAG}"

    @property
    def tag_key(self) -> str:
        return f"{self.name}:{self.tag or DEFAULT_TAG}"


@dataclass(frozen=True)
class Platform:
    os: str
    architecture: str
    variant: str | None = None
    os_version: str | None = None
    os_features: tuple[str, ...] = ()

    def matches(self, other: "Platform") -> bool:
        """Exact OCI platform match: os, arch, variant, os_version, features.

        Missing optional fields on either side are only equal when both are
        absent. ``os_features`` match as sets (order-independent).
        """
        if self.os != other.os or self.architecture != other.architecture:
            return False
        if (self.variant or None) != (other.variant or None):
            return False
        if (self.os_version or None) != (other.os_version or None):
            return False
        if set(self.os_features or ()) != set(other.os_features or ()):
            return False
        return True


@dataclass(frozen=True)
class Descriptor:
    digest: str
    media_type: str = ""
    size: int = -1
    platform: Platform | None = None
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Manifest:
    digest: str
    media_type: str
    raw: bytes
    config_digest: str | None
    layers: tuple[Descriptor, ...]

    @property
    def layer_digests(self) -> tuple[str, ...]:
        return tuple(layer.digest for layer in self.layers)


@dataclass(frozen=True)
class ImageIndex:
    digest: str
    media_type: str
    raw: bytes
    manifests: tuple[Descriptor, ...]


@dataclass(frozen=True)
class ResolveResult:
    """Output of a successful resolution.

    ``manifest`` is the final single-arch manifest selected for the target
    platform; ``layer_digests`` are its layers in declared order.
    """

    reference: ImageReference
    manifest_digest: str
    manifest: Manifest
    layer_digests: tuple[str, ...]
    platform: Platform | None
    matched_by: str
    index_digest: str | None = None
