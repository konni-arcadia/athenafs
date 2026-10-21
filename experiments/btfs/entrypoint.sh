#!/bin/sh
# Mounts every source (magnet or .torrent) listed in /config/sources.txt, one sub-folder per "name source" line.
set -e
MNT=/mnt/torrents
UP="${UPLOAD_KBS:-1}"          # kB/s; 0 means unlimited in libtorrent, so we use 1
while read -r name src; do
  case "$name" in ''|\#*) continue;; esac
  mkdir -p "$MNT/$name" "/cache/$name"
  btfs --data-directory="/cache/$name" --max-upload-rate="$UP" \
       -o allow_other "$src" "$MNT/$name"
  echo "mounted: $name"
done < /config/sources.txt
exec tail -f /dev/null
