#!/bin/sh
# Prepares the host mount point, then starts everything.
# The mount point must be a bind mount of itself with shared propagation: that is what lets the FUSE mount made
# inside the core container show up on the host and in the SMB container, including after a restart.
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
DATA=${DATA:-$PWD/data}
case $DATA in /*) ;; *) DATA=$PWD/$DATA ;; esac
export DATA
mkdir -p "$DATA/torrents" "$DATA/state" "$DATA/mount/fs"
findmnt -rn "$DATA/mount" >/dev/null || sudo mount --bind "$DATA/mount" "$DATA/mount"
sudo mount --make-rshared "$DATA/mount"
docker compose up -d --build "$@"
