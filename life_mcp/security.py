from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from html import escape
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .state import JsonState


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def host_allowed(uri: str, patterns: list[str]) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return False
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        return False
    host = parsed.hostname.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if pattern.startswith("*.") and (
            host == pattern[2:] or host.endswith(pattern[1:])
        ):
            return True
        if host == pattern:
            return True
    return False


def origin_allowed(origin: str | None, patterns: list[str]) -> bool:
    if not origin:
        return True
    return host_allowed(origin, [urlparse(item).hostname or item for item in patterns])


@dataclass(slots=True)
class MCPAuth:
    settings: Settings
    state: JsonState

    def _sign(self, payload: dict[str, Any]) -> str:
        header = _b64(b'{"alg":"HS256","typ":"JWT"}')
        body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = hmac.new(
            self.settings.oauth_signing_key.encode(),
            f"{header}.{body}".encode(),
            hashlib.sha256,
        ).digest()
        return f"{header}.{body}.{_b64(signature)}"

    def validate_access_token(self, token: str) -> bool:
        try:
            header, body, supplied = token.split(".")
            expected = hmac.new(
                self.settings.oauth_signing_key.encode(),
                f"{header}.{body}".encode(),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(expected, _unb64(supplied)):
                return False
            payload = json.loads(_unb64(body))
            return (
                payload.get("iss") == self.settings.public_base_url
                and payload.get("aud") == self.settings.resource_url
                and payload.get("exp", 0) > int(time.time())
                and "mcp" in str(payload.get("scope", "")).split()
            )
        except (ValueError, KeyError, json.JSONDecodeError):
            return False

    def authenticated(self, header: str | None) -> bool:
        if not self.settings.auth_required:
            return True
        if not header or not header.startswith("Bearer "):
            return False
        token = header[7:]
        if any(hmac.compare_digest(token, item) for item in self.settings.connector_tokens):
            return True
        return bool(self.settings.oauth_signing_key) and self.validate_access_token(token)

    def register(self, redirect_uris: list[str], client_name: str) -> dict[str, Any]:
        if not redirect_uris or not all(
            host_allowed(uri, self.settings.allowed_redirect_hosts) for uri in redirect_uris
        ):
            raise ValueError("invalid redirect_uri")
        client = {
            "client_id": "life_" + secrets.token_urlsafe(24),
            "client_name": client_name[:100],
            "redirect_uris": redirect_uris,
            "created_at": int(time.time()),
        }

        def update(data: dict[str, Any]) -> None:
            data.setdefault("clients", {})[client["client_id"]] = client

        self.state.mutate(update)
        return {
            **client,
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }

    def validate_authorize(
        self, params: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        client_id = str(params.get("client_id") or "")
        redirect_uri = str(params.get("redirect_uri") or "")
        challenge = str(params.get("code_challenge") or "")
        client = self.state.read().get("clients", {}).get(client_id)
        if not client or redirect_uri not in client.get("redirect_uris", []):
            return None, "unknown client or redirect_uri"
        if (
            params.get("response_type") != "code"
            or params.get("code_challenge_method") != "S256"
            or not challenge
        ):
            return None, "authorization_code with PKCE S256 is required"
        return {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": str(params.get("state") or ""),
            "scope": "mcp",
        }, None

    def authorize_html(self, params: dict[str, Any], error: str | None = None) -> str:
        hidden = "".join(
            f'<input type="hidden" name="{escape(str(key))}" '
            f'value="{escape(str(value))}">' for key, value in params.items()
        )
        problem = f'<p class="error">{escape(error)}</p>' if error else ""
        return f"""<!doctype html><html><head><meta name="viewport" content="width=device-width"><title>Authorize Life</title>
<style>body{{font:16px system-ui;background:#111;color:#eee;max-width:34rem;margin:4rem auto;padding:1rem}}input,button{{font:inherit;padding:.7rem;width:100%;box-sizing:border-box}}button{{margin-top:1rem;background:#9ad0f5;border:0;font-weight:700}}.error{{color:#ff8d8d}}</style></head>
<body><h1>Authorize Life MCP</h1><p>Grant read-only access to the published career truth snapshot.</p>{problem}<form method="post" action="/oauth/authorize">{hidden}<label>Life MCP admin secret<input type="password" name="admin_secret" required autocomplete="off"></label><button type="submit">Authorize MCP client</button></form></body></html>"""

    def create_code(self, validated: dict[str, Any]) -> str:
        code = "life_code_" + secrets.token_urlsafe(32)
        record = {**validated, "expires_at": int(time.time()) + 300}

        def update(data: dict[str, Any]) -> None:
            data.setdefault("codes", {})[code] = record

        self.state.mutate(update)
        return code

    def exchange_code(self, data: dict[str, Any]) -> dict[str, Any]:
        code = str(data.get("code") or "")

        def consume(state: dict[str, Any]) -> dict[str, Any] | None:
            return state.setdefault("codes", {}).pop(code, None)

        record = self.state.mutate(consume)
        if not record or record["expires_at"] < int(time.time()):
            raise ValueError("invalid or expired authorization code")
        if (
            data.get("client_id") != record["client_id"]
            or data.get("redirect_uri") != record["redirect_uri"]
        ):
            raise ValueError("client or redirect URI mismatch")
        verifier = str(data.get("code_verifier") or "")
        digest = _b64(hashlib.sha256(verifier.encode()).digest())
        if not verifier or not hmac.compare_digest(digest, record["code_challenge"]):
            raise ValueError("invalid PKCE verifier")
        now = int(time.time())
        token = self._sign({
            "iss": self.settings.public_base_url,
            "aud": self.settings.resource_url,
            "sub": record["client_id"],
            "scope": "mcp",
            "iat": now,
            "exp": now + self.settings.oauth_access_token_ttl_seconds,
        })
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": self.settings.oauth_access_token_ttl_seconds,
            "scope": "mcp",
        }
