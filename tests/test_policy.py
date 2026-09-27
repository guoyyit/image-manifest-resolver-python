"""Allow/deny policy semantics for resolution."""

from __future__ import annotations

import pytest

from imr.errors import PolicyDenied
from imr.models import Platform
from imr.policy import DefaultDecision, Policy
from imr.policy import PolicyEffect, PolicyField, PolicyRule

from conftest import build_image, commit_images

AMD = Platform("linux", "amd64")
WIN = Platform("windows", "amd64")


def test_deny_by_tag_pattern_blocks_resolution(service):
    service.policy.deny("tag", "*-secret")
    img = build_image(AMD, [b"l"])
    commit_images(service, [img], tag="app:v1-secret", index=False)
    with pytest.raises(PolicyDenied) as exc:
        service.resolve("app:v1-secret", AMD)
    assert exc.value.code == "policy_denied"
    assert "deny:tag:*-secret" in str(exc.value)  # stable reason


def test_deny_by_platform_blocks_only_matching(service):
    service.policy.deny("platform_os", "windows")
    img_a = build_image(AMD, [b"a"])
    img_w = build_image(WIN, [b"w"])
    commit_images(service, [img_a, img_w], tag="app:v1")

    # exact windows target -> denied
    with pytest.raises(PolicyDenied):
        service.resolve("app:v1", WIN)
    # linux target resolves fine; fallback to linux stays allowed
    result = service.resolve("app:v1", AMD)
    assert result.manifest_digest == img_a.manifest_digest


def test_deny_by_digest(service):
    img = build_image(AMD, [b"a"])
    commit_images(service, [img], tag="app:v1", index=False)
    service.policy.deny("digest", img.manifest_digest)
    with pytest.raises(PolicyDenied) as exc:
        service.resolve(f"app@{img.manifest_digest}", AMD)
    reason = f"deny:digest:{img.manifest_digest}"
    assert reason in str(exc.value)


def test_first_matching_rule_wins_allow_over_later_deny(service):
    img = build_image(AMD, [b"a"])
    commit_images(service, [img], tag="app:v1", index=False)
    service.policy.allow("tag", "v1").deny("name", "*")
    service.resolve("app:v1", AMD)  # explicit allow short-circuits


def test_default_deny_blocks_unmatched(service):
    service.policy.default = DefaultDecision.DENY
    img = build_image(AMD, [b"a"])
    commit_images(service, [img], tag="app:v1", index=False)
    with pytest.raises(PolicyDenied) as exc:
        service.resolve("app:v1", AMD)
    assert "deny:default" in str(exc.value)

    # adding an explicit allow re-enables
    service.policy.allow("name", "app")
    service.resolve("app:v1", AMD)


def test_denial_is_stable_across_calls(service):
    service.policy.deny("tag", "locked")
    img = build_image(AMD, [b"a"])
    commit_images(service, [img], tag="app:locked", index=False)
    messages = set()
    for _ in range(5):
        with pytest.raises(PolicyDenied) as exc:
            service.resolve("app:locked", AMD)
        messages.add(str(exc.value))
    assert len(messages) == 1


def test_deny_by_name_glob(service):
    service.policy.rules.append(
        PolicyRule(PolicyField.NAME, "registry-*", PolicyEffect.DENY)
    )
    img = build_image(AMD, [b"a"])
    commit_images(service, [img], tag="registry-evil/app:v1", index=False)
    with pytest.raises(PolicyDenied):
        service.resolve("registry-evil/app:v1", AMD)


def test_policy_change_takes_effect_on_warm_cache(service):
    img = build_image(AMD, [b"a"])
    commit_images(service, [img], tag="app:v1", index=False)
    # warm the cache
    service.resolve("app:v1", AMD)
    # deny after caching -> must be enforced immediately
    service.policy.deny("tag", "v1")
    with pytest.raises(PolicyDenied):
        service.resolve("app:v1", AMD)
    # resetting the policy re-opens access
    service.policy.rules.clear()
    result = service.resolve("app:v1", AMD)
    assert result.manifest_digest == img.manifest_digest
