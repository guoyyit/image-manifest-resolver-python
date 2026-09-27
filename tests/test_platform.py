"""Multi-candidate selection: exact match, fallback, ambiguity, determinism."""

from __future__ import annotations

from imr.models import Descriptor, Platform
from imr.platform import FallbackStep, PlatformSelector, TieBreak
from imr.errors import AmbiguousCandidate, PlatformMissing, PlatformNotFound


def desc(name: str, platform: Platform | None) -> Descriptor:
    return Descriptor(digest=f"sha256:{name:0<64}", media_type="m", platform=platform)


LINUX_AMD = Platform("linux", "amd64")
LINUX_ARM64 = Platform("linux", "arm64", "v8")
LINUX_ARM_V7 = Platform("linux", "arm", "v7")
WIN_AMD = Platform("windows", "amd64")


def selector() -> PlatformSelector:
    return PlatformSelector(
        fallback=[
            FallbackStep("linux", "amd64", name="linux-amd64"),
            FallbackStep("linux", "arm64", name="linux-arm64"),
        ]
    )


def test_exact_match_is_preferred_over_fallback():
    ds = (desc("a", WIN_AMD), desc("b", LINUX_ARM64), desc("c", LINUX_AMD))
    chosen, rule = selector().select(ds, LINUX_AMD)
    assert chosen.digest == f"sha256:{'c':0<64}"
    assert rule == "exact"


def test_exact_match_respects_variant():
    v8 = desc("8", Platform("linux", "arm64", "v8"))
    v7 = desc("7", Platform("linux", "arm64", "v7"))
    chosen, _ = selector().select((v8, v7), Platform("linux", "arm64", "v7"))
    assert chosen is v7
    chosen, _ = selector().select((v8, v7), Platform("linux", "arm64", "v8"))
    assert chosen is v8


def test_fallback_order_is_configurable_and_stable():
    ds = (desc("a", WIN_AMD), desc("b", LINUX_ARM64), desc("c", LINUX_AMD))
    chosen, rule = selector().select(ds, Platform("darwin", "arm64"))
    assert chosen.digest.endswith("c" + "0" * 63)
    assert rule == "linux-amd64"

    arm_first = PlatformSelector(
        fallback=[FallbackStep("linux", "arm64", name="arm-first"),
                  FallbackStep("linux", "amd64", name="amd-second")]
    )
    chosen, rule = arm_first.select(ds, Platform("darwin", "arm64"))
    assert chosen.digest.endswith("b" + "0" * 63)
    assert rule == "arm-first"


def test_missing_target_uses_fallback_when_allowed():
    ds = (desc("b", LINUX_ARM64), desc("c", LINUX_AMD))
    chosen, rule = selector().select(ds, None)
    assert rule == "linux-amd64"


def test_missing_target_rejected_when_disallowed():
    sel = PlatformSelector(allow_missing_target=False)
    with pytest_raises(PlatformMissing):
        sel.select((desc("c", LINUX_AMD),), None)


def pytest_raises(exc):
    import pytest
    return pytest.raises(exc)


def test_missing_target_without_fallback_and_multiple_candidates_errors():
    sel = PlatformSelector(fallback=[])
    with pytest_raises(PlatformNotFound):
        sel.select((desc("a", LINUX_AMD), desc("b", WIN_AMD)), None)


def test_ambiguous_exact_match_raises_by_default():
    ds = (desc("a", LINUX_AMD), desc("b", LINUX_AMD))
    with pytest_raises(AmbiguousCandidate):
        selector().select(ds, LINUX_AMD)


def test_ambiguous_fallback_match_raises_by_default():
    ds = (desc("a", LINUX_AMD), desc("b", LINUX_AMD))
    with pytest_raises(AmbiguousCandidate):
        selector().select(ds, Platform("darwin", "amd64"))


def test_explicit_tie_break_is_deterministic():
    ds = (desc("b", LINUX_AMD), desc("a", LINUX_AMD))
    sel = PlatformSelector(
        fallback=[FallbackStep("linux", "amd64")], tie_break=TieBreak.DIGEST_ASC
    )
    chosen, rule = sel.select(ds, Platform("darwin", "amd64"))
    assert chosen.digest == f"sha256:{'a':0<64}"
    assert "tie_break(digest_asc)" in rule
    # Repeated selection returns the same result (reproducible).
    again, _ = sel.select(tuple(reversed(ds)), Platform("darwin", "amd64"))
    assert again.digest == chosen.digest


def test_no_match_raises_platform_not_found():
    ds = (desc("w", WIN_AMD),)
    with pytest_raises(PlatformNotFound):
        selector().select(ds, Platform("linux", "arm64", "v7"))


def test_variant_fallback_semantics():
    no_variant = desc("n", Platform("linux", "arm64"))
    v8 = desc("v", Platform("linux", "arm64", "v8"))
    require_absent = PlatformSelector(
        fallback=[FallbackStep("linux", "arm64", variant=None, name="no-variant")]
    )
    chosen, _ = require_absent.select((no_variant, v8), Platform("linux", "riscv64"))
    assert chosen is no_variant

    require_v8 = PlatformSelector(
        fallback=[FallbackStep("linux", "arm64", variant="v8", name="v8")]
    )
    chosen, _ = require_v8.select((no_variant, v8), Platform("linux", "riscv64"))
    assert chosen is v8


def test_entries_without_platform_are_not_candidates():
    no_platform = Descriptor(digest="sha256:" + "z" * 64, media_type="m")
    with pytest_raises(PlatformNotFound):
        selector().select((no_platform,), Platform("linux", "amd64"))
