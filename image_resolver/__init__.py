"""Local image reference resolution and multi-platform selection service."""

from __future__ import annotations

from .cache import DigestCache
from .digest import compute_digest, verify_digest
from .errors import (
    ConcurrentUpdateError,
    DigestError,
    DigestMismatchError,
    MalformedDigestError,
    NoPlatformMatchError,
    PartialWriteError,
    PolicyDeniedError,
    ReferencedObjectMissingError,
    ResolverError,
    UnknownAlgorithmError,
)
from .models import (
    Descriptor,
    ImageIndex,
    ImageReference,
    Manifest,
    Platform,
    ResolvedImage,
)
from .platform import select_descriptor
from .policy import RuleSet, authorize
from .resolver import Resolver
from .store import ContentStore

__all__ = [
    "Resolver",
    "ContentStore",
    "DigestCache",
    "RuleSet",
    "authorize",
    "Platform",
    "ImageReference",
    "Descriptor",
    "Manifest",
    "ImageIndex",
    "ResolvedImage",
    "compute_digest",
    "verify_digest",
    "select_descriptor",
    "ResolverError",
    "DigestError",
    "MalformedDigestError",
    "UnknownAlgorithmError",
    "DigestMismatchError",
    "ReferencedObjectMissingError",
    "NoPlatformMatchError",
    "PolicyDeniedError",
    "ConcurrentUpdateError",
    "PartialWriteError",
]
