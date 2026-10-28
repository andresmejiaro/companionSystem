"""Issue or revoke a Tamagochi device credential using a local data directory."""

from __future__ import annotations

import argparse
from pathlib import Path

from .access import AccessControl, AccessError
from .pet_devices import PetDeviceService
from .storage import Store


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True,
                        help="explicit existing Companions data directory")
    sub = parser.add_subparsers(dest="command", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("--profiles", required=True, help="comma-separated hilo,limo")
    issue.add_argument("--expires-hours", type=float, default=1.0)
    issue.add_argument("--device-name", default="Tamagochi device")
    issue.add_argument("--identity-read", action="store_true")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("credential_id")
    args = parser.parse_args(argv)

    if not (Path(args.data_dir) / "profile_os.db").is_file():
        parser.error("--data-dir must point to an existing Companions database")

    store = Store(args.data_dir)
    try:
        service = PetDeviceService(AccessControl(store))
        if args.command == "issue":
            hours = args.expires_hours
            if not 0 < hours <= 24:
                parser.error("--expires-hours must be greater than 0 and at most 24")
            profiles = [part.strip() for part in args.profiles.split(",") if part.strip()]
            try:
                issued = service.issue(profiles, int(hours * 3600), args.device_name,
                                       args.identity_read)
            except ValueError as exc:
                parser.error(str(exc))
            print(f"device_id: {issued['device_id']}")
            print(f"credential_id: {issued['credential_id']}")
            print(f"principal_id: {issued['principal_id']}")
            print(f"profiles: {', '.join(issued['profiles'])}")
            print(f"expires_at: {issued['expires_at']:.0f}")
            print(f"bearer (copy once; it is not stored in plaintext): {issued['secret']}")
        else:
            try:
                service.revoke(args.credential_id)
            except AccessError as exc:
                parser.error(str(exc))
            print(f"revoked credential: {args.credential_id}")
    finally:
        store.close()


if __name__ == "__main__":
    main()
