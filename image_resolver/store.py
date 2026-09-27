"""Content store with atomic versioned updates.

The store holds content-addressable objects (manifests/indexes/blobs) keyed by
digest plus tag bindings.  Readers always observe a single, fully committed
:class:`_Version` snapshot; writers stage changes in a transaction and publish
them atomically with :meth:`ContentStore.commit_version`.  A failed update must
call :meth:`ContentStore.rollback` (or use the store as a context manager), in
which case the previous complete version keeps serving traffic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import TracebackType

from .errors import (
    ConcurrentUpdateError,
    PartialWriteError,
    ReferencedObjectMissingError,
)
from .models import ContentObject, ImageReference

# Sentinel used while a transaction is mid-commit to fail other writers fast.
_COMMITTING = object()


@dataclass(frozen=True)
class _Version:
    """An immutable, fully committed store version."""

    sequence: int
    objects: dict[str, ContentObject]
    tags: dict[tuple[str, str], str]  # (name, tag) -> digest
    refs: dict[str, str]  # "registry/name:tag" -> digest


class ContentStore:
    """In-memory store of digest-keyed objects and reference bindings."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._version = _Version(sequence=0, objects={}, tags={}, refs={})
        # Staged transaction state; None when no transaction is active.
        self._pending_objects: dict[str, ContentObject] | None = None
        self._pending_tags: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ #
    # Transaction management
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        """Open a staging transaction; fails if one is already active."""
        with self._lock:
            if self._pending_objects is not None:
                raise ConcurrentUpdateError(
                    "an update transaction is already in progress"
                )
            self._pending_objects = {}
            self._pending_tags = {}

    def __enter__(self) -> "ContentStore":
        self.begin()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.commit_version()
        else:
            self.rollback()

    def _require_tx(self) -> dict[str, ContentObject]:
        if self._pending_objects is None:
            raise PartialWriteError(
                "no active transaction; call begin() or use the store as a "
                "context manager before staging writes"
            )
        return self._pending_objects

    def put_object(self, digest: str, content: ContentObject) -> None:
        """Stage *content* under *digest*; visible only after commit."""
        with self._lock:
            pending = self._require_tx()
            existing = pending.get(digest) or self._version.objects.get(digest)
            if existing is not None and existing != content:
                raise PartialWriteError(
                    f"conflicting content staged for digest {digest}"
                )
            pending.setdefault(digest, content)

    def put_blob(self, digest: str, content: bytes) -> None:
        """Stage a raw layer/config blob; visible only after commit."""
        with self._lock:
            pending = self._require_tx()
            existing = pending.get(digest) or self._version.objects.get(digest)
            if existing is not None and existing != content:
                raise PartialWriteError(
                    f"conflicting blob staged for digest {digest}"
                )
            pending.setdefault(digest, content)

    def bind_tag(self, name: str, tag: str, digest: str) -> None:
        """Stage a tag binding within the current transaction."""
        with self._lock:
            self._require_tx()
            self._pending_tags[(name, tag)] = digest

    def commit_version(self) -> int:
        """Atomically publish every staged write as a new immutable version.

        On failure the staged writes are discarded and the previously committed
        version keeps serving; no partial version is ever visible.
        """
        with self._lock:
            pending_objects = self._require_tx()
            pending_tags = self._pending_tags
            try:
                # Merge into brand-new mappings, then swap the single reference.
                objects = dict(self._version.objects)
                objects.update(pending_objects)
                tags = dict(self._version.tags)
                tags.update(pending_tags)
                refs = dict(self._version.refs)
                for (name, tag), digest in tags.items():
                    if digest not in objects:
                        raise PartialWriteError(
                            f"cannot commit tag {name}:{tag}: target digest "
                            f"{digest} is absent from this transaction"
                        )
                    refs[f"{name}:{tag}"] = digest
                new_version = _Version(
                    sequence=self._version.sequence + 1,
                    objects=objects,
                    tags=tags,
                    refs=refs,
                )
            except BaseException:
                # Never leave staged garbage behind after a failed commit.
                self._pending_objects = None
                self._pending_tags = {}
                raise
            # Single atomic reference publication.
            self._version = new_version
            self._pending_objects = None
            self._pending_tags = {}
            return new_version.sequence

    def rollback(self) -> None:
        """Discard staged writes; the committed version stays untouched."""
        with self._lock:
            self._pending_objects = None
            self._pending_tags = {}

    # ------------------------------------------------------------------ #
    # Reads (always against one committed snapshot)
    # ------------------------------------------------------------------ #
    def get_object(self, digest: str) -> ContentObject:
        """Return object from the last committed version or raise."""
        version = self._version  # one snapshot per read
        try:
            return version.objects[digest]
        except KeyError:
            # Staged but uncommitted content must never leak.
            raise ReferencedObjectMissingError(
                f"referenced object {digest} is absent from committed version "
                f"{version.sequence}"
            ) from None

    def get_blob(self, digest: str) -> bytes:
        obj = self.get_object(digest)
        if not isinstance(obj, bytes):
            raise ReferencedObjectMissingError(f"digest {digest} is not a blob")
        return obj

    def has_object(self, digest: str) -> bool:
        return digest in self._version.objects

    def resolve_ref(self, reference: ImageReference) -> str:
        """Resolve a reference (digest-pinned or tagged) to a digest."""
        version = self._version
        if reference.digest is not None:
            if reference.digest not in version.objects:
                raise ReferencedObjectMissingError(
                    f"reference {reference.canonical_name}@{reference.digest} "
                    "target is missing"
                )
            return reference.digest
        if reference.tag is None:
            raise PartialWriteError(
                f"reference {reference.canonical_name} has neither tag nor digest"
            )
        key = (reference.name, reference.tag)
        try:
            return version.tags[key]
        except KeyError:
            raise ReferencedObjectMissingError(
                f"tag {reference.name}:{reference.tag} is not bound in committed "
                f"version {version.sequence}"
            ) from None

    def current_version(self) -> int:
        return self._version.sequence
