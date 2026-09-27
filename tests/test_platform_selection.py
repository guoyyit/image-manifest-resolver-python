"""Multi-candidate platform selection, deterministic ordering and fallback."""

from __future__ import annotations

import pytest

from image_resolver.errors import NoPlatformMatchError
from image_resolver.models import Descriptor, Platform
from image_resolver.platform import select_descriptor


def d(digest: str, os_: str, arch: str, variant: str | None = None) -> Descriptor:
    return Descriptor(
        digest=digest, media_type="m", size=1,
        platform=Platform(os=os_, architecture=arch, variant=variant),
    )


AMD64 = d("sha256:aaaa", "linux", "amd64")
ARM64 = d("sha256:bbbb", "linux", "arm64")
ARMV7 = d("sha256:cccc", "linux", "arm", "v7")
WIN = d("sha256:dddd", "windows", "amd64")
CANDIDATES = (AMD64, ARM64, ARMV7, WIN)


def test_exact_platform_match():
    sel = select_descriptor(CANDIDATES, Platform("linux", "arm64"))
    assert sel.exact is True
    assert sel.descriptor is ARM64
    assert sel.reason == "exact platform match"


def test_fallback_arm64_to_arm_v7():
    sel = select_descriptor((ARMV7,), Platform("linux", "arm64"))
    assert sel.exact is False
    assert sel.descriptor is ARMV7
    assert "fallback" in sel.reason


def test_fallback_amd64_to_386():
    i386 = d("sha256:3838", "linux", "386")
    sel = select_descriptor((i386,), Platform("linux", "amd64"))
    assert sel.descriptor is i386 and sel.exact is False


def test_no_match_for_unknown_os():
    with pytest.raises(NoPlatformMatchError):
        select_descriptor(CANDIDATES, Platform("freebsd", "amd64"))


def test_no_match_when_fallback_disabled():
    with pytest.raises(NoPlatformMatchError):
        select_descriptor((ARMV7,), Platform("linux", "arm64"), os_fallback={}, arch_fallback={})


def test_missing_target_without_default_is_an_error():
    with pytest.raises(NoPlatformMatchError):
        select_descriptor(CANDIDATES, None)


def test_missing_target_uses_default_platform():
    sel = select_descriptor(CANDIDATES, None, default_platform=Platform("linux", "amd64"))
    assert sel.descriptor is AMD64


def test_selection_is_order_independent_and_reproducible():
    same_a = d("sha256:zzz", "linux", "amd64")
    same_b = d("sha256:aaa", "linux", "amd64")
    first = select_descriptor((same_a, same_b), Platform("linux", "amd64"))
    second = select_descriptor((same_b, same_a), Platform("linux", "amd64"))
    # Digest tie-break always selects the same candidate.
    assert first.descriptor is second.descriptor is same_b


def test_exact_match_preferred_over_fallback():
    sel = select_descriptor((ARMV7, ARM64), Platform("linux", "arm64"))
    assert sel.descriptor is ARM64 and sel.exact is True


def test_variant_mismatch_blocks_same_arch_match():
    with pytest.raises(NoPlatformMatchError):
        select_descriptor((ARMV7,), Platform("linux", "arm", "v8"))


def test_arm_descriptor_without_variant_implies_v1():
    bare = d("sha256:bare", "linux", "arm")
    sel = select_descriptor((bare,), Platform("linux", "arm", "v1"))
    assert sel.exact is True


def test_descriptors_without_platform_are_skipped_not_silently_chosen():
    no_platform = Descriptor(digest="sha256:none", media_type="m", size=1)
    with pytest.raises(NoPlatformMatchError):
        select_descriptor((no_platform,), Platform("linux", "amd64"))


def test_custom_fallback_chain_is_configurable():
    # Treat darwin as an allowed fallback for linux in this deployment.
    chain = {"linux": ("linux", "darwin")}
    mac = d("sha256:mac", "darwin", "arm64")
    sel = select_descriptor(
        (mac,), Platform("linux", "arm64"),
        os_fallback=chain,
    )
    assert sel.descriptor is mac and sel.exact is False
