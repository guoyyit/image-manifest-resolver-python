"""HTTP API: resolve, atomic commits, stable error envelopes, policy rules."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from imr.api import create_app
from imr.platform import DEFAULT_FALLBACK, PlatformSelector
from imr.models import Platform

from conftest import build_image, build_index, commit_images, OCI_MANIFEST

AMD = Platform("linux", "amd64")
WIN = Platform("windows", "amd64")


@pytest.fixture
def client(service):
    app = create_app(service)
    return TestClient(app)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["version"] == 0


def test_resolve_multiarch_over_http(client, service):
    amd = build_image(AMD, [b"a1", b"a2"])
    win = build_image(WIN, [b"w1"])
    commit_images(service, [amd, win], tag="app:v1")

    r = client.post("/v1/resolve", json={
        "reference": "app:v1",
        "platform": {"os": "linux", "architecture": "amd64"},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["manifest_digest"] == amd.manifest_digest
    assert body["layer_digests"] == amd.layer_digests
    assert body["matched_by"] == "exact"
    assert body["index_digest"]
    assert body["platform"]["os"] == "linux"


def test_not_found_error_envelope_is_stable(client):
    r = client.post("/v1/resolve", json={"reference": "ghost:latest"})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "blob_not_found"
    assert "message" in err


def test_bad_reference_error_envelope(client):
    r = client.post("/v1/resolve", json={"reference": "bad ref!!"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "reference_invalid"


def test_platform_not_found_envelope(client, service):
    amd = build_image(AMD, [b"a"])
    win = build_image(WIN, [b"w"])
    commit_images(service, [amd, win], tag="app:v1")
    service.selector.fallback = []
    r = client.post("/v1/resolve", json={
        "reference": "app:v1",
        "platform": {"os": "windows", "architecture": "arm64"},
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "platform_not_found"


def test_policy_denial_over_http(client, service):
    img = build_image(AMD, [b"a"])
    commit_images(service, [img], tag="app:secret", index=False)
    r = client.post("/v1/policy/rules", json={
        "field": "tag", "pattern": "*secret*", "effect": "deny"})
    assert r.status_code == 200

    r = client.post("/v1/resolve", json={
        "reference": "app:secret",
        "platform": {"os": "linux", "architecture": "amd64"},
    })
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["code"] == "policy_denied"
    assert "deny:tag:*secret*" in err["message"]


def test_commit_endpoint_atomic_success_and_failure(client, service):
    img = build_image(AMD, [b"l1"])
    payload = {
        "blobs": {
            img.config_digest: _b64(img.config_raw),
            **{d: _b64(b) for d, b in img.layer_blobs.items()},
            img.manifest_digest: _b64(img.manifest_raw),
        },
        "manifests": {img.manifest_digest: _b64(img.manifest_raw)},
        "tags": {"app:v1": img.manifest_digest},
    }
    r = client.post("/v1/commits", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1

    # Bad commit: layer referenced but not uploaded -> rejected, no bump.
    other = build_image(Platform("linux", "arm64"), [b"missing"])
    bad = {
        "blobs": {other.manifest_digest: _b64(other.manifest_raw),
                  other.config_digest: _b64(other.config_raw)},
        "manifests": {other.manifest_digest: _b64(other.manifest_raw)},
        "tags": {"app:v2": other.manifest_digest},
    }
    r = client.post("/v1/commits", json=bad)
    assert r.status_code == 404
    assert client.get("/health").json()["version"] == 1

    # Old tag still resolves.
    r = client.post("/v1/resolve", json={
        "reference": "app:v1",
        "platform": {"os": "linux", "architecture": "amd64"}})
    assert r.status_code == 200
    assert r.json()["layer_digests"] == img.layer_digests


def test_digest_mismatch_returns_422(client):
    raw = b'{"schemaVersion":2,"mediaType":"' + OCI_MANIFEST.encode() + b'","layers":[]}'
    payload = {
        "blobs": {"sha256:" + "1" * 64: _b64(raw)},
        "manifests": {"sha256:" + "1" * 64: _b64(raw)},
    }
    r = client.post("/v1/commits", json=payload)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "digest_mismatch"


def test_policy_reset_endpoint(client, service):
    client.post("/v1/policy/rules",
                json={"field": "tag", "pattern": "*", "effect": "deny"})
    r = client.get("/v1/policy")
    assert len(r.json()["rules"]) == 1
    r = client.delete("/v1/policy/rules?default=allow")
    assert r.json() == {"rules": 0, "default": "allow"}
    assert client.get("/v1/policy").json()["rules"] == []
