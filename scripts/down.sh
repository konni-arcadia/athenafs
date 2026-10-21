#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Stops everything and releases the host mount point.
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
DATA=${DATA:-$PWD/data}
case $DATA in /*) ;; *) DATA=$PWD/$DATA ;; esac
export DATA
docker compose down
# only when up.sh had to make it a bind mount of itself (hosts whose / is not shared)
while findmnt -rn "$DATA/mount" >/dev/null; do sudo umount -l "$DATA/mount" || break; done
