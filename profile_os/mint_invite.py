"""Mint a one-time agent-enrollment invite, locally.

Usage:
    python -m profile_os.mint_invite --data-dir data [--expires-hours 24]

Prints the plaintext token once; only its hash is stored. Send the token to
the agent out of band (it consumes it via POST /enroll).
"""

from __future__ import annotations

import argparse

from .access import AccessControl
from .enroll import DEFAULT_INVITE_EXPIRES_HOURS, Enrollment
from .storage import Store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--expires-hours", type=float, default=DEFAULT_INVITE_EXPIRES_HOURS)
    parser.add_argument("--display-name-hint", default="")
    args = parser.parse_args(argv)

    store = Store(args.data_dir)
    try:
        access = AccessControl(store)
        enrollment = Enrollment(access)
        invite_id, token = enrollment.mint_invite(
            kind="agent", display_name_hint=args.display_name_hint,
            expires_hours=args.expires_hours)
        print(f"invite id: {invite_id}")
        print(f"expires in: {args.expires_hours}h")
        print(f"token (send once, not stored): {token}")
        print('agent enrolls with: POST /enroll {"invite_token": "<token>",'
              ' "display_name": "...", "public_key": "<base64 ed25519 pubkey>"}')
    finally:
        store.close()


if __name__ == "__main__":
    main()
