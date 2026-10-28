"""Authenticated, currently read-only Tamagochi API surface."""

from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .pet_devices import PetDeviceService
from .pet_conversations import PetTurnService, RuntimeUnavailable


class ConversationIn(BaseModel):
    profile_id: str
    conversation_id: str | None = None


class TurnIn(BaseModel):
    request_id: str
    text: str
    surface: str = "pet_text"


def create_pet_router(devices: PetDeviceService, turns: PetTurnService) -> APIRouter:
    router = APIRouter(prefix="/pet/v1")

    def device(request: Request) -> dict:
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing device bearer credential",
                                headers={"WWW-Authenticate": "Bearer"})
        identity = devices.authenticate(authorization[len("Bearer "):])
        if identity is None:
            raise HTTPException(401, "invalid, expired, or revoked device credential",
                                headers={"WWW-Authenticate": "Bearer"})
        request.state.pet_device = identity
        return identity

    @router.get("/capabilities")
    def capabilities(request: Request):
        identity = device(request)
        profiles = devices.granted_profiles(identity["principal_id"], identity["credential_id"])
        return {
            "version": "1",
            "profiles": profiles,
            "limits": {"text_bytes": 16384, "audio_bytes": 1048576},
            "text_turns": any(turns.runtime_supports(profile) for profile in profiles),
            "voice_turns": False,
        }

    def call(fn, *args):
        try: return fn(*args)
        except (PermissionError, LookupError): raise HTTPException(404, "not found")

    @router.post("/conversations", status_code=201)
    def create_conversation(body: ConversationIn, request: Request, response: Response):
        identity = device(request)
        try:
            result = turns.create_conversation(identity, body.profile_id, body.conversation_id)
        except (PermissionError, LookupError): raise HTTPException(404, "not found")
        response.status_code = 200 if body.conversation_id is not None else 201
        return result

    @router.post("/conversations/{conversation_id}/turns")
    def submit_turn(conversation_id: str, body: TurnIn, request: Request, response: Response):
        identity = device(request)
        try:
            result, created = turns.submit(identity, conversation_id, body.request_id, body.text, body.surface)
        except RuntimeUnavailable: raise HTTPException(503, {"error": "runtime_unavailable"})
        except LookupError: raise HTTPException(404, "not found")
        except PermissionError: raise HTTPException(404, "not found")
        except FileExistsError: raise HTTPException(409, "request_id payload conflict")
        except BlockingIOError: raise HTTPException(409, "conversation has an active turn or queue is full")
        except OverflowError: raise HTTPException(413, "text exceeds 16 KiB UTF-8 limit")
        except ValueError as exc: raise HTTPException(422, str(exc))
        response.status_code = 202 if created else 200
        return result

    @router.get("/requests/{request_id}")
    def get_request(request_id: str, request: Request):
        result = call(turns.get_request, device(request), request_id)
        if result["stage"] == "unknown":
            return JSONResponse(status_code=503, content={"outcome": "unknown", "turn": result})
        return result

    @router.get("/turns/{turn_id}")
    def get_turn(turn_id: str, request: Request):
        return call(turns.get_turn, device(request), turn_id)

    @router.post("/turns/{turn_id}/cancel")
    def cancel_turn(turn_id: str, request: Request):
        return call(turns.cancel, device(request), turn_id)

    return router

