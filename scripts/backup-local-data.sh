#!/bin/bash
# Nightly backup of the local dev data/ dir (profile_os.db + profiles/*).
# Run by the profile-os-backup.timer systemd user unit.
set -euo pipefail

SRC="/home/andres/FableCompanion/data"
DEST="/home/andres/backups/profile-os"
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$DEST"
tar czf "$DEST/data-${STAMP}.tar.gz" -C "$(dirname "$SRC")" "$(basename "$SRC")"

# keep 14 days
find "$DEST" -name 'data-*.tar.gz' -mtime +14 -delete
