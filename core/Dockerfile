# Distribyted v0.15.0 + a web supervisor (admin.py): status page and per-torrent/file/folder erasing of local data.
FROM golang:1.24-alpine AS builder
RUN apk add --no-cache git fuse-dev gcc libc-dev g++ make
RUN git clone --depth 1 --branch v0.15.0 https://github.com/distribyted/distribyted.git /src
WORKDIR /src
# Modules first, in their own layer: changing a patch below then rebuilds without downloading them again.
RUN go mod download
# Local patch: Getattr leaves Nlink at 0, and Samba treats an object with 0 links as deleted
# (NT_STATUS_OBJECT_PATH_NOT_FOUND on the share root). Folder = 2, file = 1.
RUN sed -i 's/stat.Mode = fuse.S_IFDIR | 0555/stat.Mode = fuse.S_IFDIR | 0555; stat.Nlink = 2/; s/stat.Mode = fuse.S_IFREG | 0444/stat.Mode = fuse.S_IFREG | 0444; stat.Nlink = 1/' fuse/mount.go \
 && test "$(grep -c 'stat.Nlink' fuse/mount.go)" = 3
# Local patch: DISTRIBYTED_ARCHIVES=off treats .zip/.rar/.7z as ordinary files (not folders).
# Opening an archive as a folder downloads its headers (4 to 10 MB per archive measured): a recursive listing, a preview
# or a Finder "Get Info" triggers as many of these as there are archives.
RUN printf 'package fs\n\nimport "os"\n\nfunc init() {\n\tif os.Getenv("DISTRIBYTED_ARCHIVES") == "off" {\n\t\tSupportedFactories = map[string]FsFactory{}\n\t}\n}\n' > fs/archives_off.go
# Local patch: Torrent.load() filled its file table under a read lock, so two concurrent first accesses (start-up and a
# FUSE/Samba request) wrote the same Go map and the process died with "concurrent map read and map write". Seen with
# 64 torrents (the longer load makes the overlap likely). load() now takes the write lock; Open/ReadDir read under the read lock.
COPY patches/torrent_load_lock.patch /tmp/torrent_load_lock.patch
RUN git apply /tmp/torrent_load_lock.patch
# Local patch: a path was routed to the first mounted filesystem whose name it merely starts with, so the folder
# "name-13" could be served by "name-1" or "name" (map order is random) and fail with "No such file or directory".
# A path now belongs to a filesystem only if it is that path or lies under it.
COPY patches/fs_prefix_match.patch /tmp/fs_prefix_match.patch
RUN git apply /tmp/fs_prefix_match.patch
# Local patch: a multi-file torrent shows its own top-level directory inside its folder (folder/Name/...). When every
# torrent of a folder has the same top-level directory (one torrent, or a collection split in parts), it is skipped,
# whatever the folder is called, so renaming a folder does not bring the level back. admin.py (share_path) follows the
# same rule. Applies after the lock patch (same file).
COPY patches/flatten_single_root.patch /tmp/flatten_single_root.patch
RUN git apply /tmp/flatten_single_root.patch
# Local patch: the per-torrent stats are deltas between two API calls, so every caller resets them, and when /status was
# called less than 2 s before, /routes reports 0 peers. Add cumulative byte counters and always fill peers/seeders;
# admin.py computes the rates from the counters.
COPY patches/stats_totals.patch /tmp/stats_totals.patch
RUN git apply /tmp/stats_totals.patch
# Local patch: the torrents of the configuration were added one after the other before anything was mounted: each
# unknown magnet delayed the share by up to add_timeout (462 new magnets: tens of minutes) and each .torrent file took a
# fraction of a second (520: about 100 s). They are now added in parallel (at most 128 at a time) and in the background:
# the share is mounted at once and each folder fills in as its torrents are added.
COPY patches/parallel_load.patch /tmp/parallel_load.patch
RUN git apply /tmp/parallel_load.patch
# Local patch: a magnet whose info did not arrive within add_timeout was dropped until the next restart. It now keeps
# waiting in the background and is added to its folder when the info arrives (no restart). Applies after the patch above.
COPY patches/late_metadata.patch /tmp/late_metadata.patch
RUN git apply /tmp/late_metadata.patch
# Local patch: dormant torrents. With $DISTRIBYTED_DORMANT_AFTER > 0 (seconds), a torrent given as a .torrent file is served
# from its metadata only (folders list without the BitTorrent client) and added to the client when one of its files is
# opened; it leaves the client again once no file of it has been open for that long. Every torrent in the client cost
# memory and CPU (one lock per piece, a DHT announcer, a scraper per tracker): 636 torrents, 12.5 million pieces, 14 GB.
# Dormant torrents do not seed. 0 = the old behaviour. Applies after the patches above.
COPY patches/dormant_torrents.patch /tmp/dormant_torrents.patch
RUN git apply /tmp/dormant_torrents.patch
# Local patch: health check. Dormant torrents are woken in turn ($DISTRIBYTED_HEALTH_PARALLEL at a time, for
# $DISTRIBYTED_HEALTH_PROBE s, no file open so nothing is downloaded) and the most peers and seeders seen are reported in
# /api/routes ("health"). Order from $DISTRIBYTED_HEALTH_ORDER (written by admin.py: never or least recently checked first).
# A torrent that wants no data seldom connects to peers: the peers known (announced on the DHT and trackers) are the signal.
COPY patches/health_check.patch /tmp/health_check.patch
RUN git apply /tmp/health_check.patch
# Local patch: files of $DISTRIBYTED_ROOT_FILES are served at the root of the mount (admin.py puts links to its page there).
COPY patches/root_files.go /src/fs/root_files.go
RUN sed -i 's|\treturn &ContainerFs{s: s}, nil|\taddRootFiles(s)\n\treturn \&ContainerFs{s: s}, nil|' fs/container.go \
 && test "$(grep -c 'addRootFiles(s)' fs/container.go)" = 1
# Local patch: write the metadata of every torrent whose info was obtained (magnets) to $DISTRIBYTED_METAINFO_DIR.
# Kept after the patch files: it edits a line they use as context.
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
