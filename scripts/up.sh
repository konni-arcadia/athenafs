#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Prepares the host mount point, then starts everything.
# The mount point must be on a mount with shared propagation: that is what lets the FUSE mount made inside the core
# container show up on the host and in the SMB container, including after a restart. On systemd hosts / is shared
# already and nothing is needed; elsewhere the folder is made a shared bind mount of itself, which needs sudo.
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
# risky exposures: printed here, shown on the page and in /api/status
loopback() { case "$1" in 127.*|::1|localhost) return 0 ;; *) return 1 ;; esac; }
w=""
if ! loopback "${ATHENAFS_BIND:-127.0.0.1}" && [ -z "${ATHENAFS_PASSWORD:-}" ]; then
  w="$w|the page and the API are reachable from the network (ATHENAFS_BIND=${ATHENAFS_BIND}) without a password: anyone there can add torrents and erase the cache. Set ATHENAFS_PASSWORD."
fi
if ! loopback "${SMB_BIND:-127.0.0.1}" && [ "${SMB_USER:-tor}" = tor ] && [ "${SMB_PASS:-rent}" = rent ]; then
  w="$w|the share is reachable from the network (SMB_BIND=${SMB_BIND}) with the test account tor / rent. Set SMB_USER and SMB_PASS."
fi
ATHENAFS_WARNINGS=${w#|}
export ATHENAFS_WARNINGS
[ -z "$w" ] || printf '%s\n' "$w" | tr '|' '\n' | sed '/^$/d; s/^/WARNING: /' >&2
# The share needs its port. Another SMB server on this host (Samba, ksmbd) usually holds 445 on every address, and
# Docker would then fail to start the smb container with a bare "address already in use": stop here and explain.
# Our own smb container holding the port (a restart) is fine.
smb_bind=${SMB_BIND:-127.0.0.1}; smb_port=${SMB_PORT:-445}
if command -v ss >/dev/null && [ -z "$(docker ps -q -f name='^athenafs-smb$' 2>/dev/null)" ]; then
  # a listener blocks us when it holds the port on every address, on our address, or when we want every address
  taken=$(ss -ltnH "sport = :$smb_port" 2>/dev/null | awk -v b="$smb_bind" '{a=$4; sub(/:[0-9]+$/, "", a); gsub(/[][]/, "", a)
    if (a == "0.0.0.0" || a == "::" || a == "*" || a == b || b == "0.0.0.0" || b == "::") { print a; exit }}')
  if [ -n "$taken" ]; then
    cat >&2 <<EOF
ERROR: port $smb_port is already used on this host ($taken:$smb_port), probably by another SMB server (the host's own
Samba or ksmbd), so the athenafs share cannot start there. Nothing was started. Either, in .env:
  - another port: SMB_PORT=4445 (or any free port). Macs connect with smb://<host>:4445/torrents; Windows only in
    recent versions, with: net use \\\\<host>\\torrents /TCPPORT:4445
  - or a second IP address on this host for the share: SMB_BIND=<that address> (port 445 stays usable by every client),
    with the host's own SMB server limited to its own address (Samba: "interfaces" and "bind interfaces only").
See "The SMB share" in README.md.
EOF
    exit 1
  fi
fi
mkdir -p "$DATA/torrents" "$DATA/state" "$DATA/state/smb" "$DATA/mount"
# mount/fs can be a dead FUSE mount left by a killed run (stat fails: "Transport endpoint is not connected"); the core
# unmounts it when it starts, and that propagates here
[ -d "$DATA/mount/fs" ] || mkdir "$DATA/mount/fs" 2>/dev/null || true
case "$(findmnt -no PROPAGATION --target "$DATA/mount")" in
  shared*) ;;
  *) echo "$DATA/mount is not on a shared mount: making it a shared bind mount of itself (sudo)"
     findmnt -rn "$DATA/mount" >/dev/null || sudo mount --bind "$DATA/mount" "$DATA/mount"
     sudo mount --make-rshared "$DATA/mount" ;;
esac
docker compose up -d --build "$@"
