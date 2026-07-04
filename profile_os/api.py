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
from .errors import MalformedMemoryEvent, MalformedRecord, ProfileNotFound
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


def create_app(data_dir: str = DATA_DIR, do_seed: bool = True) -> FastAPI:
    app = FastAPI(title="Assistant Profile OS", version="0.1.0")
    store = Store(data_dir)
    if do_seed:
        seed.seed(store)
    app.state.store = store

    def _wrap(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ProfileNotFound as e:
            raise HTTPException(404, str(e))
        except (MalformedMemoryEvent, MalformedRecord) as e:
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

    return app


app = create_app()
