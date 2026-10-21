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
# Address of the web page for the links at the root of the share: this machine's mDNS name unless set in .env.
ATHENAFS_PAGE_URL=${ATHENAFS_PAGE_URL:-http://$(hostname).local:${ATHENAFS_PORT:-8080}/}
export ATHENAFS_PAGE_URL
# Commit the running code comes from (scripts/measure.sh checks it): -dirty when the tree has uncommitted changes.
ATHENAFS_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
[ -z "$(git status --porcelain 2>/dev/null)" ] || ATHENAFS_COMMIT="$ATHENAFS_COMMIT-dirty"
export ATHENAFS_COMMIT
# a dead FUSE mount left by a killed run (stat fails with "Transport endpoint is not connected"): unmount the FUSE
# sub-folder only, never "$DATA/mount" itself (that is the bind mount)
while findmnt -rn -t fuse.distribyted "$DATA/mount/fs" >/dev/null && ! ls "$DATA/mount/fs" >/dev/null 2>&1; do
  sudo umount -l "$DATA/mount/fs" || break
done
mkdir -p "$DATA/torrents" "$DATA/state" "$DATA/mount/fs"
findmnt -rn "$DATA/mount" >/dev/null || sudo mount --bind "$DATA/mount" "$DATA/mount"
sudo mount --make-rshared "$DATA/mount"
docker compose up -d --build "$@"
