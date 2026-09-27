"""Digest parsing, verification and computation."""

from __future__ import annotations

import hashlib
import re

from .errors import DigestMismatch, DigestUnsupportedAlgorithm

SUPPORTED_ALGORITHMS: frozenset[str] = frozenset({"sha256", "sha512"})

_DIGEST_RE = re.compile(r"^([a-z0-9]+(?:[+._-][a-z0-9]+)*):([a-zA-Z0-9._=-]+)$")

# Hex lengths accepted per algorithm; empty set means "canonical hex, any length".
_HEX_LEN: dict[str, int] = {
    "sha256": 64,
    "sha512": 128,
}
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def parse_digest(
    digest: str, *, allowed: frozenset[str] = SUPPORTED_ALGORITHMS
) -> tuple[str, str]:
    """Validate a digest string; return (algorithm, encoded hex).

    Raises ``DigestUnsupportedAlgorithm`` for unknown/unsupported algorithms
    or malformed encodings.
    """
    if not isinstance(digest, str):
        raise DigestUnsupportedAlgorithm(f"digest must be a string, got {type(digest)!r}")
    match = _DIGEST_RE.match(digest)
    if match is None:
        raise DigestUnsupportedAlgorithm(f"malformed digest: {digest!r}")
    algorithm, encoded = match.group(1), match.group(2)
    if algorithm not in allowed:
        raise DigestUnsupportedAlgorithm(f"unsupported digest algorithm: {algorithm!r}")
    if not _HEX_RE.match(encoded):
        raise DigestUnsupportedAlgorithm(f"non-hex digest encoding for {algorithm!r}")
    expected_len = _HEX_LEN.get(algorithm)
    if expected_len is not None and len(encoded) != expected_len:
        raise DigestUnsupportedAlgorithm(
            f"bad digest length for {algorithm!r}: {len(encoded)} != {expected_len}"
        )
    return algorithm, encoded


def compute_digest(data: bytes, algorithm: str = "sha256") -> str:
    """Compute an ``algorithm:hex`` digest over bytes."""
    try:
        hasher = hashlib.new(algorithm)
    except (ValueError, TypeError) as exc:
        raise DigestUnsupportedAlgorithm(f"unsupported digest algorithm: {algorithm!r}") from exc
    hasher.update(data)
    return f"{algorithm}:{hasher.hexdigest()}"


def verify_digest(
    data: bytes,
    expected: str,
    *,
    allowed: frozenset[str] = SUPPORTED_ALGORITHMS,
) -> None:
    """Verify ``data`` hashes to ``expected``.

    Raises ``DigestUnsupportedAlgorithm`` for bad algorithms and
    ``DigestMismatch`` when content does not match.
    """
    algorithm, _ = parse_digest(expected, allowed=allowed)
    actual = compute_digest(data, algorithm)
    if actual != expected:
        raise DigestMismatch(expected, actual)
