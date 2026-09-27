"""FastAPI HTTP surface for the resolver service.

Endpoints:

* ``GET /healthz`` -- liveness plus current committed store version.
* ``POST /resolve`` -- resolve an image reference for a target platform.
* ``POST /publish`` -- publish a single manifest (mainly for tests/demos).

Every resolver error is rendered as a stable JSON body
``{"error": <code>, "reason": <stable message>}``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    ConcurrentUpdateError,
    DigestMismatchError,
    MalformedDigestError,
    NoPlatformMatchError,
    PartialWriteError,
    PolicyDeniedError,
    ReferencedObjectMissingError,
    ResolverError,
    UnknownAlgorithmError,
)
from .models import Descriptor, ImageReference, Manifest, Platform
from .resolver import Resolver
from .store import ContentStore

# Error code -> HTTP status.  Codes are part of the API contract and stable.
_ERROR_STATUS: dict[type[ResolverError], tuple[str, int]] = {
    PolicyDeniedError: ("policy_denied", 403),
    NoPlatformMatchError: ("no_platform_match", 404),
    ReferencedObjectMissingError: ("referenced_object_missing", 404),
    DigestMismatchError: ("digest_mismatch", 422),
    UnknownAlgorithmError: ("unknown_algorithm", 422),
    MalformedDigestError: ("malformed_digest", 422),
    ConcurrentUpdateError: ("concurrent_update", 409),
    PartialWriteError: ("partial_write", 409),
}


class PlatformIn(BaseModel):
    os: str
    architecture: str
    variant: str | None = None
    os_version: str | None = None


class ReferenceIn(BaseModel):
    name: str
    tag: str | None = None
    digest: str | None = None
    registry: str = "docker.io"


class ResolveRequest(BaseModel):
    reference: ReferenceIn
    platform: PlatformIn | None = None


class DescriptorIn(BaseModel):
    digest: str
    media_type: str = "application/octet-stream"
    size: int = 0


class PublishRequest(BaseModel):
    reference: ReferenceIn
    config: DescriptorIn
    layers: list[DescriptorIn] = Field(default_factory=list)
    blobs: dict[str, str] = Field(default_factory=dict)  # digest -> utf-8 bytes


def create_app(resolver: Resolver | None = None) -> FastAPI:
    resolver = resolver if resolver is not None else Resolver(ContentStore())
    app = FastAPI(title="image-manifest-resolver", version="0.1.0")

    @app.exception_handler(ResolverError)
    async def _handle_resolver_error(_request: Any, exc: ResolverError) -> JSONResponse:
        code, status = _ERROR_STATUS.get(type(exc), ("resolver_error", 500))
        return JSONResponse(
            status_code=status,
            content={"error": code, "reason": str(exc)},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"status": "ok", "version": resolver._store.current_version()}

    @app.post("/resolve")
    def resolve(request: ResolveRequest) -> dict[str, object]:
        reference = ImageReference(
            name=request.reference.name,
            tag=request.reference.tag,
            digest=request.reference.digest,
            registry=request.reference.registry,
        )
        platform = None
        if request.platform is not None:
            platform = Platform(
                os=request.platform.os,
                architecture=request.platform.architecture,
                variant=request.platform.variant,
                os_version=request.platform.os_version,
            )
        result = resolver.resolve(reference, platform)
        return {
            "manifest_digest": result.manifest_digest,
            "layer_digests": list(result.layer_digests),
            "matched_platform": {
                "os": result.matched_platform.os,
                "architecture": result.matched_platform.architecture,
                "variant": result.matched_platform.variant,
            },
            "source": result.source,
            "selection_reason": result.selection_reason,
        }

    @app.post("/publish")
    def publish(request: PublishRequest) -> dict[str, object]:
        reference = ImageReference(
            name=request.reference.name,
            tag=request.reference.tag,
            digest=request.reference.digest,
            registry=request.reference.registry,
        )
        blobs = {digest: payload.encode("utf-8") for digest, payload in request.blobs.items()}
        manifest = Manifest(
            media_type="application/vnd.oci.image.manifest.v1+json",
            config=Descriptor(
                request.config.digest, request.config.media_type, request.config.size
            ),
            layers=tuple(
                Descriptor(d.digest, d.media_type, d.size) for d in request.layers
            ),
        )
        digest = resolver.publish(reference, manifest, blobs)
        return {"manifest_digest": digest, "store_version": resolver._store.current_version()}

    return app


app = create_app()
