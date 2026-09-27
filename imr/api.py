"""HTTP interface for the resolver service.

Exposes stable JSON error envelopes: ``{"error": {"code", "message"}}``.
The ``code`` mirrors the domain error codes, so callers can branch on
failures without parsing prose.
"""

from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import ResolverError
from .models import Platform
from .platform import PlatformSelector
from .policy import DefaultDecision, Policy, PolicyEffect, PolicyField, PolicyRule
from .service import ResolverService
from .resolver import parse_object
from .store import BatchUpdate

# Domain error code -> HTTP status.
_STATUS: dict[str, int] = {
    "reference_invalid": 400,
    "digest_unsupported_algorithm": 400,
    "manifest_invalid": 400,
    "platform_missing": 400,
    "blob_not_found": 404,
    "platform_not_found": 404,
    "policy_denied": 403,
    "ambiguous_candidate": 409,
    "update_conflict": 409,
    "digest_mismatch": 422,
}


class PlatformIn(BaseModel):
    os: str
    architecture: str
    variant: str | None = None
    os_version: str | None = None


class ResolveIn(BaseModel):
    reference: str
    platform: PlatformIn | None = None


class CommitIn(BaseModel):
    # blobs: digest -> base64 content
    blobs: dict[str, str] = Field(default_factory=dict)
    # manifests: digest -> base64 raw payload (type auto-detected)
    manifests: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, str | None] = Field(default_factory=dict)
    expected_version: int | None = None


class RuleIn(BaseModel):
    field: str
    pattern: str
    effect: str = "deny"


def create_app(service: ResolverService | None = None) -> FastAPI:
    app = FastAPI(title="image-manifest-resolver", version="0.1.0")
    svc = service or ResolverService(selector=PlatformSelector())

    @app.exception_handler(ResolverError)
    async def _on_domain_error(_request, exc: ResolverError):
        status = _STATUS.get(exc.code, 500)
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "version": svc.version}

    @app.post("/v1/resolve")
    def resolve(body: ResolveIn):
        target = None
        if body.platform is not None:
            target = Platform(
                os=body.platform.os,
                architecture=body.platform.architecture,
                variant=body.platform.variant,
                os_version=body.platform.os_version,
            )
        result = svc.resolve(body.reference, target)
        return {
            "reference": str(result.reference),
            "manifest_digest": result.manifest_digest,
            "index_digest": result.index_digest,
            "layer_digests": list(result.layer_digests),
            "platform": (
                None
                if result.platform is None
                else {
                    "os": result.platform.os,
                    "architecture": result.platform.architecture,
                    "variant": result.platform.variant,
                }
            ),
            "matched_by": result.matched_by,
            "store_version": svc.version,
        }

    @app.post("/v1/commits")
    def commit(body: CommitIn):
        blobs = {
            digest: base64.b64decode(payload)
            for digest, payload in body.blobs.items()
        }
        objects: dict = {}
        for digest, payload in body.manifests.items():
            raw = base64.b64decode(payload)
            objects[digest] = parse_object(digest, raw)
        snap = svc.commit(
            BatchUpdate(
                put_blobs=blobs,
                put_manifests=objects,
                tag_updates=dict(body.tags),
                expected_version=body.expected_version,
            )
        )
        return {"committed": True, "version": snap.version}

    @app.get("/v1/policy")
    def get_policy():
        return {
            "rules": [
                {"field": r.field.value, "pattern": r.pattern,
                 "effect": r.effect.value}
                for r in svc.policy.rules
            ],
            "default": svc.policy.default.value,
        }

    @app.post("/v1/policy/rules")
    def add_rule(rule: RuleIn):
        svc.policy.rules.append(
            PolicyRule(
                PolicyField(rule.field),
                rule.pattern,
                PolicyEffect(rule.effect),
            )
        )
        return {"rules": len(svc.policy.rules)}

    @app.delete("/v1/policy/rules")
    def reset_policy(default: str = "allow"):
        svc.policy.rules.clear()
        svc.policy.default = DefaultDecision(default)
        return {"rules": 0, "default": svc.policy.default.value}

    return app
