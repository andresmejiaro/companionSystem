"""HTTP API for the Assistant Profile OS (slice zero).

HTTP was chosen over MCP for slice zero: it is testable with plain httpx,
serves the future web/mobile UI directly, and an MCP adapter can wrap these
same service calls later (see ARCHITECTURE.md).
"""

from __future__ import annotations

import os

from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import seed
from .access import AccessControl
from .dynstores import DynamicStores
from .errors import (DynStoreConflict, DynStoreNotFound, MalformedMemoryEvent,
                     MalformedRecord, ProfileNotFound, SchemaError)
from .storage import Store

DATA_DIR = os.environ.get("PROFILE_OS_DATA_DIR", "data")


class MemoryEventIn(BaseModel):
    kind: str
    content: str
    tags: list[str] = Field(default_factory=list)


class CloseoutIn(BaseModel):
    notes: str = ""
    new_state: str


class DomainRecordIn(BaseModel):
    data: dict


class StoreProposalIn(BaseModel):
    name: str
    purpose: str
    proposed_by: str
    schema_def: dict = Field(alias="schema")


class RejectIn(BaseModel):
    reason: str


def create_app(data_dir: str = DATA_DIR, do_seed: bool = True,
               auth_enabled: bool | None = None) -> FastAPI:
    if auth_enabled is None:
        auth_enabled = os.environ.get("PROFILE_OS_AUTH_ENABLED") == "1"
    app = FastAPI(title="Assistant Profile OS", version="0.1.0")
    store = Store(data_dir)
    if do_seed:
        seed.seed(store)
    app.state.store = store

    dyn = DynamicStores(store)
    app.state.dynstores = dyn
    access = AccessControl(store)
    app.state.access = access

    def _authenticate(authorization: str | None) -> str | None:
        """Resolve the bearer credential to a principal id, or 401.

        Returns None only when auth is disabled (the local default). The
        credential belongs to a principal/client (see ACCESS_CONTROL.md),
        never to a profile.
        """
        if not auth_enabled:
            return None
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer credential",
                                headers={"WWW-Authenticate": "Bearer"})
        principal_id = access.authenticate_secret(authorization[len("Bearer "):])
        if principal_id is None:
            raise HTTPException(401, "invalid, expired, or revoked credential",
                                headers={"WWW-Authenticate": "Bearer"})
        return principal_id

    def _require(operation: str, profile_id: str,
                 authorization: str | None) -> None:
        """Missing/bad credential → 401; authenticated but ungranted → 403.

        No-op while auth is disabled.
        """
        principal_id = _authenticate(authorization)
        if principal_id is None:
            return
        if not access.allowed(principal_id, operation, profile_id):
            raise HTTPException(403, f"principal lacks {operation} on {profile_id!r}")

    def _wrap(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ProfileNotFound, DynStoreNotFound) as e:
            raise HTTPException(404, str(e))
        except DynStoreConflict as e:
            raise HTTPException(409, str(e))
        except (MalformedMemoryEvent, MalformedRecord, SchemaError) as e:
            raise HTTPException(422, str(e))

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/demo", response_class=HTMLResponse)
    def demo():
        """Human-readable demo console (static page, no build step, no LLM)."""
        return (Path(__file__).parent / "demo.html").read_text()

    @app.get("/profiles")
    def list_profiles(authorization: str | None = Header(None)):
        principal_id = _authenticate(authorization)
        profiles = store.list_profiles()
        if principal_id is None:
            return profiles
        visible = access.visible_profile_ids(principal_id)
        if visible is None:  # wildcard grant
            return profiles
        return [p for p in profiles if p["id"] in visible]

    @app.get("/profiles/{profile_id}")
    def get_profile(profile_id: str, authorization: str | None = Header(None)):
        _require("boot", profile_id, authorization)
        return _wrap(store.get_profile, profile_id)

    @app.post("/profiles/{profile_id}/boot")
    def boot(profile_id: str, authorization: str | None = Header(None)):
        _require("boot", profile_id, authorization)
        return _wrap(store.boot, profile_id)

    @app.post("/profiles/{profile_id}/memories", status_code=201)
    def remember(profile_id: str, event: MemoryEventIn,
                 authorization: str | None = Header(None)):
        _require("remember", profile_id, authorization)
        return _wrap(store.remember, profile_id, event.model_dump())

    @app.get("/profiles/{profile_id}/memories/search")
    def search(profile_id: str, q: str, limit: int = 20,
               authorization: str | None = Header(None)):
        _require("search", profile_id, authorization)
        return _wrap(store.search, profile_id, q, limit)

    @app.post("/profiles/{profile_id}/closeout", status_code=201)
    def closeout(profile_id: str, body: CloseoutIn,
                 authorization: str | None = Header(None)):
        _require("closeout", profile_id, authorization)
        return _wrap(store.closeout, profile_id, body.notes, body.new_state)

    @app.get("/profiles/{profile_id}/domain")
    def list_stores(profile_id: str, authorization: str | None = Header(None)):
        _require("records:read", profile_id, authorization)
        return _wrap(store.list_domain_stores, profile_id)

    @app.get("/profiles/{profile_id}/domain/{store_name}")
    def query_domain(profile_id: str, store_name: str,
                     contains: str | None = None, limit: int = 50,
                     authorization: str | None = Header(None)):
        _require("records:read", profile_id, authorization)
        return _wrap(store.query_domain, profile_id, store_name, contains, limit)

    @app.post("/profiles/{profile_id}/domain/{store_name}", status_code=201)
    def add_domain(profile_id: str, store_name: str, body: DomainRecordIn,
                   authorization: str | None = Header(None)):
        _require("records:write", profile_id, authorization)
        return _wrap(store.add_domain_record, profile_id, store_name, body.data)

    # -- dynamic stores (slice two) ------------------------------------------
    # Every route is enforced via _require when PROFILE_OS_AUTH_ENABLED=1
    # (default: off, open on localhost). See ACCESS_CONTROL.md for the map.

    @app.post("/profiles/{profile_id}/stores", status_code=201)
    def propose_store(profile_id: str, body: StoreProposalIn,
                      authorization: str | None = Header(None)):
        _require("stores:propose", profile_id, authorization)
        return _wrap(dyn.propose, profile_id, body.name, body.purpose,
                     body.proposed_by, body.schema_def)

    @app.get("/profiles/{profile_id}/stores")
    def list_stores(profile_id: str, authorization: str | None = Header(None)):
        _require("records:read", profile_id, authorization)
        return _wrap(dyn.list, profile_id)

    @app.get("/profiles/{profile_id}/stores/{name}")
    def get_store(profile_id: str, name: str,
                  authorization: str | None = Header(None)):
        _require("records:read", profile_id, authorization)
        return _wrap(dyn.get, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/approve")
    def approve_store(profile_id: str, name: str,
                      authorization: str | None = Header(None)):
        _require("stores:approve", profile_id, authorization)
        return _wrap(dyn.approve, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/reject")
    def reject_store(profile_id: str, name: str, body: RejectIn,
                     authorization: str | None = Header(None)):
        _require("stores:approve", profile_id, authorization)
        return _wrap(dyn.reject, profile_id, name, body.reason)

    @app.post("/profiles/{profile_id}/stores/{name}/archive")
    def archive_store(profile_id: str, name: str,
                      authorization: str | None = Header(None)):
        _require("stores:approve", profile_id, authorization)
        return _wrap(dyn.archive, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/records", status_code=201)
    def add_store_record(profile_id: str, name: str, body: DomainRecordIn,
                         authorization: str | None = Header(None)):
        _require("records:write", profile_id, authorization)
        return _wrap(dyn.add_record, profile_id, name, body.data)

    @app.get("/profiles/{profile_id}/stores/{name}/records")
    def query_store_records(profile_id: str, name: str,
                            contains: str | None = None, limit: int = 50,
                            authorization: str | None = Header(None)):
        _require("records:read", profile_id, authorization)
        return _wrap(dyn.query_records, profile_id, name, contains, limit)

    @app.get("/profiles/{profile_id}/stores/{name}/audit")
    def store_audit(profile_id: str, name: str, limit: int = 100,
                    authorization: str | None = Header(None)):
        _require("audit:read", profile_id, authorization)
        return _wrap(dyn.audit_events, profile_id, name, limit)

    @app.get("/profiles/{profile_id}/audit")
    def profile_audit(profile_id: str, limit: int = 100,
                      authorization: str | None = Header(None)):
        _require("audit:read", profile_id, authorization)
        return _wrap(dyn.audit_events, profile_id, None, limit)

    return app


app = create_app()
