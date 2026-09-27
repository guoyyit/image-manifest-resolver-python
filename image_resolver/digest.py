"""Digest computation and verification of the manifest/index/layer chain.

Only algorithms listed in :data:`SUPPORTED_ALGORITHMS` are accepted.  Content is
serialised with a stable canonical JSON encoding so that digest computation is
reproducible across processes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

from .errors import (
    DigestMismatchError,
    MalformedDigestError,
    UnknownAlgorithmError,
)

SUPPORTED_ALGORITHMS = frozenset({"sha256", "sha384", "sha512"})

_DIGEST_FORMAT_ERROR = (
    "malformed digest %r: expected 'algorithm:hex_encoded' with non-empty parts"
)


def parse_digest(ref: str) -> tuple[str, str]:
    """Split ``algorithm:hex`` into ``(algorithm, hex)``.

    Raises :class:`MalformedDigestError` for wrong shape and
    :class:`UnknownAlgorithmError` for unsupported algorithms.
    """
    if not isinstance(ref, str) or ref.count(":") != 1:
        raise MalformedDigestError(_DIGEST_FORMAT_ERROR % (ref,))
    algorithm, encoded = ref.split(":", 1)
    if not algorithm or not encoded:
        raise MalformedDigestError(_DIGEST_FORMAT_ERROR % (ref,))
    # OCI digest algorithm component grammar.
    if not all(c.isalnum() or c in "_+.-" for c in algorithm):
        raise MalformedDigestError(_DIGEST_FORMAT_ERROR % (ref,))
    ensure_supported_algorithm(algorithm)
    return algorithm, encoded


def ensure_supported_algorithm(algorithm: str) -> None:
    """Raise :class:`UnknownAlgorithmError` if *algorithm* is not supported."""
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise UnknownAlgorithmError(f"unsupported digest algorithm: {algorithm!r}")


def _to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    raise TypeError(f"cannot canonically serialise value of type {type(value)!r}")


def canonical_bytes(content: object) -> bytes:
    """Stable canonical JSON encoding of a content object."""
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    return json.dumps(
        _to_jsonable(content),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_digest(content: object, algorithm: str = "sha256") -> str:
    """Return ``algorithm:hex`` over the canonical content bytes."""
    ensure_supported_algorithm(algorithm)
    hasher = hashlib.new(algorithm)
    hasher.update(canonical_bytes(content))
    return f"{algorithm}:{hasher.hexdigest()}"


def verify_digest(content: object, expected: str) -> None:
    """Verify *content* against the *expected* digest.

    Raises :class:`UnknownAlgorithmError`, :class:`MalformedDigestError` or
    :class:`DigestMismatchError`; on success returns ``None``.
    """
    algorithm, _encoded = parse_digest(expected)
    actual = compute_digest(content, algorithm)
    if not hmac.compare_digest(actual, expected):
        raise DigestMismatchError(
            f"digest mismatch: expected {expected}, computed {actual}"
        )
