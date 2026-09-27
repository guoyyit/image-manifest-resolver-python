"""Allow/deny policy rules by platform, digest or tag.

Semantics:
- Rules are evaluated *in configuration order*; the first matching rule
  decides (explicit allow or explicit deny).
- If no rule matches, ``default`` decides (default: allow).
- Patterns use ``fnmatch`` glob syntax (``*`` / ``?`` / ``[seq]``).
- Denials raise ``PolicyDenied`` with a stable reason:
  ``deny:<field>:<pattern>`` for an explicit rule, or ``deny:default``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum

from .errors import PolicyDenied
from .models import DEFAULT_TAG, ImageReference, Platform


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class PolicyField(str, Enum):
    TAG = "tag"
    DIGEST = "digest"
    NAME = "name"
    PLATFORM = "platform"  # "os/arch[/variant]"
    PLATFORM_OS = "platform_os"
    PLATFORM_ARCH = "platform_arch"


class DefaultDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyRule:
    field: PolicyField
    pattern: str
    effect: PolicyEffect

    @property
    def reason(self) -> str:
        return f"{self.effect.value}:{self.field.value}:{self.pattern}"


@dataclass
class Policy:
    rules: list[PolicyRule] = field(default_factory=list)
    default: DefaultDecision = DefaultDecision.ALLOW

    def deny(self, field_name: str, pattern: str) -> "Policy":
        self.rules.append(
            PolicyRule(PolicyField(field_name), pattern, PolicyEffect.DENY)
        )
        return self

    def allow(self, field_name: str, pattern: str) -> "Policy":
        self.rules.append(
            PolicyRule(PolicyField(field_name), pattern, PolicyEffect.ALLOW)
        )
        return self

    def evaluate(
        self,
        reference: ImageReference,
        platform: Platform | None,
    ) -> None:
        """Raise ``PolicyDenied`` with a stable reason when forbidden."""
        values = self._field_values(reference, platform)
        for rule in self.rules:
            value = values.get(rule.field)
            if value is not None and fnmatch.fnmatchcase(value, rule.pattern):
                if rule.effect is PolicyEffect.DENY:
                    raise PolicyDenied(
                        f"resolution denied by rule {rule.reason} "
                        f"(reference={reference}, platform={value if rule.field.value.startswith('platform') else '-'})"
                    )
                return  # explicit allow short-circuits
        if self.default is DefaultDecision.DENY:
            raise PolicyDenied(
                f"resolution denied: no allow rule matched (deny:default) "
                f"for reference={reference}"
            )

    @staticmethod
    def _field_values(
        reference: ImageReference, platform: Platform | None
    ) -> dict[PolicyField, str | None]:
        tag = reference.tag or DEFAULT_TAG
        values: dict[PolicyField, str | None] = {
            PolicyField.TAG: tag,
            PolicyField.DIGEST: reference.digest,
            PolicyField.NAME: reference.name,
            PolicyField.PLATFORM: None,
            PolicyField.PLATFORM_OS: None,
            PolicyField.PLATFORM_ARCH: None,
        }
        if platform is not None:
            suffix = f"/{platform.variant}" if platform.variant else ""
            values[PolicyField.PLATFORM] = f"{platform.os}/{platform.architecture}{suffix}"
            values[PolicyField.PLATFORM_OS] = platform.os
            values[PolicyField.PLATFORM_ARCH] = platform.architecture
        return values
