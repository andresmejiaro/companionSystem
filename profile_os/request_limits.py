"""Shared HTTP request-body limits for the backend and public MCP adapter."""

from __future__ import annotations

import os

from fastapi import Request


DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024


class RequestBodyTooLarge(Exception):
    pass


def configured_max_request_bytes() -> int:
    raw = os.environ.get(
        "PROFILE_OS_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES))
    try:
        limit = int(raw)
    except ValueError as error:
        raise RuntimeError("PROFILE_OS_MAX_REQUEST_BYTES must be an integer") from error
    if limit < 1:
        raise RuntimeError("PROFILE_OS_MAX_REQUEST_BYTES must be positive")
    return limit


async def read_request_body(request: Request, limit: int) -> bytes:
    """Read at most ``limit`` bytes, including for chunked requests."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise RequestBodyTooLarge
        except ValueError:
            pass

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise RequestBodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def replay_request_body(request: Request, body: bytes) -> None:
    """Make a body consumed by middleware available to downstream parsing."""
    # BaseHTTPMiddleware's wrapped receive checks Request._body before it
    # consults _receive. Preserve both because read_request_body() consumed
    # the original stream without calling Request.body().
    request._body = body
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive
