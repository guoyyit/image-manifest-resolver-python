"""Resolution orchestration.

Pipeline for :meth:`Resolver.resolve`:

1. Resolve the reference (tag or digest) to a root digest via the committed
   store version.
2. Load the root object: a manifest is used directly; an index goes through
   deterministic :mod:`platform` selection and its chosen child manifest is
   fetched.
3. Verify the complete digest chain (index descriptor -> child manifest,
   manifest -> config/layer blobs).  Digest mismatch, unknown algorithm and
   missing targets raise distinct errors and abort resolution.
4. Enforce the :class:`~image_resolver.policy.RuleSet` (on every call, so a
   policy change takes effect even for cached results).
5. Cache the immutable verification result by ``(digest, platform)``.

Publishing (:meth:`Resolver.publish`) stages objects plus blobs, verifies the
chain inside one transaction, then commits atomically and invalidates the
cache.  Any failure rolls the transaction back and leaves the previous
complete version serving.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from .cache import DigestCache
from .digest import parse_digest, verify_digest
from .errors import (
    DigestMismatchError,
    PartialWriteError,
    ReferencedObjectMissingError,
    ResolverError,
)
from .models import (
    Descriptor,
    ImageIndex,
    ImageReference,
    Manifest,
    Platform,
    ResolvedImage,
)
from .platform import select_descriptor
from .policy import RuleSet, authorize
from .store import ContentStore

_ALLOW_ALL = RuleSet(default_decision="allow")


class Resolver:
    def __init__(
        self,
        store: ContentStore,
        rule_set: RuleSet | None = None,
        cache: DigestCache | None = None,
        os_fallback: dict[str, tuple[str, ...]] | None = None,
        arch_fallback: dict[str, tuple[str, ...]] | None = None,
        default_platform: Platform | None = None,
    ) -> None:
        self._store = store
        self._rule_set = rule_set if rule_set is not None else _ALLOW_ALL
        self._cache = cache if cache is not None else DigestCache()
        self._os_fallback = os_fallback
        self._arch_fallback = arch_fallback
        self._default_platform = default_platform
        # Serialises publish transactions (store itself allows one tx at a
        # time); readers never block on it.
        self._publish_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    @staticmethod
    def _platform_signature(platform: Platform | None) -> str:
        if platform is None:
            return "default"
        return f"{platform.os}/{platform.architecture}/{platform.variant or ''}"

    def _verify_blob_chain(self, manifest: Manifest) -> None:
        """Ensure config and every layer blob exists with a matching digest."""
        descriptors: tuple[Descriptor, ...] = (manifest.config, *manifest.layers)
        for descriptor in descriptors:
            parse_digest(descriptor.digest)  # reject unknown algorithms early
            blob = self._store.get_blob(descriptor.digest)
            verify_digest(blob, descriptor.digest)

    def _load_verified(
        self, root_digest: str, target_platform: Platform | None
    ) -> tuple[Manifest, str, Platform, str]:
        """Return (manifest, manifest_digest, matched_platform, source)."""
        parse_digest(root_digest)
        root = self._store.get_object(root_digest)
        # Defense in depth: the stored object must match the referenced digest
        # (tag bindings cannot point at content with a different digest).
        verify_digest(root, root_digest)

        if isinstance(root, Manifest):
            self._verify_blob_chain(root)
            platform = target_platform or self._default_platform
            if platform is None:
                # Single manifests have no platform descriptor; without any
                # requested platform we must not fabricate one for policy.
                platform = Platform(os="unknown", architecture="unknown")
            return root, root_digest, platform, "manifest"

        if isinstance(root, ImageIndex):
            selection = select_descriptor(
                root.manifests,
                target_platform,
                self._os_fallback,
                self._arch_fallback,
                self._default_platform,
            )
            child_digest = selection.descriptor.digest
            parse_digest(child_digest)
            child = self._store.get_object(child_digest)
            if not isinstance(child, Manifest):
                raise PartialWriteError(
                    f"index {root_digest} descriptor {child_digest} points at a "
                    "non-manifest object; nested indexes are not supported"
                )
            verify_digest(child, child_digest)
            self._verify_blob_chain(child)
            return child, child_digest, selection.platform, "index"

        raise PartialWriteError(
            f"root object {root_digest} has unsupported type {type(root).__name__}"
        )

    def resolve(
        self,
        reference: ImageReference,
        target_platform: Platform | None = None,
    ) -> ResolvedImage:
        """Resolve *reference* for *target_platform* to a verified manifest."""
        # Tag -> digest is resolved outside the cache so a tag rebound to a new
        # manifest never yields a stale result.
        root_digest = self._store.resolve_ref(reference)
        key = f"{root_digest}|{self._platform_signature(target_platform)}"

        def loader() -> ResolvedImage:
            manifest, manifest_digest, matched, source = self._load_verified(
                root_digest, target_platform
            )
            return ResolvedImage(
                reference=reference,
                manifest_digest=manifest_digest,
                manifest=manifest,
                layer_digests=tuple(layer.digest for layer in manifest.layers),
                matched_platform=matched,
                source=source,
                selection_reason=(
                    "single manifest reference"
                    if source == "manifest"
                    else "matched via multi-architecture index"
                ),
            )

        resolved = self._cache.get_or_load(key, loader)
        # Policy is enforced on every call (also cache hits) with stable
        # reasons, so rule changes apply immediately.
        authorize(
            self._rule_set, reference, resolved.manifest_digest, resolved.matched_platform
        )
        return resolved

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def _stage_manifest_blobs(
        self, manifest: Manifest, blobs: Mapping[str, bytes]
    ) -> None:
        descriptors = (manifest.config, *manifest.layers)
        for descriptor in descriptors:
            parse_digest(descriptor.digest)
            if self._store.has_object(descriptor.digest):
                # Trust nothing: re-verify the already-committed blob.
                verify_digest(self._store.get_blob(descriptor.digest), descriptor.digest)
                continue
            if descriptor.digest not in blobs:
                raise ReferencedObjectMissingError(
                    f"manifest references missing blob {descriptor.digest}"
                )
            blob = blobs[descriptor.digest]
            verify_digest(blob, descriptor.digest)
            self._store.put_blob(descriptor.digest, blob)

    def publish(
        self,
        reference: ImageReference,
        content: Manifest | ImageIndex,
        blobs: Mapping[str, bytes] | None = None,
        objects: Mapping[str, Manifest | ImageIndex] | None = None,
    ) -> str:
        """Verify and atomically publish *content*; return its digest.

        *blobs* supplies config/layer bytes; *objects* supplies child
        manifests/indexes referenced by an index.  Everything is staged and
        verified in one transaction.  On any failure the transaction is rolled
        back, nothing is cached and the last committed version keeps serving.
        """
        from .digest import compute_digest

        blobs = blobs or {}
        objects = dict(objects or {})
        with self._publish_lock:
            self._store.begin()
            try:
                if isinstance(content, Manifest):
                    self._stage_manifest_blobs(content, blobs)
                elif isinstance(content, ImageIndex):
                    # Stage any newly supplied child objects first; they remain
                    # invisible until the whole index commits atomically.
                    for child_digest, child_obj in objects.items():
                        if not isinstance(child_obj, Manifest):
                            raise PartialWriteError(
                                f"index object {child_digest} is not a manifest"
                            )
                        # The map key must equal the manifest's real digest.
                        verify_digest(child_obj, child_digest)
                        if not self._store.has_object(child_digest):
                            self._stage_manifest_blobs(child_obj, blobs)
                            self._store.put_object(child_digest, child_obj)
                    for descriptor in content.manifests:
                        parse_digest(descriptor.digest)
                        staged_child = objects.get(descriptor.digest)
                        if staged_child is not None:
                            child = staged_child
                        elif self._store.has_object(descriptor.digest):
                            child = self._store.get_object(descriptor.digest)
                        else:
                            raise ReferencedObjectMissingError(
                                f"index candidate {descriptor.digest} is missing "
                                "from both objects and the committed store"
                            )
                        if not isinstance(child, Manifest):
                            raise PartialWriteError(
                                f"index candidate {descriptor.digest} is not a manifest"
                            )
                        verify_digest(child, descriptor.digest)
                else:
                    raise PartialWriteError(
                        f"unsupported publish content type {type(content).__name__}"
                    )

                digest = compute_digest(content)
                self._store.put_object(digest, content)
                if reference.tag is not None:
                    self._store.bind_tag(reference.name, reference.tag, digest)
                self._store.commit_version()
            except BaseException:
                self._store.rollback()
                raise
            # Content-addressed entries are immutable, but a tag may have moved
            # to a new digest; drop every entry to guarantee freshness.
            self._cache.invalidate()
            return digest
