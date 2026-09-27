"""Allow/deny policy semantics with stable denial reasons."""

from __future__ import annotations

import pytest

from image_resolver.errors import PolicyDeniedError
from image_resolver.models import ImageReference, Platform
from image_resolver.policy import RuleSet, authorize
from tests.conftest import make_manifest, publish_manifest

REF = ImageReference(name="lib/app", tag="v1")
AMD64 = Platform("linux", "amd64")
ARM64 = Platform("linux", "arm64")
DIGEST = "sha256:" + "a" * 64


def test_default_deny_has_stable_reason():
    with pytest.raises(PolicyDeniedError) as exc:
        authorize(RuleSet(), REF, DIGEST, AMD64)
    assert str(exc.value) == "denied by default policy"


def test_default_allow_passes():
    authorize(RuleSet(default_decision="allow"), REF, DIGEST, AMD64)


def test_platform_whitelist():
    rules = RuleSet(
        allow_platforms=frozenset({("linux", "amd64", None)}),
        default_decision="allow",
    )
    authorize(rules, REF, DIGEST, AMD64)
    with pytest.raises(PolicyDeniedError) as exc:
        authorize(rules, REF, DIGEST, ARM64)
    assert "not in allowed platform list" in str(exc.value)


def test_digest_deny_beats_allow():
    rules = RuleSet(
        allow_digests=frozenset({DIGEST}),
        deny_digests=frozenset({DIGEST}),
        default_decision="allow",
    )
    with pytest.raises(PolicyDeniedError) as exc:
        authorize(rules, REF, DIGEST, AMD64)
    assert "explicitly denied" in str(exc.value)


def test_name_glob_whitelist_and_deny():
    rules = RuleSet(
        allow_name_patterns=("docker.io/lib/*",),
        default_decision="allow",
    )
    authorize(rules, REF, DIGEST, AMD64)
    with pytest.raises(PolicyDeniedError):
        authorize(rules, ImageReference(name="other/x", tag="v1"), DIGEST, AMD64)

    rules = RuleSet(
        allow_name_patterns=("docker.io/lib/*",),
        deny_name_patterns=("docker.io/lib/app:v1",),
        default_decision="allow",
    )
    with pytest.raises(PolicyDeniedError) as exc:
        authorize(rules, REF, DIGEST, AMD64)
    assert "repository reference explicitly denied" in str(exc.value)


def test_invalid_default_decision_rejected():
    with pytest.raises(ValueError):
        RuleSet(default_decision="maybe")


def test_resolver_enforces_policy_on_every_call(resolver, store):
    manifest, blobs = make_manifest(b"cfg", (b"layer",))
    digest = publish_manifest(resolver, "app", "v1", manifest, blobs)
    other, other_blobs = make_manifest(b"cfg2", (b"layer2",))
    other_digest = publish_manifest(resolver, "app", "v2", other, other_blobs)

    from image_resolver.resolver import Resolver

    strict = Resolver(
        store,
        rule_set=RuleSet(
            deny_platforms=frozenset({("linux", "arm64", None)}),
            default_decision="allow",
        ),
    )
    strict.resolve(ImageReference(name="app", tag="v1"), AMD64)
    with pytest.raises(PolicyDeniedError) as exc:
        strict.resolve(ImageReference(name="app", tag="v1"), ARM64)
    assert "arm64" in str(exc.value)

    # Digest whitelist allows exactly the published digest.
    pinned = Resolver(
        store,
        rule_set=RuleSet(allow_digests=frozenset({digest}), default_decision="allow"),
    )
    pinned.resolve(ImageReference(name="app", digest=digest), AMD64)
    with pytest.raises(PolicyDeniedError):
        pinned.resolve(ImageReference(name="app", digest=other_digest), AMD64)
