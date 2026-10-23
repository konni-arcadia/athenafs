#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Smoke test of a core image on any Docker host, Docker Desktop on macOS included (an Apple Silicon Mac tests arm64):
# runs the image alone in a throwaway volume (no SMB share, no host mount, gentleman mode and health checks off), adds a
# small public-domain torrent through the API, reads one small file twice inside the container (the second read must
# download nothing), then removes the container and the volume.
# Usage, from the repository: sh scripts/smoke-image.sh [image]   (default: the latest release's core image)
set -eu
cd "$(dirname "$0")/.."
img=${1:-ghcr.io/konni-arcadia/athenafs-core:latest}
name=athenafs-smoke
port=18080
url=https://archive.org/download/1601_0903_librivox/1601_0903_librivox_archive.torrent   # LibriVox: public domain
api() { curl -s "http://127.0.0.1:$port$1"; }
json() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; docker volume rm "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "== image $img"
docker pull "$img" >/dev/null 2>&1 || echo "(not pulled: using the local image)"
# AppArmor only where the host has it (Docker Desktop has not)
aa=""; docker info --format '{{.SecurityOptions}}' | grep -q apparmor && aa="--security-opt apparmor:unconfined"
docker run -d --name "$name" --device /dev/fuse --cap-drop ALL --cap-add SYS_ADMIN --cap-add DAC_OVERRIDE $aa \
  --security-opt no-new-privileges:true -p "127.0.0.1:$port:8080" -v "$name:/data" \
  -v "$PWD/core/config.yaml:/config/config.yaml:ro" \
  -e DISTRIBYTED_CONFIG=/config/config.yaml -e DISTRIBYTED_METAINFO_DIR=/data/metainfo \
  -e DISTRIBYTED_DORMANT_AFTER=600 -e DISTRIBYTED_HEALTH_PROBE=0 -e DISTRIBYTED_HEALTH_ORDER=/data/health-order.txt \
  -e DISTRIBYTED_ROOT_FILES=/data/share-root -e DISTRIBYTED_READY_VIEWS=/data/ready-views.json \
  -e ATHENAFS_GENTLEMAN=off "$img" >/dev/null
echo "architecture: $(docker exec "$name" uname -m)"

n=0; until [ "$(api /api/status | json 'd["service"]' 2>/dev/null)" = running ]; do
  n=$((n + 1)); [ $n -lt 60 ] || { echo "FAIL: the service did not start"; docker logs --tail 30 "$name"; exit 1; }; sleep 2; done
echo "service: running ($(api /api/status | json 'd["commit"] or "no commit recorded"'))"

curl -s -o /dev/null -H "Origin: http://127.0.0.1:$port" --data-urlencode "link=$url" "http://127.0.0.1:$port/add"
n=0; until [ "$(api /api/status | json 'd["torrents"]["mounted"]' 2>/dev/null)" = 1 ]; do
  n=$((n + 1)); [ $n -lt 90 ] || { echo "FAIL: the torrent was not mounted"; docker logs --tail 30 "$name"; exit 1; }; sleep 2; done
echo "torrent: mounted"

# one small file, read twice inside the container
f=$(docker exec "$name" sh -c 'find /mnt/torrents/fs -type f -size -100k -name "*.xml" | head -1')
[ -n "$f" ] || { echo "FAIL: no small file found in the mount"; exit 1; }
c0=$(api /api/status | json 'd["cache"]["used"]')
s1=$(docker exec "$name" timeout 180 sha256sum "$f" | cut -c1-16) || { echo "FAIL: the first read did not finish"; exit 1; }
sleep 3; c1=$(api /api/status | json 'd["cache"]["used"]')
s2=$(docker exec "$name" sha256sum "$f" | cut -c1-16)
sleep 3; c2=$(api /api/status | json 'd["cache"]["used"]')
echo "file: ${f#/mnt/torrents/fs/}"
echo "first read:  $s1, cache +$((c1 - c0)) bytes"
echo "second read: $s2, cache +$((c2 - c1)) bytes"
if [ "$s1" = "$s2" ] && [ $((c1 - c0)) -gt 0 ] && [ $((c2 - c1)) -eq 0 ]; then echo "PASS"; else echo "FAIL"; exit 1; fi
