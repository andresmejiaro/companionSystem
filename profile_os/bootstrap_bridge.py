"""Bootstrap an operational bridge principal + credential locally.

Usage:
    python -m profile_os.bootstrap_bridge --data-dir data --secret "$SECRET"

This is for MCP/hosted-assistant adapters. It grants operational profile
access only: boot, memory, closeout, dynamic-store proposal, and dynamic-store
record read/write. It deliberately does not grant store approval, audit,
profile management, credential management, or grant management.
"""

from __future__ import annotations

import argparse

from .access import ALL_PROFILES, AccessControl
from .storage import Store

BRIDGE_NAME = "Claude MCP bridge"
BRIDGE_OPS = [
    "boot",
    "remember",
    "search",
    "closeout",
    "stores:propose",
    "records:read",
    "records:write",
]


def _profiles_arg(value: str) -> list[str]:
    profiles = [item.strip() for item in value.split(",") if item.strip()]
    return profiles or [ALL_PROFILES]


def bootstrap(
    data_dir: str,
    secret: str,
    profiles: list[str] | None = None,
    label: str = "mcp backend key",
    display_name: str = BRIDGE_NAME,
) -> dict:
    if not secret:
        raise ValueError("secret is required")
    profile_ids = profiles or [ALL_PROFILES]
    store = Store(data_dir)
    try:
        access = AccessControl(store)
        row = access.db.execute(
            "SELECT id FROM access_principals WHERE kind='bridge' AND display_name=?"
            " AND disabled_at IS NULL", (display_name,)).fetchone()
        principal = access.get_principal(row["id"]) if row else \
            access.create_principal("bridge", display_name)
        credential = access.create_credential(principal["id"], label, secret)
        granted = []
        for profile_id in profile_ids:
            for op in BRIDGE_OPS:
                if not access.allowed(principal["id"], op, profile_id):
                    access.grant(principal["id"], op, profile_id=profile_id)
                    granted.append({"profile_id": profile_id, "operation": op})
        # identity:read is global (not profile-scoped): every bridge gets
        # read access to the "who am I" drift-prevention file, if configured.
        if not access.allowed(principal["id"], "identity:read", None):
            access.grant(principal["id"], "identity:read", profile_id=None)
            granted.append({"profile_id": None, "operation": "identity:read"})
        return {
            "principal_id": principal["id"],
            "credential_id": credential["id"],
            "profiles": profile_ids,
            "operations": BRIDGE_OPS,
            "granted": granted,
        }
    finally:
        store.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--secret", required=True,
                        help="backend bearer secret to hash; never stored or printed")
    parser.add_argument("--profiles", default=ALL_PROFILES,
                        help="comma-separated profile ids, or '*' for all profiles")
    parser.add_argument("--label", default="mcp backend key")
    parser.add_argument("--display-name", default=BRIDGE_NAME)
    args = parser.parse_args(argv)
    result = bootstrap(
        args.data_dir,
        args.secret,
        _profiles_arg(args.profiles),
        label=args.label,
        display_name=args.display_name,
    )
    print(f"bridge principal: {result['principal_id']}")
    print(f"credential:       {result['credential_id']} (hash stored; secret NOT stored)")
    print(f"profiles:         {', '.join(result['profiles'])}")
    print(f"operations:       {', '.join(result['operations'])}")
    print(f"new grants:       {len(result['granted'])}")


if __name__ == "__main__":
    main()
