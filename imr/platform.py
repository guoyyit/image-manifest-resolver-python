"""Deterministic platform matching with configurable fallback order."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .errors import AmbiguousCandidate, PlatformMissing, PlatformNotFound
from .models import Descriptor, Platform


class TieBreak(str, Enum):
    """How to resolve multiple candidates surviving a selection step."""

    ERROR = "error"  # never silently pick one (default)
    DIGEST_ASC = "digest_asc"  # explicit deterministic tie-break


@dataclass(frozen=True)
class FallbackStep:
    """A single fallback rule matched against descriptor platforms.

    - os/architecture use "*" as wildcard.
    - variant: "*" matches any (including absent); None requires the
      candidate to declare no variant; a string requires equality.
    """

    os: str = "*"
    architecture: str = "*"
    variant: str | None = "*"
    name: str = ""


@dataclass
class PlatformSelector:
    """Selects one manifest descriptor from an index, deterministically.

    Selection order when a target platform is supplied:
      1. exact OCI platform match;
      2. configured ``fallback`` steps, in order.
    When the target is missing, only steps 2 runs (unless missing targets
    are forbidden via ``allow_missing_target=False``).

    A step yielding multiple survivors is ambiguous: the default policy
    raises instead of silently returning an arbitrary candidate. Configure
    ``tie_break`` for an explicit deterministic choice.
    """

    fallback: list[FallbackStep] = field(default_factory=list)
    tie_break: TieBreak = TieBreak.ERROR
    allow_missing_target: bool = True

    def select(
        self, descriptors: tuple[Descriptor, ...], target: Platform | None
    ) -> tuple[Descriptor, str]:
        """Return ``(descriptor, rule_name)`` or raise."""
        if target is None and not self.allow_missing_target:
            raise PlatformMissing("target platform is required but was not provided")

        candidates = tuple(d for d in descriptors if d.platform is not None)

        if target is not None:
            exact = tuple(d for d in candidates if d.platform.matches(target))
            if exact:
                return self._choose(exact, "exact")

        for index, step in enumerate(self.fallback):
            survivors = tuple(d for d in candidates if self._step_matches(step, d.platform))
            if survivors:
                rule = step.name or f"fallback[{index}]:{self._step_label(step)}"
                return self._choose(survivors, rule)

        # Deterministic last resort: caller expressed no platform and the
        # index contains exactly one entry. When a concrete target was given,
        # a miss is always an error (never a silent arbitrary choice).
        if target is None and len(candidates) == 1:
            return self._choose(candidates, "sole_candidate")

        wanted = self._describe(target)
        raise PlatformNotFound(
            f"no manifest candidate for platform {wanted} "
            f"(tried exact match and {len(self.fallback)} fallback step(s))"
        )

    @staticmethod
    def _step_matches(step: FallbackStep, platform: Platform) -> bool:
        if step.os != "*" and step.os != platform.os:
            return False
        if step.architecture != "*" and step.architecture != platform.architecture:
            return False
        if step.variant == "*":
            return True
        if step.variant is None:
            return platform.variant is None
        return platform.variant == step.variant

    @staticmethod
    def _step_label(step: FallbackStep) -> str:
        variant = "*" if step.variant == "*" else ("<none>" if step.variant is None else step.variant)
        return f"{step.os}/{step.architecture}/{variant}"

    def _choose(
        self, candidates: tuple[Descriptor, ...], rule: str
    ) -> tuple[Descriptor, str]:
        if len(candidates) == 1:
            return candidates[0], rule
        if self.tie_break is TieBreak.DIGEST_ASC:
            chosen = min(candidates, key=lambda d: d.digest)
            return chosen, f"{rule}+tie_break(digest_asc)"
        digests = ", ".join(sorted(d.digest for d in candidates))
        raise AmbiguousCandidate(
            f"rule {rule!r} matched {len(candidates)} candidates [{digests}]; "
            "configure tie_break to resolve deterministically"
        )

    @staticmethod
    def _describe(target: Platform | None) -> str:
        if target is None:
            return "<missing>"
        variant = f"/{target.variant}" if target.variant else ""
        return f"{target.os}/{target.architecture}{variant}"


DEFAULT_FALLBACK: tuple[FallbackStep, ...] = (
    FallbackStep(os="linux", architecture="amd64", name="linux-amd64"),
    FallbackStep(os="linux", architecture="arm64", name="linux-arm64"),
)
