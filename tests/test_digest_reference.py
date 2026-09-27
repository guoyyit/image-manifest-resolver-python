"""Digest chain verification and reference parsing."""

from __future__ import annotations

import pytest

from imr.digest import compute_digest, parse_digest, verify_digest
from imr.errors import (
    DigestMismatch,
    DigestUnsupportedAlgorithm,
    ReferenceInvalid,
)
from imr.reference import parse_reference


def test_sha256_sha512_roundtrip():
    data = b"manifest-payload"
    for algo in ("sha256", "sha512"):
        d = compute_digest(data, algo)
        assert d.startswith(algo + ":")
        verify_digest(data, d)


def test_verify_detects_tampering():
    d = compute_digest(b"original")
    with pytest.raises(DigestMismatch) as exc:
        verify_digest(b"tampered", d)
    assert exc.value.expected == d
    assert exc.value.actual != d
    assert exc.value.code == "digest_mismatch"


@pytest.mark.parametrize(
    "bad",
    [
        "sha1:" + "a" * 40,              # unsupported algorithm
        "md5:" + "0" * 32,
        "sha256:zzzz",                   # non-hex
        "sha256:" + "a" * 63,            # wrong length
        "sha256" + "a" * 64,             # no separator
        "SHA256:" + "a" * 64,            # uppercase algorithm
        "",
    ],
)
def test_parse_digest_rejects_unknown_or_malformed(bad):
    with pytest.raises(DigestUnsupportedAlgorithm):
        parse_digest(bad)


def test_unknown_algorithm_in_verify_is_distinguishable():
    with pytest.raises(DigestUnsupportedAlgorithm) as exc:
        verify_digest(b"x", "frobnicate:abcd")
    assert exc.value.code == "digest_unsupported_algorithm"


@pytest.mark.parametrize(
    "text,name,tag,digest_present",
    [
        ("nginx", "nginx", "latest", False),
        ("library/nginx:1.27", "library/nginx", "1.27", False),
        ("registry.example.com:5000/team/app:v1.2.3",
         "registry.example.com:5000/team/app", "v1.2.3", False),
        ("localhost/app", "localhost/app", "latest", False),
        ("nginx@sha256:" + "a" * 64, "nginx", None, True),
        ("h.io/x/y:tag@sha256:" + "b" * 64, "h.io/x/y", "tag", True),
    ],
)
def test_parse_reference_accepts(text, name, tag, digest_present):
    ref = parse_reference(text)
    assert ref.name == name
    assert ref.tag == tag
    assert (ref.digest is not None) is digest_present
    assert str(ref)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "ng inx:latest",
        "a/-bad",
        "nginx:",
        "nginx@",
        "nginx@sha1:" + "a" * 40,
        "x/y:-tag",
    ],
)
def test_parse_reference_rejects(bad):
    with pytest.raises((ReferenceInvalid, DigestUnsupportedAlgorithm)):
        parse_reference(bad)
