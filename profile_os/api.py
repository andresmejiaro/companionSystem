"""HTTP API for the Assistant Profile OS (slice zero).

HTTP was chosen over MCP for slice zero: it is testable with plain httpx,
serves the future web/mobile UI directly, and an MCP adapter can wrap these
same service calls later (see ARCHITECTURE.md).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import seed
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


def create_app(data_dir: str = DATA_DIR, do_seed: bool = True) -> FastAPI:
    app = FastAPI(title="Assistant Profile OS", version="0.1.0")
    store = Store(data_dir)
    if do_seed:
        seed.seed(store)
    app.state.store = store

    dyn = DynamicStores(store)
    app.state.dynstores = dyn

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

    @app.get("/profiles")
    def list_profiles():
        return store.list_profiles()

    @app.get("/profiles/{profile_id}")
    def get_profile(profile_id: str):
        return _wrap(store.get_profile, profile_id)

    @app.post("/profiles/{profile_id}/boot")
    def boot(profile_id: str):
        return _wrap(store.boot, profile_id)

    @app.post("/profiles/{profile_id}/memories", status_code=201)
    def remember(profile_id: str, event: MemoryEventIn):
        return _wrap(store.remember, profile_id, event.model_dump())

    @app.get("/profiles/{profile_id}/memories/search")
    def search(profile_id: str, q: str, limit: int = 20):
        return _wrap(store.search, profile_id, q, limit)

    @app.post("/profiles/{profile_id}/closeout", status_code=201)
    def closeout(profile_id: str, body: CloseoutIn):
        return _wrap(store.closeout, profile_id, body.notes, body.new_state)

    @app.get("/profiles/{profile_id}/domain")
    def list_stores(profile_id: str):
        return _wrap(store.list_domain_stores, profile_id)

    @app.get("/profiles/{profile_id}/domain/{store_name}")
    def query_domain(profile_id: str, store_name: str,
                     contains: str | None = None, limit: int = 50):
        return _wrap(store.query_domain, profile_id, store_name, contains, limit)

    @app.post("/profiles/{profile_id}/domain/{store_name}", status_code=201)
    def add_domain(profile_id: str, store_name: str, body: DomainRecordIn):
        return _wrap(store.add_domain_record, profile_id, store_name, body.data)

    # -- dynamic stores (slice two) ------------------------------------------
    # NOTE: approve/reject/archive are ADMIN/USER operations. There is no auth
    # yet, so they are NOT secure — real per-profile API keys plus an admin key
    # will be enforced by middleware here in a later slice (see ARCHITECTURE.md).

    @app.post("/profiles/{profile_id}/stores", status_code=201)
    def propose_store(profile_id: str, body: StoreProposalIn):
        return _wrap(dyn.propose, profile_id, body.name, body.purpose,
                     body.proposed_by, body.schema_def)

    @app.get("/profiles/{profile_id}/stores")
    def list_stores(profile_id: str):
        return _wrap(dyn.list, profile_id)

    @app.get("/profiles/{profile_id}/stores/{name}")
    def get_store(profile_id: str, name: str):
        return _wrap(dyn.get, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/approve")
    def approve_store(profile_id: str, name: str):
        return _wrap(dyn.approve, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/reject")
    def reject_store(profile_id: str, name: str, body: RejectIn):
        return _wrap(dyn.reject, profile_id, name, body.reason)

    @app.post("/profiles/{profile_id}/stores/{name}/archive")
    def archive_store(profile_id: str, name: str):
        return _wrap(dyn.archive, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/records", status_code=201)
    def add_store_record(profile_id: str, name: str, body: DomainRecordIn):
        return _wrap(dyn.add_record, profile_id, name, body.data)

    @app.get("/profiles/{profile_id}/stores/{name}/records")
    def query_store_records(profile_id: str, name: str,
                            contains: str | None = None, limit: int = 50):
        return _wrap(dyn.query_records, profile_id, name, contains, limit)

    @app.get("/profiles/{profile_id}/stores/{name}/audit")
    def store_audit(profile_id: str, name: str, limit: int = 100):
        return _wrap(dyn.audit_events, profile_id, name, limit)

    @app.get("/profiles/{profile_id}/audit")
    def profile_audit(profile_id: str, limit: int = 100):
        return _wrap(dyn.audit_events, profile_id, None, limit)

    return app


app = create_app()
