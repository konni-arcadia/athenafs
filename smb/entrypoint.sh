#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Creates the Samba account from SMB_USER / SMB_PASS (defaults: tor / rent), names the share SMB_SHARE (default
# torrents), then runs smbd.
set -e
U="${SMB_USER:-tor}"; P="${SMB_PASS:-rent}"; S="${SMB_SHARE:-torrents}"
case "$S" in *[!A-Za-z0-9._-]*|"") echo "SMB_SHARE: letters, digits, . _ - only" >&2; exit 1 ;; esac
sed -i "s/^\[[^]]*\] *# the share\$/[$S] # the share/" /etc/samba/smb.conf
id "$U" >/dev/null 2>&1 || useradd -M -s /usr/sbin/nologin "$U"
printf '%s\n%s\n' "$P" "$P" | smbpasswd -a -s "$U" >/dev/null
exec tini -- smbd --foreground --no-process-group --debug-stdout
