# Distribyted v0.15.0 + a web supervisor (admin.py): status page and per-torrent/file/folder erasing of local data.
FROM golang:1.24-alpine AS builder
RUN apk add --no-cache git fuse-dev gcc libc-dev g++ make
RUN git clone --depth 1 --branch v0.15.0 https://github.com/distribyted/distribyted.git /src
WORKDIR /src
# Local patch: Getattr leaves Nlink at 0, and Samba treats an object with 0 links as deleted
# (NT_STATUS_OBJECT_PATH_NOT_FOUND on the share root). Folder = 2, file = 1.
RUN sed -i 's/stat.Mode = fuse.S_IFDIR | 0555/stat.Mode = fuse.S_IFDIR | 0555; stat.Nlink = 2/; s/stat.Mode = fuse.S_IFREG | 0444/stat.Mode = fuse.S_IFREG | 0444; stat.Nlink = 1/' fuse/mount.go \
 && test "$(grep -c 'stat.Nlink' fuse/mount.go)" = 3
# Local patch: DISTRIBYTED_ARCHIVES=off treats .zip/.rar/.7z as ordinary files (not folders).
# Opening an archive as a folder downloads its headers (4 to 10 MB per archive measured): a recursive listing, a preview
# or a Finder "Get Info" triggers as many of these as there are archives.
RUN printf 'package fs\n\nimport "os"\n\nfunc init() {\n\tif os.Getenv("DISTRIBYTED_ARCHIVES") == "off" {\n\t\tSupportedFactories = map[string]FsFactory{}\n\t}\n}\n' > fs/archives_off.go
# Local patch: write the metadata of every torrent whose info was obtained (magnets) to $DISTRIBYTED_METAINFO_DIR.
COPY patches/metainfo_dump.go /src/torrent/metainfo_dump.go
RUN sed -i 's|// Add to stats|dumpMetainfo(t) // Add to stats|' torrent/service.go \
 && test "$(grep -c 'dumpMetainfo(t)' torrent/service.go)" = 1
RUN BIN_OUTPUT=/bin/distribyted make build

FROM alpine:3
RUN apk add --no-cache gcc libc-dev fuse-dev python3 \
 && echo "user_allow_other" > /etc/fuse.conf
COPY --from=builder /bin/distribyted /bin/distribyted
COPY admin.py /app/admin.py
ENV DISTRIBYTED_FUSE_ALLOW_OTHER=true
ENTRYPOINT ["python3", "-u", "/app/admin.py"]
