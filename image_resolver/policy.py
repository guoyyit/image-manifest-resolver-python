"""Allow/deny policy rules by platform, digest, or repository name/tag.

Semantics (evaluated in a fixed order so denial reasons are stable):

1. Every *deny* rule wins over every *allow* rule.
2. Digest rules are evaluated first, then platform rules, then name/tag
   patterns, then the policy default.
3. Empty allow lists impose no restriction; non-empty allow lists form a
   whitelist.
4. If nothing permits the request and ``default_decision`` is ``"deny"`` (the
   default) the request is rejected with reason ``"denied by default policy"``.

Patterns use :func:`fnmatch.fnmatchcase` shell-style globbing and are matched
against ``registry/name`` and ``registry/name:tag`` / ``registry/name@digest``.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from .errors import PolicyDeniedError
from .models import ImageReference, Platform


@dataclass(frozen=True)
class RuleSet:
    allow_platforms: frozenset[tuple[str, str, str | None]] = frozenset()
    deny_platforms: frozenset[tuple[str, str, str | None]] = frozenset()
    allow_digests: frozenset[str] = frozenset()
    deny_digests: frozenset[str] = frozenset()
    allow_name_patterns: tuple[str, ...] = ()
    deny_name_patterns: tuple[str, ...] = ()
    default_decision: str = "deny"  # "allow" | "deny"

    def __post_init__(self) -> None:
        if self.default_decision not in ("allow", "deny"):
            raise ValueError(
                f"invalid default_decision {self.default_decision!r}; "
                "expected 'allow' or 'deny'"
            )


def _platform_key(platform: Platform) -> tuple[str, str, str | None]:
    return (platform.os, platform.architecture, platform.variant)


def _name_subjects(reference: ImageReference) -> tuple[str, ...]:
    subjects = [reference.name, reference.canonical_name]
    if reference.tag is not None:
        subjects.append(f"{reference.name}:{reference.tag}")
        subjects.append(f"{reference.canonical_name}:{reference.tag}")
    if reference.digest is not None:
        subjects.append(f"{reference.name}@{reference.digest}")
        subjects.append(f"{reference.canonical_name}@{reference.digest}")
    return tuple(subjects)


def _matches_any(patterns: tuple[str, ...], subjects: tuple[str, ...]) -> bool:
    return any(
        fnmatchcase(subject, pattern)
        for pattern in patterns
        for subject in subjects
    )


def authorize(
    rule_set: RuleSet,
    reference: ImageReference,
    manifest_digest: str,
    platform: Platform,
) -> None:
    """Raise :class:`PolicyDeniedError` with a stable reason if not permitted."""
    # 1. Deny rules always win.
    if manifest_digest in rule_set.deny_digests:
        raise PolicyDeniedError(f"denied: digest {manifest_digest} explicitly denied")
    platform_key = _platform_key(platform)
    if platform_key in rule_set.deny_platforms:
        raise PolicyDeniedError(
            f"denied: platform {platform.os}/{platform.architecture}"
            f"{('/' + platform.variant) if platform.variant else ''} explicitly denied"
        )
    subjects = _name_subjects(reference)
    if _matches_any(rule_set.deny_name_patterns, subjects):
        raise PolicyDeniedError("denied: repository reference explicitly denied")

    # 2. Non-empty allow lists act as whitelists.
    if rule_set.allow_digests and manifest_digest not in rule_set.allow_digests:
        raise PolicyDeniedError(
            f"denied: digest {manifest_digest} not in allowed digest list"
        )
    if rule_set.allow_platforms and platform_key not in rule_set.allow_platforms:
        raise PolicyDeniedError(
            f"denied: platform {platform.os}/{platform.architecture}"
            f"{('/' + platform.variant) if platform.variant else ''} "
            "not in allowed platform list"
        )
    if rule_set.allow_name_patterns and not _matches_any(
        rule_set.allow_name_patterns, subjects
    ):
        raise PolicyDeniedError("denied: repository reference not in allowed list")

    # 3. Default decision.
    if rule_set.default_decision == "deny":
        raise PolicyDeniedError("denied by default policy")
