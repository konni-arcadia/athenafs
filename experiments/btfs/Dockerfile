FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends btfs fuse3 ca-certificates curl procps \
 && rm -rf /var/lib/apt/lists/* \
 && sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
