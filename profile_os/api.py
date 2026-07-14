"""HTTP API for the Assistant Profile OS (slice zero).

HTTP was chosen over MCP for slice zero: it is testable with plain httpx,
serves the future web/mobile UI directly, and an MCP adapter can wrap these
same service calls later (see ARCHITECTURE.md).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import seed
from .access import AccessControl, AccessError
from .dynstores import DynamicStores
from .projects import Projects
from .enroll import Enrollment, InviteConsumed, InviteInvalid
from .errors import (DynStoreConflict, DynStoreNotFound, FileNotFoundInStore,
                     MalformedMemoryEvent, MalformedMessage, MalformedRecord,
                     MemoryEventNotFound, MessageNotFound, ProfileNotFound, SchemaError)
from .sign import signing_message
from .storage import Store

DATA_DIR = os.environ.get("PROFILE_OS_DATA_DIR", "data")

SIGNATURE_SKEW_SECONDS = 120
_SIG_RE = re.compile(
    r'key_id=(?P<key_id>[^,]+),ts=(?P<ts>\d+),nonce=(?P<nonce>[0-9a-fA-F]+),sig=(?P<sig>[A-Za-z0-9+/=]+)')

# Owner grants auto-issued to the creating principal on POST /profiles.
OWNER_OPS = ["boot", "remember", "search", "closeout", "records:read",
            "records:write", "stores:propose", "manage_profile"]

AUTO_STORE_LIMIT = int(os.environ.get("PROFILE_OS_AUTO_STORE_LIMIT", "3"))
AUTO_STORE_MAX_FIELDS = int(os.environ.get("PROFILE_OS_AUTO_STORE_MAX_FIELDS", "12"))
MAX_PROFILES_PER_PRINCIPAL = int(os.environ.get("PROFILE_OS_MAX_PROFILES_PER_PRINCIPAL", "10"))

PROFILE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class MemoryEventIn(BaseModel):
    kind: str
    content: str
    tags: list[str] = Field(default_factory=list)


class MemoryEventUpdateIn(BaseModel):
    kind: str | None = None
    content: str | None = None
    tags: list[str] | None = None


class MessageIn(BaseModel):
    to_profile_id: str
    content: str


class ProfileCreateTotpIn(BaseModel):
    id: str
    display_name: str
    base_prompt: str = ""
    role_prompt: str = ""
    allowed_tools: list[str] | None = None
    totp_code: str


class FileWriteIn(BaseModel):
    content: str


class CloseoutIn(BaseModel):
    facts: str
    texture: str
    exchange: str
    notes: str = ""


class DomainRecordIn(BaseModel):
    data: dict


class BulkRecordsIn(BaseModel):
    records: list[dict]


class RecordPatchIn(BaseModel):
    patch: dict


class RecordQueryIn(BaseModel):
    where: dict = Field(default_factory=dict)
    fields: list[str] | None = None
    order_by: str | None = None
    descending: bool = True
    limit: int = 50


class PendingStoreUpdateIn(BaseModel):
    purpose: str
    schema_def: dict = Field(alias="schema")


class StoreProposalIn(BaseModel):
    name: str
    purpose: str
    proposed_by: str
    schema_def: dict = Field(alias="schema")


class RejectIn(BaseModel):
    reason: str


class ProfileCreateIn(BaseModel):
    id: str
    display_name: str
    base_prompt: str = ""
    role_prompt: str = ""


class EnrollIn(BaseModel):
    invite_token: str
    display_name: str
    public_key: str


class PromptEditProposeIn(BaseModel):
    base_prompt: str | None = None
    role_prompt: str | None = None


class DescriptionIn(BaseModel):
    description: str


class ApprovalDecideIn(BaseModel):
    approve: bool
    totp_code: str | None = None


class AdminVerifyIn(BaseModel):
    secret: str
    totp_code: str


class SessionInspectIn(BaseModel):
    """A live TOTP gate for the read-only companion session inspector."""
    totp_code: str


class ProjectCreateIn(BaseModel):
    name: str
    purpose: str
    schema_def: dict = Field(alias="schema")


class ProjectJoinIn(BaseModel):
    profile_id: str


def create_app(data_dir: str = DATA_DIR, do_seed: bool = True,
               auth_enabled: bool | None = None,
               identity_file: str | None = None) -> FastAPI:
    if auth_enabled is None:
        auth_enabled = os.environ.get("PROFILE_OS_AUTH_ENABLED") == "1"
    if identity_file is None:
        identity_file = os.environ.get("PROFILE_OS_IDENTITY_FILE")
    app = FastAPI(title="Assistant Profile OS", version="0.1.0")
    store = Store(data_dir)
    if do_seed:
        seed.seed(store)
    app.state.store = store

    dyn = DynamicStores(store)
    app.state.dynstores = dyn
    projects = Projects(store)
    app.state.projects = projects
    access = AccessControl(store)
    app.state.access = access
    enrollment = Enrollment(access)
    app.state.enrollment = enrollment

    _nonce_cache: dict[tuple[str, str], float] = {}
    _enroll_hits: dict[str, list[float]] = {}
    _verify_hits: dict[str, list[float]] = {}
    _profile_totp_hits: dict[str, list[float]] = {}
    _settings_hits: dict[str, list[float]] = {}
    _settings_session_key = secrets.token_bytes(32)
    _settings_session_seconds = 15 * 60

    def _rate_limited(bucket: dict[str, list[float]], key: str,
                      limit: int = 5, window: float = 60) -> bool:
        now = time.time()
        hits = [t for t in bucket.get(key, []) if t > now - window]
        hits.append(now)
        bucket[key] = hits
        return len(hits) > limit

    def _settings_session_token() -> str:
        expires_at = str(int(time.time()) + _settings_session_seconds)
        signature = hmac.new(_settings_session_key, expires_at.encode(),
                             hashlib.sha256).hexdigest()
        return f"{expires_at}.{signature}"

    def _has_settings_session(request: Request) -> bool:
        token = request.cookies.get("profile_os_settings", "")
        try:
            expires_at, signature = token.split(".", 1)
            expected = hmac.new(_settings_session_key, expires_at.encode(),
                                hashlib.sha256).hexdigest()
            return int(expires_at) >= time.time() and hmac.compare_digest(
                signature, expected)
        except (TypeError, ValueError):
            return False

    @app.middleware("http")
    async def _buffer_body(request: Request, call_next):
        """Read the body once and replay it so both signature auth and
        pydantic parsing see the same bytes (needed for sha256(body))."""
        body = await request.body()
        request.state.raw_body = body

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive
        return await call_next(request)

    def _check_signature(request: Request, authorization: str) -> str | None:
        m = _SIG_RE.match(authorization[len("Signature "):])
        if not m:
            raise HTTPException(401, "malformed signature header",
                                headers={"WWW-Authenticate": "Signature"})
        key_id, ts, nonce, sig_b64 = m["key_id"], m["ts"], m["nonce"], m["sig"]
        now = time.time()
        if abs(now - int(ts)) > SIGNATURE_SKEW_SECONDS:
            raise HTTPException(401, "signature timestamp outside allowed skew",
                                headers={"WWW-Authenticate": "Signature"})
        cache_key = (key_id, nonce)
        if cache_key in _nonce_cache:
            raise HTTPException(401, "replayed nonce",
                                headers={"WWW-Authenticate": "Signature"})
        try:
            signature = base64.b64decode(sig_b64, validate=True)
        except Exception:
            raise HTTPException(401, "malformed signature",
                                headers={"WWW-Authenticate": "Signature"})
        message = signing_message(ts, nonce, request.method,
                                  request.url.path, request.state.raw_body)
        principal = access.authenticate_signature(key_id, message, signature)
        if principal is None:
            raise HTTPException(401, "invalid, expired, or revoked key",
                                headers={"WWW-Authenticate": "Signature"})
        _nonce_cache[cache_key] = now
        # opportunistic cleanup so the in-memory cache doesn't grow unbounded
        stale = [k for k, seen in _nonce_cache.items()
                if seen < now - 2 * SIGNATURE_SKEW_SECONDS]
        for k in stale:
            _nonce_cache.pop(k, None)
        return principal["id"]

    def _authenticate(request: Request) -> str | None:
        """Resolve the bearer/signature credential to a principal id, or 401.

        Returns None only when auth is disabled (the local default). The
        credential belongs to a principal/client (see ACCESS_CONTROL.md),
        never to a profile.
        """
        if not auth_enabled:
            return None
        authorization = request.headers.get("authorization")
        if not authorization:
            raise HTTPException(401, "missing credential",
                                headers={"WWW-Authenticate": "Bearer"})
        if authorization.startswith("Bearer "):
            principal_id = access.authenticate_secret(authorization[len("Bearer "):])
            if principal_id is None:
                raise HTTPException(401, "invalid, expired, or revoked credential",
                                    headers={"WWW-Authenticate": "Bearer"})
            return principal_id
        if authorization.startswith("Signature "):
            return _check_signature(request, authorization)
        raise HTTPException(401, "unsupported authorization scheme",
                            headers={"WWW-Authenticate": "Bearer"})

    def _require(operation: str, profile_id: str, request: Request) -> str | None:
        """Missing/bad credential → 401; authenticated but ungranted → 403.

        No-op while auth is disabled. Returns the principal id (or None with
        auth disabled) so callers needing the caller's identity don't have
        to authenticate twice.
        """
        principal_id = _authenticate(request)
        if principal_id is None:
            return None
        if not access.allowed(principal_id, operation, profile_id):
            raise HTTPException(403, f"principal lacks {operation} on {profile_id!r}")
        return principal_id

    def _require_global(operation: str, request: Request) -> str | None:
        principal_id = _authenticate(request)
        if principal_id is None:
            return None
        if not access.allowed(principal_id, operation, None):
            raise HTTPException(403, f"principal lacks global {operation}")
        return principal_id

    def _require_global_any(operations: list[str], request: Request) -> str | None:
        principal_id = _authenticate(request)
        if principal_id is None:
            return None
        if not any(access.allowed(principal_id, op, None) for op in operations):
            raise HTTPException(403, f"principal lacks any of global {operations}")
        return principal_id

    def _wrap(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ProfileNotFound, DynStoreNotFound, MemoryEventNotFound,
                MessageNotFound, FileNotFoundInStore) as e:
            raise HTTPException(404, str(e))
        except DynStoreConflict as e:
            raise HTTPException(409, str(e))
        except (MalformedMemoryEvent, MalformedRecord, SchemaError, MalformedMessage) as e:
            raise HTTPException(422, str(e))

    def _expire_approvals() -> None:
        for approval in access.expire_pending_approvals():
            if approval["kind"] == "store_schema":
                store_id = approval["payload"].get("store_id")
                if store_id:
                    try:
                        dyn.reject_id(store_id, "approval expired after 24 hours", actor="system")
                    except (DynStoreConflict, DynStoreNotFound):
                        pass
            elif approval["kind"] == "project_create":
                project_id = approval["payload"].get("project_id")
                if project_id:
                    try:
                        projects.reject_create(project_id)
                    except (DynStoreConflict, DynStoreNotFound):
                        pass

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def root_directory():
        """Keep the root useful without restoring a conversation surface."""
        return RedirectResponse(url="/directory", status_code=307)

    @app.get("/directory", response_class=HTMLResponse,
             include_in_schema=False)
    def directory():
        """Small human-facing index of administrative entry points."""
        return (Path(__file__).parent / "directory.html").read_text()

    @app.get("/companions/new", include_in_schema=False)
    def new_companion():
        """Phone-friendly companion creation, protected by an admin TOTP."""
        return HTMLResponse(
            (Path(__file__).parent / "new_companion.html").read_text())

    @app.get("/settings", response_class=HTMLResponse,
             include_in_schema=False)
    def settings(request: Request):
        """TOTP-gated entry point for the local administration console."""
        page = "demo.html" if _has_settings_session(request) else "settings_login.html"
        return (Path(__file__).parent / page).read_text()

    @app.post("/settings/unlock", include_in_schema=False)
    async def unlock_settings(request: Request):
        """Verify a live admin TOTP code and issue a brief secure session."""
        client_ip = request.client.host if request.client else "unknown"
        if _rate_limited(_settings_hits, client_ip):
            raise HTTPException(429, "too many attempts; try again later")
        form = await request.form()
        admin_id = access.find_totp_admin_principal_id()
        if admin_id is None or not access.verify_totp(
                admin_id, str(form.get("totp_code") or "")):
            return HTMLResponse(
                (Path(__file__).parent / "settings_login.html").read_text()
                .replace("<!-- ERROR -->", "<p class=\"error\">Invalid or expired authenticator code.</p>"),
                status_code=401)
        response = RedirectResponse(url="/settings", status_code=303)
        response.set_cookie("profile_os_settings", _settings_session_token(),
                            max_age=_settings_session_seconds, httponly=True,
                            secure=True, samesite="strict", path="/settings")
        return response

    @app.get("/identity")
    def identity(request: Request):
        _require_global("identity:read", request)
        if not identity_file:
            raise HTTPException(404, "no identity file configured")
        path = Path(identity_file)
        if not path.is_file():
            raise HTTPException(404, "identity file not found")
        return {"content": path.read_text()}

    @app.get("/demo", response_class=HTMLResponse)
    def demo():
        """Legacy settings URL; the TOTP gate lives at /settings."""
        return RedirectResponse(url="/settings", status_code=307)

    @app.get("/profiles")
    def list_profiles(request: Request):
        principal_id = _authenticate(request)
        profiles = store.list_profiles()
        if principal_id is None:
            return profiles
        visible = access.visible_profile_ids(principal_id)
        if visible is None:  # wildcard grant
            return profiles
        return [p for p in profiles if p["id"] in visible]

    @app.post("/profiles", status_code=201)
    def create_profile(body: ProfileCreateIn, request: Request):
        principal_id = _require_global("create_profile", request)
        if not PROFILE_ID_RE.match(body.id):
            raise HTTPException(422, "id must match [a-z0-9_-]{1,64}")
        existing = {p["id"] for p in store.list_profiles()}
        if body.id in existing:
            raise HTTPException(409, f"profile {body.id!r} already exists")
        if principal_id is not None:
            count = access.db.execute(
                "SELECT COUNT(*) c FROM access_grants WHERE principal_id=?"
                " AND operation='manage_profile' AND revoked_at IS NULL",
                (principal_id,)).fetchone()["c"]
            if count >= MAX_PROFILES_PER_PRINCIPAL:
                raise HTTPException(
                    403, f"principal already owns {count} profiles"
                        f" (limit {MAX_PROFILES_PER_PRINCIPAL})")
        profile = store.create_profile(
            body.id, body.display_name, body.base_prompt, body.role_prompt)
        if principal_id is not None:
            for op in OWNER_OPS:
                access.grant(principal_id, op, profile_id=body.id)
            access.record_audit(principal_id, "create_profile", body.id)
        return profile

    @app.post("/profiles/totp-create", status_code=201)
    def create_profile_totp(body: ProfileCreateTotpIn, request: Request):
        """Create a profile with a live TOTP code alone — no admin secret,
        no SSH. For creating/migrating a companion from mobile, where the
        admin secret deliberately isn't carried. Public at the transport
        layer (no bearer header) — the credential being checked is the
        totp_code itself, same principle as /admin/verify-totp. Owner
        grants aren't needed here: the found admin already covers every
        profile via its wildcard-scoped grants."""
        client_ip = request.client.host if request.client else "unknown"
        if _rate_limited(_profile_totp_hits, client_ip):
            raise HTTPException(429, "too many attempts; try again later")
        admin_id = access.find_totp_admin_principal_id()
        if admin_id is None:
            raise HTTPException(403, "no TOTP-enrolled admin found")
        if not access.verify_totp(admin_id, body.totp_code):
            raise HTTPException(401, "missing or invalid TOTP code")
        if not PROFILE_ID_RE.match(body.id):
            raise HTTPException(422, "id must match [a-z0-9_-]{1,64}")
        if body.id in {p["id"] for p in store.list_profiles()}:
            raise HTTPException(409, f"profile {body.id!r} already exists")
        profile = store.create_profile(
            body.id, body.display_name, body.base_prompt, body.role_prompt,
            allowed_tools=body.allowed_tools)
        access.record_audit(admin_id, "create_profile_totp", body.id)
        return profile

    @app.post("/enroll", status_code=201)
    def enroll(body: EnrollIn, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if _rate_limited(_enroll_hits, client_ip):
            raise HTTPException(429, "too many enrollment attempts; try again later")
        try:
            return enrollment.enroll(body.invite_token, body.display_name,
                                     body.public_key)
        except InviteConsumed as e:
            raise HTTPException(410, str(e))
        except InviteInvalid as e:
            raise HTTPException(401, str(e))

    @app.post("/admin/verify-totp")
    def admin_verify_totp(body: AdminVerifyIn, request: Request):
        """Login check for the MCP OAuth consent screen: does this secret +
        live TOTP code belong to a principal holding approvals:decide? This
        route is intentionally public at the transport layer (no bearer
        header) — the credential being checked IS the request body, exactly
        like any login form. Rate-limited against brute force."""
        client_ip = request.client.host if request.client else "unknown"
        if _rate_limited(_verify_hits, client_ip):
            raise HTTPException(429, "too many attempts; try again later")
        principal_id = access.authenticate_secret(body.secret)
        if principal_id is None:
            raise HTTPException(401, "invalid secret")
        if not access.allowed(principal_id, "approvals:decide", None):
            raise HTTPException(403, "principal lacks approvals:decide")
        if not access.verify_totp(principal_id, body.totp_code):
            raise HTTPException(401, "invalid, missing, or reused TOTP code")
        return {"ok": True, "principal_id": principal_id}

    @app.get("/profiles/{profile_id}")
    def get_profile(profile_id: str, request: Request):
        _require("boot", profile_id, request)
        return _wrap(store.get_profile, profile_id)

    @app.delete("/profiles/{profile_id}", status_code=204)
    def delete_profile(profile_id: str, request: Request):
        _require("delete_profile", profile_id, request)
        _wrap(store.get_profile, profile_id)  # 404 if unknown, before any deletion
        dyn.delete_profile_data(profile_id)
        store.delete_profile(profile_id)
        with access.db:
            access.db.execute(
                "UPDATE access_grants SET revoked_at=? WHERE profile_id=? AND revoked_at IS NULL",
                (time.time(), profile_id))

    @app.post("/profiles/{profile_id}/boot")
    def boot(profile_id: str, request: Request):
        _require("boot", profile_id, request)
        return _wrap(store.boot, profile_id)

    @app.post("/profiles/{profile_id}/session")
    def start_session(profile_id: str, request: Request):
        """One-call model hydration packet for a companion's first turn.

        It carries semantic context, not database records: the current
        handoff and bounded boot-memory slice, with each memory reduced to
        kind/content. Full history, IDs, tags, timestamps, and closeout
        archives stay on their dedicated tools.
        """
        principal_id = _require("boot", profile_id, request)
        booted = _wrap(store.boot, profile_id)
        profile = booted["profile"]
        booted["profile"] = {
            key: profile[key] for key in
            ("id", "display_name", "description", "allowed_tools", "memory_policy", "closeout_rules")
            if key in profile
        }
        booted.pop("state_updated_at", None)
        hydrated_memories = [
            {"kind": event["kind"], "content": event["content"]}
            for event in booted.pop("recent_memories", [])
        ]
        identity_content = None
        if principal_id is None or access.allowed(principal_id, "identity:read", None):
            if identity_file:
                path = Path(identity_file)
                if path.is_file():
                    identity_content = path.read_text()
        now = time.time()
        return {
            **booted,
            "identity": identity_content,
            "memories": hydrated_memories,
            # Deliberately only a flag: inbox contents stay behind read_inbox,
            # but a newly hydrated companion knows when to check it.
            "you_got_mail": bool(_wrap(store.list_inbox, profile_id, True, 1)),
            "server_time": {
                "unix": now,
                "iso": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            },
        }

    @app.post("/profiles/{profile_id}/session-inspect")
    def inspect_session(profile_id: str, body: SessionInspectIn, request: Request):
        """Return a session payload only after a live administrator TOTP check.

        This is intentionally separate from the normal companion-facing
        ``start_session`` endpoint: the public MCP inspection page uses the
        bridge's global ``approvals:totp_decide`` grant plus a one-time code,
        and never receives a reusable admin credential.
        """
        principal_id = _require_global_any(["approvals:decide", "approvals:totp_decide"], request)
        if principal_id is not None:
            totp_principal = (
                principal_id if access.allowed(principal_id, "approvals:decide", None)
                else access.find_totp_admin_principal_id()
            )
            if totp_principal is None or not access.has_totp(totp_principal):
                raise HTTPException(403, "no TOTP enrolled; run python -m profile_os.enroll_totp")
            if not access.verify_totp(totp_principal, body.totp_code):
                raise HTTPException(401, "missing or invalid TOTP code")

        # Keep the returned shape exactly equal to start_session, so the raw
        # inspector view is the payload an MCP client actually receives.
        return start_session(profile_id, request)

    @app.post("/profiles/{profile_id}/memories", status_code=201)
    def remember(profile_id: str, event: MemoryEventIn, request: Request):
        _require("remember", profile_id, request)
        return _wrap(store.remember, profile_id, event.model_dump())

    @app.patch("/profiles/{profile_id}/memories/{event_id}")
    def update_memory(profile_id: str, event_id: str, body: MemoryEventUpdateIn,
                      request: Request):
        """Self-service: a companion may revise its own memory events. Same
        grant as writing one (remember) — not a backend/admin action."""
        _require("remember", profile_id, request)
        if body.kind is None and body.content is None and body.tags is None:
            raise HTTPException(422, "at least one of kind/content/tags is required")
        return _wrap(store.update_memory, profile_id, event_id,
                    body.kind, body.content, body.tags)

    @app.delete("/profiles/{profile_id}/memories/{event_id}", status_code=204)
    def delete_memory(profile_id: str, event_id: str, request: Request):
        """Self-service: a companion may erase its own memory events."""
        _require("remember", profile_id, request)
        _wrap(store.delete_memory, profile_id, event_id)

    @app.get("/profiles/{profile_id}/memories/search")
    def search(profile_id: str, q: str, request: Request, limit: int = 20):
        _require("search", profile_id, request)
        return _wrap(store.search, profile_id, q, limit)

    # -- inbox: companion-to-companion messages, no approval, no backend action ----

    @app.post("/profiles/{from_profile_id}/messages", status_code=201)
    def send_message(from_profile_id: str, body: MessageIn, request: Request):
        _require("remember", from_profile_id, request)
        return _wrap(store.send_message, from_profile_id, body.to_profile_id, body.content)

    @app.get("/profiles/{profile_id}/inbox")
    def get_inbox(profile_id: str, request: Request,
                 unread_only: bool = False, limit: int = 50):
        _require("search", profile_id, request)
        return _wrap(store.list_inbox, profile_id, unread_only, limit)

    @app.post("/profiles/{profile_id}/inbox/{message_id}/read")
    def mark_message_read(profile_id: str, message_id: str, request: Request):
        _require("search", profile_id, request)
        return _wrap(store.mark_message_read, profile_id, message_id)

    # -- file store: plain files on disk, self-service, never git/DB blob --------

    @app.put("/profiles/{profile_id}/files/{filename}", status_code=201)
    def write_file(profile_id: str, filename: str, body: FileWriteIn, request: Request):
        _require("remember", profile_id, request)
        return _wrap(store.write_file, profile_id, filename, body.content)

    @app.get("/profiles/{profile_id}/files")
    def list_files(profile_id: str, request: Request):
        _require("search", profile_id, request)
        return _wrap(store.list_files, profile_id)

    @app.get("/profiles/{profile_id}/files/{filename}")
    def read_file(profile_id: str, filename: str, request: Request):
        _require("search", profile_id, request)
        return _wrap(store.read_file, profile_id, filename)

    @app.delete("/profiles/{profile_id}/files/{filename}", status_code=204)
    def delete_file(profile_id: str, filename: str, request: Request):
        _require("remember", profile_id, request)
        _wrap(store.delete_file, profile_id, filename)

    @app.post("/profiles/{profile_id}/closeout", status_code=201)
    def closeout(profile_id: str, body: CloseoutIn, request: Request):
        _require("closeout", profile_id, request)
        return _wrap(store.closeout, profile_id, body.facts, body.texture,
                     body.exchange, body.notes)

    # -- shared projects ------------------------------------------------------

    @app.post("/profiles/{profile_id}/projects", status_code=201)
    def propose_project(profile_id: str, body: ProjectCreateIn, request: Request):
        principal_id = _require("manage_profile", profile_id, request)
        project = _wrap(projects.propose_create, profile_id, body.name,
                        body.purpose, body.schema_def)
        approval = access.propose_approval(
            "project_create", principal_id or profile_id,
            {"project_id": project["id"], "name": project["name"],
             "purpose": project["purpose"], "schema": project["schema"]},
            profile_id=profile_id)
        return {**project, "approval_id": approval["id"]}

    @app.get("/profiles/{profile_id}/projects")
    def list_projects(profile_id: str, request: Request, available: bool = False):
        _require("records:read", profile_id, request)
        fn = projects.list_available if available else projects.list_for
        return _wrap(fn, profile_id)

    @app.post("/projects/{project_id}/join", status_code=201)
    def propose_project_join(project_id: str, body: ProjectJoinIn, request: Request):
        principal_id = _require("manage_profile", body.profile_id, request)
        project = _wrap(projects.request_join, body.profile_id, project_id)
        approval = access.propose_approval(
            "project_join", principal_id or body.profile_id,
            {"project_id": project_id, "project_name": project["name"],
             "joining_profile_id": body.profile_id}, profile_id=body.profile_id)
        return {**project, "approval_id": approval["id"]}

    @app.delete("/projects/{project_id}/members/{profile_id}")
    def leave_project(project_id: str, profile_id: str, request: Request):
        _require("manage_profile", profile_id, request)
        return _wrap(projects.leave, profile_id, project_id)

    @app.get("/projects/{project_id}/records")
    def query_project_records(project_id: str, profile_id: str, request: Request,
                              contains: str | None = None, limit: int = 50):
        _require("records:read", profile_id, request)
        return _wrap(projects.query, profile_id, project_id, contains, limit)

    @app.post("/projects/{project_id}/records", status_code=201)
    def add_project_record(project_id: str, profile_id: str, body: DomainRecordIn,
                           request: Request):
        _require("records:write", profile_id, request)
        return _wrap(projects.add_record, profile_id, project_id, body.data)

    @app.get("/settings/notifications")
    def settings_notifications(request: Request, include_silenced: bool = False):
        if not _has_settings_session(request):
            raise HTTPException(401, "settings are locked")
        return projects.notifications(include_silenced)

    @app.post("/settings/notifications/{notification_id}/silence")
    def silence_notification(notification_id: str, request: Request):
        if not _has_settings_session(request):
            raise HTTPException(401, "settings are locked")
        return _wrap(projects.silence, notification_id)

    @app.delete("/settings/projects/{project_id}")
    def delete_empty_project(project_id: str, request: Request):
        if not _has_settings_session(request):
            raise HTTPException(401, "settings are locked")
        return _wrap(projects.delete, project_id)

    # -- TOTP-gated approvals ("edgy" actions) --------------------------------
    # Routine writes (remember, closeout, records) never need a code. Only
    # actions a companion shouldn't be able to do unilaterally — starting
    # with editing its own prompts — go through propose -> pending -> a
    # human decision, and approving (not rejecting) requires a live TOTP
    # code. See ACCESS_CONTROL.md "TOTP-gated approvals".

    @app.post("/profiles/{profile_id}/prompt", status_code=201)
    def propose_prompt_edit(profile_id: str, body: PromptEditProposeIn, request: Request):
        principal_id = _require("manage_profile", profile_id, request)
        _wrap(store.get_profile, profile_id)  # 404 if unknown
        if body.base_prompt is None and body.role_prompt is None:
            raise HTTPException(422, "at least one of base_prompt/role_prompt is required")
        return access.propose_approval(
            "prompt_edit", principal_id or "anonymous",
            {"base_prompt": body.base_prompt, "role_prompt": body.role_prompt},
            profile_id=profile_id)

    @app.post("/approvals/{approval_id}/retract")
    def retract_approval(approval_id: str, request: Request):
        """A companion may retract only its own still-pending proposal."""
        _expire_approvals()
        principal_id = _authenticate(request)
        try:
            approval = access.retract_approval(approval_id, principal_id)
        except AccessError as e:
            raise HTTPException(409, str(e))
        if approval["kind"] == "store_schema":
            store_id = approval["payload"].get("store_id")
            if store_id:
                _wrap(dyn.reject_id, store_id, "withdrawn by proposer", actor=principal_id or "anonymous")
        elif approval["kind"] == "project_create":
            project_id = approval["payload"].get("project_id")
            if project_id:
                _wrap(projects.reject_create, project_id)
        return approval

    @app.put("/profiles/{profile_id}/description")
    def update_description(profile_id: str, body: DescriptionIn, request: Request):
        """Self-service, no approval — unlike prompt edits, this is discovery
        metadata ('what do I do'), not behavior. Lets other companions find
        who to ask via list_profiles instead of a human hardcoding names
        into prompts."""
        _require("manage_profile", profile_id, request)
        return _wrap(store.update_description, profile_id, body.description)

    _APPROVAL_OPS = ["approvals:decide", "approvals:totp_decide"]

    @app.get("/approvals")
    def list_approvals(request: Request, profile_id: str | None = None):
        _expire_approvals()
        _require_global_any(_APPROVAL_OPS, request)
        return access.list_pending_approvals(profile_id)

    @app.get("/approvals/{approval_id}")
    def get_approval_route(approval_id: str, request: Request):
        _expire_approvals()
        _require_global_any(_APPROVAL_OPS, request)
        try:
            return access.get_approval(approval_id)
        except AccessError as e:
            raise HTTPException(404, str(e))

    @app.post("/approvals/{approval_id}/decide")
    def decide_approval(approval_id: str, body: ApprovalDecideIn, request: Request):
        _expire_approvals()
        principal_id = _require_global_any(_APPROVAL_OPS, request)
        try:
            row = access.get_approval(approval_id)
        except AccessError as e:
            raise HTTPException(404, str(e))
        if row["status"] != "pending":
            raise HTTPException(409, f"approval already {row['status']}")
        if body.approve:
            if principal_id is None:
                pass  # auth disabled: local/dev convenience, no TOTP surface
            else:
                # A caller holding full approvals:decide (e.g. the admin's own
                # bearer) verifies against their own TOTP, exactly as before.
                # A caller with only approvals:totp_decide (the mcp service,
                # acting on a companion's behalf via a public approval link)
                # verifies against the single TOTP-enrolled admin instead —
                # there is exactly one admin, so this is unambiguous, and the
                # live single-use code is still the actual gate either way.
                totp_principal = (principal_id
                                 if access.allowed(principal_id, "approvals:decide", None)
                                 else access.find_totp_admin_principal_id())
                if totp_principal is None or not access.has_totp(totp_principal):
                    raise HTTPException(
                        403, "no TOTP enrolled; run python -m profile_os.enroll_totp")
                if not access.verify_totp(totp_principal, body.totp_code or ""):
                    raise HTTPException(401, "missing or invalid TOTP code")
        if row["kind"] == "store_schema":
            payload = row["payload"]
            store_id = payload.get("store_id")
            if not store_id:
                raise HTTPException(422, "store approval payload missing store_id")
            actor = f"approval:{principal_id or 'anonymous'}"
            if body.approve:
                _wrap(dyn.approve_id, store_id, actor=actor)
            else:
                _wrap(dyn.reject_id, store_id, "rejected via approval link", actor=actor)
        elif row["kind"] == "project_create":
            project_id = row["payload"].get("project_id")
            if body.approve:
                _wrap(projects.approve_create, project_id)
            else:
                _wrap(projects.reject_create, project_id)
        elif row["kind"] == "project_join" and body.approve:
            _wrap(projects.approve_join,
                  row["payload"].get("joining_profile_id"),
                  row["payload"].get("project_id"))
        try:
            decided = access.decide_approval(approval_id, body.approve,
                                            principal_id or "anonymous")
        except AccessError as e:
            raise HTTPException(409, str(e))
        if body.approve and decided["kind"] == "prompt_edit":
            payload = decided["payload"]
            _wrap(store.update_prompts, decided["profile_id"],
                 payload.get("base_prompt"), payload.get("role_prompt"))
        return decided

    @app.get("/profiles/{profile_id}/domain")
    def list_stores(profile_id: str, request: Request):
        _require("records:read", profile_id, request)
        return _wrap(store.list_domain_stores, profile_id)

    @app.get("/profiles/{profile_id}/domain/{store_name}")
    def query_domain(profile_id: str, store_name: str, request: Request,
                     contains: str | None = None, limit: int = 50):
        _require("records:read", profile_id, request)
        return _wrap(store.query_domain, profile_id, store_name, contains, limit)

    @app.post("/profiles/{profile_id}/domain/{store_name}", status_code=201)
    def add_domain(profile_id: str, store_name: str, body: DomainRecordIn,
                   request: Request):
        _require("records:write", profile_id, request)
        return _wrap(store.add_domain_record, profile_id, store_name, body.data)

    # -- dynamic stores (slice two) ------------------------------------------
    # Every route is enforced via _require when PROFILE_OS_AUTH_ENABLED=1
    # (default: off, open on localhost). See ACCESS_CONTROL.md for the map.

    @app.post("/profiles/{profile_id}/stores", status_code=201)
    def propose_store(profile_id: str, body: StoreProposalIn, request: Request):
        _expire_approvals()
        principal_id = _require("stores:propose", profile_id, request)
        proposed = _wrap(dyn.propose, profile_id, body.name, body.purpose,
                         body.proposed_by, body.schema_def)
        result = _maybe_auto_approve(profile_id, body.name, principal_id, proposed)
        if result["status"] != "pending":
            return result
        approval = access.propose_approval(
            "store_schema",
            principal_id or body.proposed_by,
            {
                "store_id": result["id"],
                "profile_id": profile_id,
                "store_name": result["name"],
                "version": result["version"],
                "purpose": result["purpose"],
                "schema": result["schema"],
            },
            profile_id=profile_id,
        )
        return {**result, "approval_id": approval["id"]}

    @app.get("/profiles/{profile_id}/stores")
    def list_stores(profile_id: str, request: Request):
        _require("records:read", profile_id, request)
        return _wrap(dyn.list, profile_id)

    @app.get("/profiles/{profile_id}/stores/{name}")
    def get_store(profile_id: str, name: str, request: Request):
        _require("records:read", profile_id, request)
        return _wrap(dyn.get, profile_id, name)

    @app.patch("/profiles/{profile_id}/stores/{name}")
    def update_pending_store(profile_id: str, name: str, body: PendingStoreUpdateIn,
                             request: Request):
        principal_id = _require("stores:propose", profile_id, request)
        current = _wrap(dyn.get, profile_id, name)
        pending = next((a for a in access.list_pending_approvals(profile_id)
                        if a["kind"] == "store_schema" and a["payload"].get("store_id") == current["id"]), None)
        if pending is None or (principal_id is not None and pending["proposed_by_principal"] != principal_id):
            raise HTTPException(403, "only the proposing companion may modify this pending store")
        updated = _wrap(dyn.update_pending, profile_id, name, body.purpose,
                        body.schema_def, principal_id or "anonymous")
        # Retire the superseded approval and issue a fresh 24-hour decision link.
        for approval in access.list_pending_approvals(profile_id):
            if approval["kind"] == "store_schema" and approval["payload"].get("store_id") == updated["id"]:
                access.retract_approval(approval["id"], principal_id)
        approval = access.propose_approval("store_schema", principal_id or updated["proposed_by"],
            {"store_id": updated["id"], "profile_id": profile_id, "store_name": updated["name"],
             "version": updated["version"], "purpose": updated["purpose"], "schema": updated["schema"]},
            profile_id=profile_id)
        return {**updated, "approval_id": approval["id"]}

    @app.delete("/profiles/{profile_id}/stores/{name}")
    def withdraw_pending_store(profile_id: str, name: str, request: Request):
        principal_id = _require("stores:propose", profile_id, request)
        current = _wrap(dyn.get, profile_id, name)
        pending = next((a for a in access.list_pending_approvals(profile_id)
                        if a["kind"] == "store_schema" and a["payload"].get("store_id") == current["id"]), None)
        if pending is None or (principal_id is not None and pending["proposed_by_principal"] != principal_id):
            raise HTTPException(403, "only the proposing companion may withdraw this pending store")
        withdrawn = _wrap(dyn.withdraw, profile_id, name, principal_id or "anonymous")
        for approval in access.list_pending_approvals(profile_id):
            if approval["kind"] == "store_schema" and approval["payload"].get("store_id") == withdrawn["id"]:
                access.retract_approval(approval["id"], principal_id)
        return withdrawn

    @app.post("/profiles/{profile_id}/stores/{name}/approve")
    def approve_store(profile_id: str, name: str, request: Request):
        _require("stores:approve", profile_id, request)
        return _wrap(dyn.approve, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/reject")
    def reject_store(profile_id: str, name: str, body: RejectIn, request: Request):
        _require("stores:approve", profile_id, request)
        return _wrap(dyn.reject, profile_id, name, body.reason)

    @app.post("/profiles/{profile_id}/stores/{name}/archive")
    def archive_store(profile_id: str, name: str, request: Request):
        _require("stores:approve", profile_id, request)
        return _wrap(dyn.archive, profile_id, name)

    @app.post("/profiles/{profile_id}/stores/{name}/records", status_code=201)
    def add_store_record(profile_id: str, name: str, body: DomainRecordIn,
                         request: Request):
        _require("records:write", profile_id, request)
        return _wrap(dyn.add_record, profile_id, name, body.data)

    @app.post("/profiles/{profile_id}/stores/{name}/records/bulk", status_code=201)
    def bulk_add_store_records(profile_id: str, name: str, body: BulkRecordsIn,
                               request: Request):
        _require("records:write", profile_id, request)
        return _wrap(dyn.add_records, profile_id, name, body.records)

    @app.get("/profiles/{profile_id}/stores/{name}/records")
    def query_store_records(profile_id: str, name: str, request: Request,
                            contains: str | None = None, limit: int = 50):
        _require("records:read", profile_id, request)
        return _wrap(dyn.query_records, profile_id, name, contains, limit)

    @app.post("/profiles/{profile_id}/stores/{name}/records/query")
    def filter_store_records(profile_id: str, name: str, body: RecordQueryIn,
                             request: Request):
        _require("records:read", profile_id, request)
        return _wrap(dyn.filter_records, profile_id, name, body.where, body.fields,
                     body.order_by, body.descending, body.limit)

    @app.get("/profiles/{profile_id}/stores/{name}/records/{record_id}")
    def get_store_record(profile_id: str, name: str, record_id: str,
                         request: Request, fields: str | None = None):
        _require("records:read", profile_id, request)
        selected = [field for field in (fields or "").split(",") if field] or None
        return _wrap(dyn.get_record, profile_id, name, record_id, selected)

    @app.patch("/profiles/{profile_id}/stores/{name}/records/{record_id}")
    def update_store_record(profile_id: str, name: str, record_id: str,
                            body: RecordPatchIn, request: Request):
        _require("records:write", profile_id, request)
        return _wrap(dyn.update_record, profile_id, name, record_id, body.patch)

    @app.delete("/profiles/{profile_id}/stores/{name}/records/{record_id}")
    def delete_store_record(profile_id: str, name: str, record_id: str,
                            request: Request):
        _require("records:write", profile_id, request)
        return _wrap(dyn.delete_record, profile_id, name, record_id)

    @app.get("/profiles/{profile_id}/stores/{name}/audit")
    def store_audit(profile_id: str, name: str, request: Request, limit: int = 100):
        _require("audit:read", profile_id, request)
        return _wrap(dyn.audit_events, profile_id, name, limit)

    @app.get("/profiles/{profile_id}/audit")
    def profile_audit(profile_id: str, request: Request, limit: int = 100):
        _require("audit:read", profile_id, request)
        return _wrap(dyn.audit_events, profile_id, None, limit)

    def _maybe_auto_approve(profile_id: str, name: str,
                            principal_id: str | None, proposal: dict) -> dict:
        """Auto-approve a just-proposed store when the proposer owns the
        profile (manage_profile) and the proposal is within budget."""
        if principal_id is None:
            return proposal
        if not access.allowed(principal_id, "manage_profile", profile_id):
            return proposal
        approved_or_pending = sum(
            1 for s in dyn.list(profile_id) if s["status"] in ("pending", "approved")
            and s["name"] != name)
        field_count = len(proposal["schema"]["fields"])
        if approved_or_pending >= AUTO_STORE_LIMIT or field_count > AUTO_STORE_MAX_FIELDS:
            return proposal
        return dyn.approve(profile_id, name, actor=f"auto:{principal_id}")

    return app


app = create_app(do_seed=os.environ.get("PROFILE_OS_SEED_DEMO_PROFILES", "1") == "1")
