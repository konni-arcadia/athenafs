#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Creates the Samba account from SMB_USER / SMB_PASS (defaults: tor / rent), names the share SMB_SHARE (default
# torrents), keeps the hidden folders up to date, then runs smbd.
set -e
U="${SMB_USER:-tor}"; P="${SMB_PASS:-rent}"; S="${SMB_SHARE:-torrents}"
case "$S" in *[!A-Za-z0-9._-]*|"") echo "SMB_SHARE: letters, digits, . _ - only" >&2; exit 1 ;; esac
sed -i "s/^\[[^]]*\] *# the share\$/[$S] # the share/" /etc/samba/smb.conf
id "$U" >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin "$U"
printf '%s\n%s\n' "$P" "$P" | smbpasswd -a -s "$U" >/dev/null

# What Finder and Explorer write or read for their previews and indexes: invisible, so never read. Plus the folders the
# page hides from the share (/etc/athenafs/hidden-folders.txt, one per line, written by the core's supervisor): they stay
# in the mount (search, Cache, ready views) but clients neither see nor open them. Rebuilt and reloaded when that changes.
STATIC='/.DS_Store/._*/.Spotlight-V100/.Trashes/.fseventsd/.TemporaryItems/.localized/Thumbs.db/desktop.ini/$RECYCLE.BIN/System Volume Information/'
HIDDEN=/etc/athenafs/hidden-folders.txt
veto() {
  h=$(grep -E '^[A-Za-z0-9._-]+$' "$HIDDEN" 2>/dev/null | tr '\n' '/' || true)   # set -e: no match is not an error
  printf '   veto files = %s%s\n' "$STATIC" "$h" > /etc/samba/athenafs-veto.conf
}
veto
(
  last=$(cat "$HIDDEN" 2>/dev/null || true)
  while sleep 3; do
    cur=$(cat "$HIDDEN" 2>/dev/null || true)
    if [ "$cur" != "$last" ]; then
      last=$cur; veto; smbcontrol smbd reload-config >/dev/null 2>&1 || true
    fi
  done
) &
exec tini -- smbd --foreground --no-process-group --debug-stdout
