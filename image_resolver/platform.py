"""Deterministic platform matching, priorities and fallback ordering.

Selection rules (all decisions are reproducible):

1. An **exact** match requires equal ``os`` and ``architecture``, compatible
   ``variant`` and every requested optional field (``os_version`` / features)
   to be satisfied by the candidate.
2. When no exact match exists, candidates are tried against configurable
   fallback chains (``os_fallback`` / ``arch_fallback``).  A candidate is only
   returned through an explicitly configured fallback; arbitrary candidates are
   never returned.
3. Candidates are ranked ``(os_score, arch_score, variant_score,
   os_version_score)``; ties are broken by ``(os, architecture, variant,
   digest)`` so the same index always yields the same result.
4. Candidates without a platform, or candidates/requests that cannot be
   satisfied through any chain, cause :class:`NoPlatformMatchError`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import NoPlatformMatchError
from .models import Descriptor, Platform

# Default fallback chains. Only explicitly listed alternatives are ever used.
DEFAULT_OS_FALLBACK: dict[str, tuple[str, ...]] = {
    "linux": ("linux",),
    "windows": ("windows",),
    "darwin": ("darwin",),
}
DEFAULT_ARCH_FALLBACK: dict[str, tuple[str, ...]] = {
    "amd64": ("amd64", "386"),
    "arm64": ("arm64", "arm"),
    "arm": ("arm",),
    "386": ("386",),
    "ppc64le": ("ppc64le",),
    "s390x": ("s390x",),
}

# OCI implied variants when a descriptor omits the variant field.
_IMPLIED_VARIANT: dict[tuple[str, str | None], str | None] = {
    ("arm", None): "v1",
}


@dataclass(frozen=True)
class Selection:
    descriptor: Descriptor
    platform: Platform
    exact: bool
    reason: str


def _variant_value(architecture: str, variant: str | None) -> str | None:
    return _IMPLIED_VARIANT.get((architecture, variant), variant)


def variants_compatible(candidate: Platform, target: Platform) -> bool:
    """True when candidate and target variants denote the same CPU variant."""
    c = _variant_value(candidate.architecture, candidate.variant)
    t = _variant_value(target.architecture, target.variant)
    return c == t


def exact_match(candidate: Platform, target: Platform) -> bool:
    """Strict OCI-style subset match of *candidate* against *target*."""
    if candidate.os != target.os or candidate.architecture != target.architecture:
        return False
    if not variants_compatible(candidate, target):
        return False
    if target.os_version is not None and candidate.os_version != target.os_version:
        return False
    if not (set(target.os_features) <= set(candidate.os_features)):
        return False
    if not (set(target.features) <= set(candidate.features)):
        return False
    return True


def _chain_score(value: str | None, wanted: str, chain: tuple[str, ...] | None) -> int | None:
    """Position of *value* in the fallback chain for *wanted*; 0 == exact."""
    if value == wanted:
        return 0
    if chain is not None:
        for index, alternative in enumerate(chain, start=1):
            if value == alternative and alternative != wanted:
                return index
    return None


def fallback_rank(
    candidate: Platform,
    target: Platform,
    os_fallback: dict[str, tuple[str, ...]] | None = None,
    arch_fallback: dict[str, tuple[str, ...]] | None = None,
) -> int | None:
    """Rank how *candidate* can satisfy *target*.

    ``None`` means the candidate is not acceptable at all. Lower is better;
    ``0`` is an exact os/architecture match through a fallback chain.
    """
    # None means "use built-in defaults"; an explicitly empty mapping disables
    # that fallback dimension.
    effective_os_fallback = DEFAULT_OS_FALLBACK if os_fallback is None else os_fallback
    effective_arch_fallback = (
        DEFAULT_ARCH_FALLBACK if arch_fallback is None else arch_fallback
    )
    os_chain = effective_os_fallback.get(target.os)
    arch_chain = effective_arch_fallback.get(target.architecture)
    os_score = _chain_score(candidate.os, target.os, os_chain)
    arch_score = _chain_score(candidate.architecture, target.architecture, arch_chain)
    if os_score is None or arch_score is None:
        return None
    # Same-architecture fallback (e.g. amd64->386 differs in arch, so this is
    # only reached for chains that repeat the arch) keeps variant strictness;
    # cross-architecture fallback compares across different CPU families where
    # variants are not interchangeable, so variant is only checked when the
    # architectures agree.
    if candidate.architecture == target.architecture and not variants_compatible(
        candidate, target
    ):
        return None
    if target.os_version is not None and candidate.os_version != target.os_version:
        return None
    if not (set(target.os_features) <= set(candidate.os_features)):
        return None
    if not (set(target.features) <= set(candidate.features)):
        return None
    # Weight OS deviation less than architecture deviation.
    return os_score * 16 + arch_score


def select_descriptor(
    candidates: tuple[Descriptor, ...],
    target: Platform | None,
    os_fallback: dict[str, tuple[str, ...]] | None = None,
    arch_fallback: dict[str, tuple[str, ...]] | None = None,
    default_platform: Platform | None = None,
) -> Selection:
    """Deterministically pick one candidate for *target*.

    A missing *target* falls back to *default_platform*; if neither is given
    the call fails rather than silently choosing a candidate.
    """
    effective_target = target or default_platform
    if effective_target is None:
        raise NoPlatformMatchError(
            "no target platform supplied and no default platform configured"
        )

    # None means "use built-in defaults"; an explicitly empty mapping disables
    # that fallback dimension.
    effective_os_fallback = DEFAULT_OS_FALLBACK if os_fallback is None else os_fallback
    effective_arch_fallback = (
        DEFAULT_ARCH_FALLBACK if arch_fallback is None else arch_fallback
    )

    ranked: list[tuple[tuple[int, int, int, int, str, str, str, str], Descriptor, int | None]] = []
    for descriptor in candidates:
        if descriptor.platform is None:
            continue
        candidate = descriptor.platform
        exact = exact_match(candidate, effective_target)
        rank: int | None = 0 if exact else fallback_rank(
            candidate, effective_target, os_fallback, arch_fallback
        )
        if rank is None:
            continue
        variant = candidate.variant or ""
        # Lower is better: exact first, then fallback distance, then stable
        # content-derived tie-break.
        sort_key = (
            rank,
            0 if exact else 1,
            candidate.os,
            candidate.architecture,
            variant,
            descriptor.digest,
        )
        ranked.append((sort_key, descriptor, rank))

    if not ranked:
        raise NoPlatformMatchError(
            "no manifest candidate matches platform "
            f"{effective_target.os}/{effective_target.architecture}"
            f"{('/' + effective_target.variant) if effective_target.variant else ''} "
            "under the configured fallback chains"
        )

    ranked.sort(key=lambda item: item[0])
    _, descriptor, rank = ranked[0]
    candidate = descriptor.platform
    assert candidate is not None
    if rank == 0 and exact_match(candidate, effective_target):
        reason = "exact platform match"
        exact = True
    else:
        reason = (
            f"fallback match {candidate.os}/{candidate.architecture}"
            f"{('/' + candidate.variant) if candidate.variant else ''} "
            f"for requested {effective_target.os}/{effective_target.architecture}"
            f"{('/' + effective_target.variant) if effective_target.variant else ''}"
        )
        exact = False
    return Selection(descriptor=descriptor, platform=candidate, exact=exact, reason=reason)
