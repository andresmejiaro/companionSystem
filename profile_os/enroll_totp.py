"""Enroll (or re-enroll) the admin principal for TOTP-gated approvals.

Two-step, local CLI only (never over HTTP):

    python -m profile_os.enroll_totp --data-dir data
        -> prints an otpauth:// URI. Scan it (as a QR you generate yourself)
           or paste the secret manually into Google/Microsoft Authenticator.

    python -m profile_os.enroll_totp --data-dir data --confirm 123456
        -> confirms enrollment with the first live code from the app.
           Until confirmed, verify_totp() refuses the secret, so a half-done
           enrollment can never silently gate approvals with a code nobody
           has.

TOTP gates "edgy" actions only (currently: approving a companion's own
proposed prompt edit). Routine writes (remember, closeout, records) never
need a code — see ACCESS_CONTROL.md "TOTP-gated approvals".
"""

from __future__ import annotations

import argparse

from .access import AccessControl
from .storage import Store


def _admin_principal_id(access: AccessControl) -> str | None:
    row = access.db.execute(
        "SELECT id FROM access_principals WHERE kind='admin' AND disabled_at IS NULL"
        " ORDER BY created_at LIMIT 1").fetchone()
    return row["id"] if row else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--confirm", help="6-digit code from your authenticator app")
    args = parser.parse_args(argv)

    store = Store(args.data_dir)
    try:
        access = AccessControl(store)
        principal_id = _admin_principal_id(access)
        if principal_id is None:
            print("no admin principal found; run bootstrap_admin first")
            return
        if args.confirm:
            ok = access.confirm_totp(principal_id, args.confirm)
            print("confirmed — TOTP is now active for approvals" if ok
                 else "invalid code; re-run enroll_totp without --confirm to restart")
        else:
            uri = access.enroll_totp(principal_id)
            print("add this to Google/Microsoft Authenticator (scan as QR or")
            print("paste the otpauth:// URI / secret manually):")
            print(uri)
            print()
            print("then confirm with:")
            print("  python -m profile_os.enroll_totp --data-dir "
                 f"{args.data_dir} --confirm <6-digit code>")
    finally:
        store.close()


if __name__ == "__main__":
    main()
