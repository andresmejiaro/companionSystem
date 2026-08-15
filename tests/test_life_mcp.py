import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from life_mcp.app import create_app
from life_mcp.config import Settings
from life_mcp.snapshot import SnapshotError, SnapshotReadOnly, SnapshotStore


def sample_snapshot(generated_at="2026-03-08T10:07:52Z"):
    return {
        "summary": {
            "generated_at_utc": generated_at,
            "docs_root": "/home/private/Documents",
            "conflicts_count": 0,
            "categories": {"job": 1, "certification": 1},
            "resolution_policy": {"priority": ["official", "linkedin"]},
        },
        "entities": [
            {
                "entity_key": "job|capgemini|2026",
                "category": "job",
                "organization": "Capgemini",
                "title": "Gen AI - MLE",
                "start_date": "2026-01-07",
                "start_precision": "day",
                "end_date": None,
                "end_precision": "open",
                "highlights": "Current role.",
                "technologies": ["Python", "C#"],
                "sources": ["source-hash-1"],
                "source_types": ["employment_certificate"],
                "tags": ["official_record"],
                "resolution_policy": "authority_first_then_precision",
                "source_precedence_note": "Official record wins.",
            },
            {
                "entity_key": "certification|azure|2026",
                "category": "certification",
                "organization": "Microsoft",
                "title": "Azure certification",
                "start_date": "2026-02-09",
                "start_precision": "day",
                "end_date": None,
                "end_precision": "unknown",
                "sources": ["source-hash-2"],
                "source_types": ["professional_certification"],
                "tags": [],
                "resolution_policy": "authority_first_then_precision",
                "source_precedence_note": "Official record wins.",
            },
        ],
    }


def settings(tmp_path: Path, **changes):
    values = dict(
        public_base_url="https://life.example",
        data_dir=tmp_path,
        connector_tokens=["connector-secret"],
        oauth_signing_key="signing-secret-which-is-long-enough",
        admin_secret="admin-secret",
        auth_required=True,
        allowed_origins=["https://chatgpt.com", "https://*.claude.ai"],
        allowed_redirect_hosts=["chatgpt.com", "*.chatgpt.com", "claude.ai", "*.claude.ai"],
    )
    values.update(changes)
    return Settings(**values)


@pytest.fixture
def app(tmp_path):
    value = create_app(settings(tmp_path))
    value.state.snapshot_store.publish(sample_snapshot())
    return value


@pytest.fixture
def client(app):
    with TestClient(app) as value:
        yield value


def rpc(client, method, params=None, request_id=1):
    return client.post(
        "/mcp",
        headers={"Authorization": "Bearer connector-secret"},
        json={"jsonrpc": "2.0", "id": request_id, "method": method,
              "params": params or {}},
    ).json()


def call(client, name, arguments=None):
    return rpc(client, "tools/call", {
        "name": name, "arguments": arguments or {},
    })["result"]


def test_publish_is_versioned_atomic_and_strips_local_path(tmp_path):
    store = SnapshotStore(tmp_path)
    first = store.publish(sample_snapshot())
    assert len(first["snapshot_id"]) == 64
    assert first["previous_snapshot_id"] is None
    assert "docs_root" not in store.load()["summary"]
    second = store.publish(sample_snapshot("2026-08-09T12:00:00Z"))
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["previous_snapshot_id"] == first["snapshot_id"]
    assert (tmp_path / "snapshots" / f"{first['snapshot_id']}.json").exists()
    assert store.publish(sample_snapshot("2026-08-09T12:00:00Z")) == second


def test_publish_rejects_duplicate_entities_and_private_evidence(tmp_path):
    duplicate = sample_snapshot()
    duplicate["entities"].append(dict(duplicate["entities"][0]))
    with pytest.raises(SnapshotError, match="duplicate entity_key"):
        SnapshotStore(tmp_path).publish(duplicate)
    private = sample_snapshot()
    private["entities"][0]["source_rel_path"] = "private/certificate.pdf"
    with pytest.raises(SnapshotError, match="private field"):
        SnapshotStore(tmp_path).publish(private)


def test_read_only_mode_blocks_publication_but_keeps_snapshot_readable(tmp_path):
    writable = SnapshotStore(tmp_path)
    writable.publish(sample_snapshot())

    readonly = SnapshotStore(tmp_path, read_only=True)
    assert readonly.status()["published"] is True
    with pytest.raises(SnapshotReadOnly, match="publication is disabled"):
        readonly.publish(sample_snapshot("2026-08-14T10:00:00Z"))


def test_health_is_public_but_mcp_requires_auth(client):
    health = client.get("/health")
    assert health.json() == {
        "status": "healthy", "service": "life-career-truth-mcp",
        "version": "0.1.0", "snapshot_published": True, "read_only": False,
    }
    denied = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    })
    assert denied.status_code == 401
    assert "resource_metadata=" in denied.headers["www-authenticate"]


def test_tool_surface_is_read_only_and_has_no_mutation_path(client):
    tools = rpc(client, "tools/list")["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "career_source_status", "search_career_entities", "get_career_entity",
        "list_career_timeline", "get_career_provenance",
    }
    for tool in tools:
        assert tool["annotations"] == {
            "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": False,
        }
    removed = rpc(client, "tools/call", {
        "name": "publish_snapshot", "arguments": {"content": "{}"},
    })
    assert removed["error"]["code"] == -32602


def test_read_tools_share_one_snapshot_and_preserve_provenance(client):
    status = call(client, "career_source_status")["structuredContent"]
    found = call(client, "search_career_entities", {
        "query": "Python", "category": "job",
    })["structuredContent"]
    entity_key = found["items"][0]["entity_key"]
    entity = call(client, "get_career_entity", {
        "entity_key": entity_key,
    })["structuredContent"]
    timeline = call(client, "list_career_timeline", {
        "category": "job",
    })["structuredContent"]
    provenance = call(client, "get_career_provenance", {
        "entity_key": entity_key,
    })["structuredContent"]
    assert status["snapshot_id"] == found["snapshot_id"] == entity["snapshot_id"]
    assert timeline["items"][0]["organization"] == "Capgemini"
    assert provenance["sources"] == ["source-hash-1"]
    assert "source_rel_path" not in provenance


def test_unknown_or_invalid_calls_return_safe_errors(client):
    unknown = rpc(client, "tools/call", {
        "name": "update_career_entity", "arguments": {},
    })
    assert unknown["error"]["code"] == -32602
    invalid = call(client, "search_career_entities", {"query": ""})
    assert invalid["isError"] is True
    missing = call(client, "get_career_entity", {"entity_key": "missing"})
    assert missing["structuredContent"]["error"]["code"] == "not_found"


def test_chatgpt_dcr_pkce_flow_issues_working_token(client):
    redirect_uri = "https://chatgpt.com/aip/callback"
    registration = client.post("/oauth/register", json={
        "client_name": "ChatGPT", "redirect_uris": [redirect_uri],
    })
    client_id = registration.json()["client_id"]
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    authorize = {
        "client_id": client_id, "redirect_uri": redirect_uri,
        "response_type": "code", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "client-state",
    }
    page = client.get("/oauth/authorize", params=authorize)
    assert "Authorize Life" in page.text
    approval = client.post("/oauth/authorize", data={
        **authorize, "scope": "mcp", "admin_secret": "admin-secret",
    }, follow_redirects=False)
    assert approval.status_code == 303
    query = parse_qs(urlparse(approval.headers["location"]).query)
    exchanged = client.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": query["code"][0],
        "client_id": client_id, "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    })
    token = exchanged.json()["access_token"]
    live = client.post("/mcp", headers={"Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 2, "method": "ping",
    })
    assert live.json()["result"] == {}
