"""Bootstrap the first admin principal + credential locally.

Usage:
    python -m profile_os.bootstrap_admin --data-dir data --secret "$SECRET"

Creates (idempotently, keyed by display name) an admin principal, stores a
credential holding only a salted hash of the secret, and grants
stores:approve over all profiles ('*') plus audit:read, manage_grants and
credentials:manage. Never prints or stores the plaintext secret.

This is deliberately a local CLI, not an HTTP endpoint: minting the first
admin credential must not be possible over the (unauthenticated) API.
"""

from __future__ import annotations

import argparse

from .access import ALL_PROFILES, AccessControl
from .storage import Store

ADMIN_NAME = "bootstrap admin"
ADMIN_OPS = ["stores:approve", "audit:read", "manage_grants", "credentials:manage"]


def bootstrap(data_dir: str, secret: str, label: str = "bootstrap key") -> dict:
    store = Store(data_dir)
    try:
        access = AccessControl(store)
        row = access.db.execute(
            "SELECT id FROM access_principals WHERE kind='admin' AND display_name=?"
            " AND disabled_at IS NULL", (ADMIN_NAME,)).fetchone()
        principal = access.get_principal(row["id"]) if row else \
            access.create_principal("admin", ADMIN_NAME)
        credential = access.create_credential(principal["id"], label, secret)
        granted = []
        for op in ADMIN_OPS:
            if not access.allowed(principal["id"], op, ALL_PROFILES):
                access.grant(principal["id"], op, profile_id=ALL_PROFILES)
                granted.append(op)
        return {"principal_id": principal["id"], "credential_id": credential["id"],
                "granted": granted, "profile_scope": ALL_PROFILES}
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--secret", required=True,
                        help="the bearer secret to hash; it is never stored or printed")
    parser.add_argument("--label", default="bootstrap key")
    args = parser.parse_args(argv)
    result = bootstrap(args.data_dir, args.secret, args.label)
    print(f"admin principal: {result['principal_id']}")
    print(f"credential:      {result['credential_id']} (hash stored; secret NOT stored)")
    print(f"grants ensured:  {ADMIN_OPS} on profiles '{ALL_PROFILES}'")
    print('use with: curl -H "Authorization: Bearer <your secret>" '
          "-X POST .../stores/<name>/approve (requires PROFILE_OS_AUTH_ENABLED=1)")


if __name__ == "__main__":
    main()
