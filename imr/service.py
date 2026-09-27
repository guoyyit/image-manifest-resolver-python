"""Facade wiring store + resolver + policy + cache into one service."""

from __future__ import annotations

from .cache import ResolutionCache
from .models import Platform, ResolveResult
from .platform import PlatformSelector
from .policy import Policy
from .resolver import Resolver, parse_index, parse_manifest
from .store import BatchUpdate, ContentStore, Snapshot


class ResolverService:
    """Public service API: atomic updates and reproducible resolution."""

    def __init__(
        self,
        selector: PlatformSelector | None = None,
        policy: Policy | None = None,
    ) -> None:
        self.store = ContentStore()
        self._cache: ResolutionCache[ResolveResult] = ResolutionCache()
        self.selector = selector or PlatformSelector()
        self.policy = policy or Policy()
        self._resolver = Resolver(
            self.store, self.selector, self.policy, self._cache
        )

    @property
    def version(self) -> int:
        return self.store.snapshot().version

    def commit(self, update: BatchUpdate) -> Snapshot:
        """Atomically apply an update; invalidate caches only on success.

        A failed commit raises before any state swap and leaves both the
        store and the cache serving the last complete version.
        """
        snapshot = self.store.commit(update)
        self._cache.clear()
        return snapshot

    def resolve(self, reference: str, target: Platform | None = None) -> ResolveResult:
        return self._resolver.resolve(reference, target)

    def snapshot(self) -> Snapshot:
        return self.store.snapshot()

    # ----- convenience helpers --------------------------------------------

    @staticmethod
    def build_manifest(digest: str, raw: bytes, media_type: str = ""):
        return parse_manifest(digest, raw, media_type)

    @staticmethod
    def build_index(digest: str, raw: bytes, media_type: str = ""):
        return parse_index(digest, raw, media_type)
