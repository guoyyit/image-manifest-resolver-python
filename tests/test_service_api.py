"""HTTP API behaviour and stable error codes."""

from __future__ import annotations

import pytest

from image_resolver.digest import compute_digest
from image_resolver.policy import RuleSet
from image_resolver.resolver import Resolver
from image_resolver.service import create_app
from image_resolver.store import ContentStore

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    resolver = Resolver(ContentStore(), rule_set=RuleSet(default_decision="allow"))
    return TestClient(create_app(resolver)), resolver


def _publish(client, name="app", tag="v1", cfg=b"cfg", layer=b"layer"):
    cfg_d, layer_d = compute_digest(cfg), compute_digest(layer)
    response = client.post(
        "/publish",
        json={
            "reference": {"name": name, "tag": tag},
            "config": {"digest": cfg_d, "size": len(cfg)},
            "layers": [{"digest": layer_d, "size": len(layer)}],
            "blobs": {cfg_d: cfg.decode(), layer_d: layer.decode()},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["manifest_digest"]


def test_healthz(client):
    http, _ = client
    assert http.get("/healthz").json()["status"] == "ok"


def test_resolve_success(client):
    http, _ = client
    digest = _publish(http)
    response = http.post(
        "/resolve",
        json={
            "reference": {"name": "app", "tag": "v1"},
            "platform": {"os": "linux", "architecture": "amd64"},
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["manifest_digest"] == digest
    assert body["layer_digests"] == [compute_digest(b"layer")]
    assert body["matched_platform"]["os"] == "linux"


def test_missing_reference_returns_distinct_code(client):
    http, _ = client
    response = http.post("/resolve", json={"reference": {"name": "app", "tag": "x"}})
    assert response.status_code == 404
    assert response.json()["error"] == "referenced_object_missing"


def test_digest_mismatch_returns_distinct_code(client):
    http, _ = client
    wrong = "sha256:" + "1" * 64
    response = http.post(
        "/publish",
        json={
            "reference": {"name": "bad", "tag": "v1"},
            "config": {"digest": wrong, "size": 3},
            "blobs": {wrong: "cfg"},
        },
    )
    assert response.status_code == 422
    assert response.json()["error"] == "digest_mismatch"


def test_unknown_algorithm_returns_distinct_code(client):
    http, _ = client
    response = http.post(
        "/publish",
        json={"reference": {"name": "bad", "tag": "v1"}, "config": {"digest": "md5:x"}},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "unknown_algorithm"


def test_policy_denied_returns_403(client):
    resolver = Resolver(ContentStore(), rule_set=RuleSet())
    strict_http = TestClient(create_app(resolver))
    cfg_d = compute_digest(b"cfg")
    strict_http.post(
        "/publish",
        json={
            "reference": {"name": "a", "tag": "v1"},
            "config": {"digest": cfg_d},
            "blobs": {cfg_d: "cfg"},
        },
    )
    response = strict_http.post(
        "/resolve",
        json={
            "reference": {"name": "a", "tag": "v1"},
            "platform": {"os": "linux", "architecture": "amd64"},
        },
    )
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "policy_denied"
    assert body["reason"] == "denied by default policy"


def test_failed_publish_does_not_advance_version(client):
    http, _ = client
    _publish(http)
    version_before = http.get("/healthz").json()["version"]
    response = http.post(
        "/publish",
        json={"reference": {"name": "bad", "tag": "v1"}, "config": {"digest": "md5:x"}},
    )
    assert response.status_code == 422
    assert http.get("/healthz").json()["version"] == version_before
