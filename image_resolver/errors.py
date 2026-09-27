"""Distinguishable error types for the resolver service."""

from __future__ import annotations


class ResolverError(Exception):
    """Base class for all resolver errors."""


class DigestError(ResolverError):
    """Base class for digest chain verification failures."""


class MalformedDigestError(DigestError):
    """A digest string is not of the form 'algorithm:hex'."""


class UnknownAlgorithmError(DigestError):
    """A digest uses an unsupported/unknown algorithm."""


class DigestMismatchError(DigestError):
    """Content digest does not match the referenced digest."""


class ReferencedObjectMissingError(ResolverError):
    """A referenced manifest or blob is missing from the store."""


class NoPlatformMatchError(ResolverError):
    """No index candidate is acceptable for the target platform."""


class PolicyDeniedError(ResolverError):
    """Resolution denied by allow/deny policy; reason is stable."""


class ConcurrentUpdateError(ResolverError):
    """Atomic update conflict detected."""


class PartialWriteError(ResolverError):
    """An update failed before commit; previous state must remain live."""
