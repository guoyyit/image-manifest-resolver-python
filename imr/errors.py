"""Stable, distinguishable error types for the resolver.

Each error carries a stable ``code`` string so callers (and tests) can
branch on failure reasons without matching human-readable messages.
"""

from __future__ import annotations


class ResolverError(Exception):
    """Base class for all resolver errors."""

    code: str = "internal_error"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message or self.code)
        if code is not None:
            self.code = code


class ReferenceInvalid(ResolverError):
    code = "reference_invalid"


class DigestUnsupportedAlgorithm(ResolverError):
    code = "digest_unsupported_algorithm"


class DigestMismatch(ResolverError):
    code = "digest_mismatch"

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"digest mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual


class BlobNotFound(ResolverError):
    code = "blob_not_found"


class ManifestInvalid(ResolverError):
    code = "manifest_invalid"


class PlatformNotFound(ResolverError):
    code = "platform_not_found"


class AmbiguousCandidate(ResolverError):
    code = "ambiguous_candidate"


class PlatformMissing(ResolverError):
    code = "platform_missing"


class PolicyDenied(ResolverError):
    code = "policy_denied"


class UpdateConflict(ResolverError):
    code = "update_conflict"
