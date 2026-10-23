# pinned by digest (see core/Dockerfile): to update, change it, rebuild, test from a Mac
FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad
RUN apt-get update && apt-get install -y --no-install-recommends samba tini && rm -rf /var/lib/apt/lists/*
COPY smb.conf /etc/samba/smb.conf
COPY entrypoint.sh /entrypoint.sh
# Licences and sources of what this image contains (it is published): Debian ships each package's in
# /usr/share/doc/<package>/copyright; ours are in NOTICE.
COPY NOTICE /usr/share/doc/athenafs/NOTICE
LABEL org.opencontainers.image.licenses="GPL-3.0-or-later"
ENTRYPOINT ["/entrypoint.sh"]
