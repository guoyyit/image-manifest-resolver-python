"""Atomic content store.

Reads always observe a fully committed snapshot. A ``BatchUpdate`` is
validated in full (blob digests, manifest presence, tag targets) while
building a *new* immutable snapshot; only then is the version pointer
swapped under a lock. Any failure raises before the swap, so the service
keeps serving the previous complete state.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import MappingProxyType

from .digest import verify_digest
from .errors import (
    BlobNotFound,
    DigestMismatch,
    ManifestInvalid,
    ReferenceInvalid,
    UpdateConflict,
)
from .models import ImageIndex, Manifest


@dataclass(frozen=True)
class Snapshot:
    """Immutable committed state. Mappings are read-only proxies."""

    version: int
    blobs: MappingProxyType[str, bytes]
    manifests: MappingProxyType[str, Manifest | ImageIndex]
    tags: MappingProxyType[str, str]


@dataclass(frozen=True)
class BatchUpdate:
    """A proposed atomic update.

    - put_blobs: digest -> bytes (content must hash to the key).
    - put_manifests: digest -> Manifest/ImageIndex, already parsed.
    - tag_updates: "name:tag" -> digest, or None to delete.
    - expected_version: optimistic concurrency check.
    """

    put_blobs: dict[str, bytes] = field(default_factory=dict)
    put_manifests: dict[str, Manifest | ImageIndex] = field(default_factory=dict)
    tag_updates: dict[str, str | None] = field(default_factory=dict)
    expected_version: int | None = None


class ContentStore:
    def __init__(self) -> None:
        self._snapshot = Snapshot(
            version=0,
            blobs=MappingProxyType({}),
            manifests=MappingProxyType({}),
            tags=MappingProxyType({}),
        )
        self._lock = threading.RLock()

    # ----- reads -----------------------------------------------------------

    def snapshot(self) -> Snapshot:
        # MappingProxyType wraps dicts that are never mutated after publish.
        return self._snapshot

    def resolve_tag(self, ref_name: str, tag: str) -> str | None:
        return self._snapshot.tags.get(f"{ref_name}:{tag}")

    def get_object(self, digest: str) -> Manifest | ImageIndex | None:
        return self._snapshot.manifests.get(digest)

    def get_blob(self, digest: str) -> bytes | None:
        return self._snapshot.blobs.get(digest)

    # ----- writes ----------------------------------------------------------

    def commit(self, update: BatchUpdate) -> Snapshot:
        """Validate then atomically apply ``update``; return new snapshot.

        Raises before the pointer swap on any validation error, leaving the
        current snapshot untouched.
        """
        with self._lock:
            current = self._snapshot
            if (
                update.expected_version is not None
                and update.expected_version != current.version
            ):
                raise UpdateConflict(
                    f"expected version {update.expected_version}, "
                    f"store is at {current.version}"
                )

            # 1. Verify every new blob against its content-addressed key.
            new_blobs = dict(current.blobs)
            for digest, data in update.put_blobs.items():
                if not isinstance(data, (bytes, bytearray)):
                    raise ManifestInvalid(f"blob {digest} must be bytes")
                verify_digest(bytes(data), digest)  # DigestMismatch on failure
                new_blobs[digest] = bytes(data)

            # 2. Validate and collect manifests/indexes.
            new_objects = dict(current.manifests)
            for digest, obj in update.put_manifests.items():
                if not isinstance(obj, (Manifest, ImageIndex)):
                    raise ManifestInvalid(
                        f"object {digest} is neither Manifest nor ImageIndex"
                    )
                if obj.digest != digest:
                    raise DigestMismatch(digest, obj.digest)
                # Raw payload must hash to the digest (chain integrity).
                verify_digest(obj.raw, digest)
                if isinstance(obj, Manifest):
                    self._check_manifest_targets(obj, new_blobs, new_objects)
                else:
                    self._check_index_targets(obj, new_blobs, new_objects, update)
                new_objects[digest] = obj

            # 3. Validate tag targets against the resulting object set.
            new_tags = dict(current.tags)
            for tag_key, target in update.tag_updates.items():
                if not tag_key or ":" not in tag_key:
                    raise ReferenceInvalid(f"invalid tag key: {tag_key!r}")
                if target is None:
                    new_tags.pop(tag_key, None)
                    continue
                if target not in new_objects:
                    raise BlobNotFound(
                        f"cannot tag {tag_key!r}: manifest {target} absent in update"
                    )
                new_tags[tag_key] = target

            # 4. Single atomic pointer swap.
            published = Snapshot(
                version=current.version + 1,
                blobs=MappingProxyType(new_blobs),
                manifests=MappingProxyType(new_objects),
                tags=MappingProxyType(new_tags),
            )
            self._snapshot = published
            return published

    @staticmethod
    def _check_manifest_targets(
        manifest: Manifest,
        blobs: dict[str, bytes],
        objects: dict[str, Manifest | ImageIndex],
    ) -> None:
        # Config blob must exist (allow absent config for OCI attestation
        # manifests: config_digest may be a descriptor to a blob).
        if manifest.config_digest is not None and manifest.config_digest not in blobs:
            raise BlobNotFound(
                f"manifest {manifest.digest}: config blob "
                f"{manifest.config_digest} missing"
            )
        for layer in manifest.layers:
            if layer.digest not in blobs:
                raise BlobNotFound(
                    f"manifest {manifest.digest}: layer blob {layer.digest} missing"
                )

    @staticmethod
    def _check_index_targets(
        index: ImageIndex,
        blobs: dict[str, bytes],
        objects: dict[str, Manifest | ImageIndex],
        update: BatchUpdate,
    ) -> None:
        for desc in index.manifests:
            if desc.digest not in objects:
                raise BlobNotFound(
                    f"index {index.digest}: referenced manifest {desc.digest} missing"
                )
