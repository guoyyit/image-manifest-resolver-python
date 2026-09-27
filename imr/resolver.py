"""End-to-end resolution: reference -> platform-matched manifest + layers.

Resolution steps
----------------
1. Parse the reference and resolve it to a root digest (via tag or digest).
2. Load the root object (blob + parsed manifest/index), verifying its digest.
3. If it is an index, select one child deterministically via
   ``PlatformSelector`` and load that manifest, verifying each link.
4. Verify the manifest's layer digest chain against stored blobs.
5. Enforce the ``Policy`` (denials produce stable reasons).

Digest verification failures, unknown algorithms and missing targets raise
distinguishable errors; such failures are never cached.
"""

from __future__ import annotations

import json

from .cache import ResolutionCache
from .digest import parse_digest, verify_digest
from .errors import (
    BlobNotFound,
    DigestMismatch,
    DigestUnsupportedAlgorithm,
    ManifestInvalid,
    PolicyDenied,
)
from .models import (
    Descriptor,
    DescriptorKind,
    ImageIndex,
    ImageReference,
    Manifest,
    Platform,
    ResolveResult,
)
from .platform import PlatformSelector
from .policy import Policy
from .reference import parse_reference
from .store import ContentStore


def classify(media_type: str) -> DescriptorKind:
    """Classify a media type as index, manifest, or unknown."""
    from .models import INDEX_MEDIA_TYPES, MANIFEST_MEDIA_TYPES

    if media_type in INDEX_MEDIA_TYPES:
        return DescriptorKind.INDEX
    if media_type in MANIFEST_MEDIA_TYPES:
        return DescriptorKind.MANIFEST
    return DescriptorKind.UNKNOWN


def _descriptor_from_json(data: dict, *, with_platform: bool) -> Descriptor:
    digest = data.get("digest")
    if not isinstance(digest, str):
        raise ManifestInvalid("descriptor missing string 'digest'")
    parse_digest(digest)  # validate algorithm/shape up front
    platform: Platform | None = None
    if with_platform and isinstance(data.get("platform"), dict):
        platform = _platform_from_json(data["platform"])
    return Descriptor(
        digest=digest,
        media_type=str(data.get("mediaType", "")),
        size=int(data.get("size", -1)),
        platform=platform,
        annotations=dict(data.get("annotations") or {}),
    )


def _platform_from_json(data: dict) -> Platform:
    try:
        os_name = data["os"]
        architecture = data["architecture"]
    except KeyError as exc:
        raise ManifestInvalid(f"platform missing field {exc}") from exc
    if not isinstance(os_name, str) or not isinstance(architecture, str):
        raise ManifestInvalid("platform os/architecture must be strings")
    variant = data.get("variant")
    os_version = data.get("os.version")
    features = data.get("os.features") or ()
    return Platform(
        os=os_name,
        architecture=architecture,
        variant=variant if isinstance(variant, str) else None,
        os_version=os_version if isinstance(os_version, str) else None,
        os_features=tuple(features) if isinstance(features, (list, tuple)) else (),
    )


def _load_json(raw: bytes, digest: str) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestInvalid(f"{digest}: payload is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ManifestInvalid(f"{digest}: manifest payload is not a JSON object")
    return data


def _detect_media_type(data: dict, provided: str, digest: str) -> str:
    media_type = provided or str(data.get("mediaType", ""))
    if not media_type:
        # Structural fallback (used when a registry strips mediaType).
        if isinstance(data.get("manifests"), list):
            from .models import INDEX_MEDIA_TYPES

            media_type = next(iter(INDEX_MEDIA_TYPES))
        elif isinstance(data.get("layers"), list):
            from .models import MANIFEST_MEDIA_TYPES

            media_type = next(iter(MANIFEST_MEDIA_TYPES))
    if classify(media_type) is DescriptorKind.UNKNOWN:
        raise ManifestInvalid(f"{digest}: unsupported media type {media_type!r}")
    return media_type


def parse_manifest(digest: str, raw: bytes, media_type: str = "") -> Manifest:
    """Parse and validate a single image manifest JSON payload."""
    verify_digest(raw, digest)
    data = _load_json(raw, digest)
    media_type = _detect_media_type(data, media_type, digest)
    if classify(media_type) is not DescriptorKind.MANIFEST:
        raise ManifestInvalid(f"{digest}: expected manifest, got {media_type!r}")

    config = data.get("config")
    config_digest: str | None = None
    if isinstance(config, dict):
        config_digest = config.get("digest")
        if config_digest is not None:
            parse_digest(config_digest)

    layers_raw = data.get("layers")
    if not isinstance(layers_raw, list):
        raise ManifestInvalid(f"{digest}: manifest 'layers' must be a list")
    layers = tuple(_descriptor_from_json(item, with_platform=False) for item in layers_raw)

    return Manifest(
        digest=digest,
        media_type=media_type,
        raw=raw,
        config_digest=config_digest,
        layers=layers,
    )


def parse_index(digest: str, raw: bytes, media_type: str = "") -> ImageIndex:
    """Parse and validate an image index JSON payload."""
    verify_digest(raw, digest)
    data = _load_json(raw, digest)
    media_type = _detect_media_type(data, media_type, digest)
    if classify(media_type) is not DescriptorKind.INDEX:
        raise ManifestInvalid(f"{digest}: expected index, got {media_type!r}")

    entries_raw = data.get("manifests")
    if not isinstance(entries_raw, list):
        raise ManifestInvalid(f"{digest}: index 'manifests' must be a list")
    entries = tuple(
        _descriptor_from_json(item, with_platform=True) for item in entries_raw
    )
    return ImageIndex(
        digest=digest, media_type=media_type, raw=raw, manifests=entries
    )


def parse_object(digest: str, raw: bytes, media_type: str = "") -> Manifest | ImageIndex:
    """Parse a raw payload into a Manifest or ImageIndex, auto-detecting kind."""
    if media_type:
        kind = classify(media_type)
        if kind is DescriptorKind.INDEX:
            return parse_index(digest, raw, media_type)
        if kind is DescriptorKind.MANIFEST:
            return parse_manifest(digest, raw, media_type)
    data = _load_json(raw, digest)
    detected = _detect_media_type(data, media_type, digest)
    if classify(detected) is DescriptorKind.INDEX:
        return parse_index(digest, raw, detected)
    return parse_manifest(digest, raw, detected)


class Resolver:
    def __init__(
        self,
        store: ContentStore,
        selector: PlatformSelector | None = None,
        policy: Policy | None = None,
        cache: ResolutionCache[ResolveResult] | None = None,
    ) -> None:
        self.store = store
        self.selector = selector or PlatformSelector()
        self.policy = policy or Policy()
        self.cache = cache if cache is not None else ResolutionCache()

    def resolve(self, reference: str, target: Platform | None = None) -> ResolveResult:
        ref = parse_reference(reference)
        snap = self.store.snapshot()

        root_digest = self._root_digest(ref, snap.version)
        # Content-based key: same (root, platform) can never resolve to a
        # different result while the store version is unchanged.
        cache_key = ("resolve", root_digest, target)
        result = self.cache.get_or_load(
            cache_key, snap.version, lambda: self._resolve_digest(ref, root_digest, target)
        )

        # Policy is evaluated on every call against the final concrete
        # target (chosen manifest digest + matched platform), so a policy
        # change takes effect immediately and is never bypassed by cache.
        concrete_ref = ImageReference(
            name=result.reference.name,
            tag=result.reference.tag,
            digest=result.manifest_digest,
        )
        self.policy.evaluate(concrete_ref, result.platform)
        return result

    def _root_digest(self, ref: ImageReference, version: int) -> str:
        if ref.digest is not None:
            return ref.digest
        tag = ref.tag or "latest"
        resolved = self.store.resolve_tag(ref.name, tag)
        if resolved is None:
            raise BlobNotFound(f"tag not found: {ref.name}:{tag}")
        return resolved

    def _load_object(
        self, digest: str, media_type_hint: str = ""
    ) -> Manifest | ImageIndex:
        raw = self.store.get_blob(digest)
        if raw is None:
            raise BlobNotFound(f"blob missing: {digest}")
        verify_digest(raw, digest)  # chain integrity at load time
        return parse_object(digest, raw, media_type_hint)

    def _resolve_digest(
        self,
        ref: ImageReference,
        root_digest: str,
        target: Platform | None,
    ) -> ResolveResult:
        root = self._load_object(root_digest)
        index_digest: str | None = None
        matched_by = "direct"

        if isinstance(root, ImageIndex):
            index_digest = root.digest
            chosen, matched_by = self.selector.select(root.manifests, target)
            manifest = self._load_object(chosen.digest, chosen.media_type)
            if not isinstance(manifest, Manifest):
                raise ManifestInvalid(
                    f"index entry {chosen.digest} resolved to non-manifest "
                    "(nested indexes are not supported)"
                )
            platform_for_result = chosen.platform
        elif isinstance(root, Manifest):
            manifest = root
            platform_for_result = target
        else:  # pragma: no cover - _load_object never returns anything else
            raise ManifestInvalid(f"{root_digest}: unknown object kind")

        # Verify the full layer chain against present blobs.
        for layer in manifest.layers:
            if self.store.get_blob(layer.digest) is None:
                raise BlobNotFound(f"layer blob missing: {layer.digest}")

        return ResolveResult(
            reference=ref,
            manifest_digest=manifest.digest,
            manifest=manifest,
            layer_digests=manifest.layer_digests,
            platform=platform_for_result,
            matched_by=matched_by,
            index_digest=index_digest,
        )
