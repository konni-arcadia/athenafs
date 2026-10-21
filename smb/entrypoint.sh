#!/bin/sh
# Creates the Samba account from SMB_USER / SMB_PASS (defaults: tor / rent), then runs smbd.
set -e
U="${SMB_USER:-tor}"; P="${SMB_PASS:-rent}"
id "$U" >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin "$U"
printf '%s\n%s\n' "$P" "$P" | smbpasswd -a -s "$U" >/dev/null
exec tini -- smbd --foreground --no-process-group --debug-stdout
