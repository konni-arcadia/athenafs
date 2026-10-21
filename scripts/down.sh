#!/bin/sh
# Stops everything and releases the host mount point.
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
DATA=${DATA:-$PWD/data}
case $DATA in /*) ;; *) DATA=$PWD/$DATA ;; esac
export DATA
docker compose down
while findmnt -rn "$DATA/mount" >/dev/null; do sudo umount -l "$DATA/mount" || break; done
