"""Client-side helper for the Ed25519 signed-request auth scheme.

Signed message:

    f"{ts}\\n{nonce}\\n{METHOD}\\n{PATH}\\n{sha256(body).hexdigest()}"

Used by companions/tests to build the `Authorization: Signature ...` header
described in ACCESS_CONTROL.md. Verification lives in api.py / access.py.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def signing_message(ts: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    body_hash = hashlib.sha256(body or b"").hexdigest()
    return f"{ts}\n{nonce}\n{method}\n{path}\n{body_hash}".encode()


def sign_request(private_key: Ed25519PrivateKey, key_id: str, method: str,
                 path: str, body: bytes = b"") -> str:
    """Build the full `Authorization` header value for one request."""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    message = signing_message(ts, nonce, method.upper(), path, body)
    signature = private_key.sign(message)
    sig_b64 = base64.b64encode(signature).decode()
    return f"Signature key_id={key_id},ts={ts},nonce={nonce},sig={sig_b64}"
