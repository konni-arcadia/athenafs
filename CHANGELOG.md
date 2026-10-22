# Changelog

Notable changes, newest first. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/) once the first one is tagged. Each entry names the commits; measurements per
commit are in `docs/measurements.md`, the reasons and tests behind each change in `docs/findings.md`.

**Upgrading:** read the "Settings" and "Changed" lines of every version between yours and the new one, compare your `.env`
with `.env.example`, then `sh scripts/up.sh` (it rebuilds).

## [Unreleased]

### Added
- Ready views: per folder, a sibling folder (`<folder>_READY`) showing its files that are completely in the cache,
  archives (zip, 7z, rar) opened as folders and other files as they are, so browsing it never downloads anything. Set on
  the page (Folders: turn on, rename, turn off) or with `POST /api/ready-view`; renaming a folder renames its view unless
  the view's name was customised. Distribyted patches: `ready_views` (the views, rebuilt when the list of complete files
  changes, every 30 s) and `fs_indexed_lookup` (lookups walk up the path instead of scanning every mounted filesystem).
  The first prototype's "archive views" (archives only, `_ZIP`) are migrated.
- Hide a folder from the share (page, or `POST /api/folder-visibility`): Samba neither lists nor opens it, within seconds
  and without a restart; it stays in the mount, so search, Cache and its ready view keep working. With its ready view on,
  the share shows only what is ready ("local library"). Settings: `/data/folder_settings.json`.
- Gentleman mode, on by default: trackers are scraped every 6 h (seeders, leechers); up to 3 torrents with 1 or 2
  seeders and leechers waiting get a slot, download their rarest pieces (counted from the connected peers' bitfields)
  into a separate cache (5 % of the cache cap) and seed them, until 3 seeders or 24 h. Settings on the page; patches
  `gentleman` and `upload_limit`; `GET /api/gentleman`.
- Upload cap for the whole client, 1 MB/s by default (`ATHENAFS_UPLOAD_LIMIT_KBPS`, page Settings).
- Magnets without trackers get their folder's usual trackers (and `ATHENAFS_EXTRA_TRACKERS`) when the configuration is
  generated: 409 trackerless magnets went from about 1 resolved every 2 minutes (DHT only, 25 queries a second for the
  whole client) to 93 in 3 minutes.
- A magnet resolved while running goes to sleep at once (patch `sleep_after_metadata`): after the import, 343 such
  torrents had stayed awake (Distribyted 8.6 GB); now 0.
- Whole-folder actions on the page: erase its cache, remove the folder with or without its cached data (one restart);
  `POST /api/folder-remove`. Erasing whole torrents goes through the cached pieces (fast on torrents of hundreds of
  thousands of pieces).
- Page: security warnings as a header badge that unfolds; the Cache section folded by default; rates below 1 kB/s in B/s.
- Page: cache and disk donuts, rate histogram over 10 minutes with a scale, "Largest in the cache" folded at the bottom.
- Reproducible builds: base images pinned by digest (`golang:1.24-alpine`, `alpine:3`, `debian:trixie-slim`) and
  Distribyted fetched by commit (`58d471c`, tag v0.15.0) instead of by tag. Alpine and Debian packages installed during the
  build (`apk`, `apt`) are not pinned. Go modules are pinned by Distribyted's `go.sum`.
- Security (7213a7a): both containers drop every capability and set `no-new-privileges`; the core keeps `SYS_ADMIN` and
  `DAC_OVERRIDE`, Samba keeps `CHOWN`, `SETUID`, `SETGID`. `up.sh` needs `sudo` only where `/` is not a shared mount.
  Warnings (in `up.sh`, on the page, in `/api/status`) when the page is exposed without a password or the share with the
  test account. Torrent paths that leave the share (`..`) are never read. README: Security section.
- Licence GPL-3.0-or-later (c57a3e3).
- Every setting through `.env` (d403e53): `ATHENAFS_PASSWORD`, `ATHENAFS_SMB_HOST`, `SMB_SHARE`, `ATHENAFS_ADD_TIMEOUT`,
  `ATHENAFS_READ_TIMEOUT` (they were read nowhere, or only from files).
- API (30ee655): `/api/status`, `/api/torrents`, `/api/torrents/{hash}`, `/api/file`, `/api/usage` (largest in the cache
  by file, folder or torrent), `POST /api/erase` (a file, folder or torrent of the share), `smb_url` and `unc_path` on every
  item, documentation at `/api/docs` and `/api/openapi.json`; `X-Athenafs: 1` for scripts. Page: "Largest in the cache".
- Health check (c9145b6): dormant torrents woken in turn to count their peers; shown per torrent and in the search,
  warning before caching files of a torrent where no peer was found.
- Dormant torrents (e9c3b55): a torrent enters the BitTorrent client only while one of its files is open (plus
  `ATHENAFS_DORMANT_AFTER`, 600 s). With 636 torrents: core memory 13.8 GB to 1.0 GB, CPU at rest 85 % to 9 %.
- Measurements bound to commits (99f3c5a): `scripts/measure.sh`, `docs/measurements.md`.
- Page and features (2ac7150): new layout, activity (rates, files receiving data), search over every file name with its
  cache state, "Cache" (read files into the cache ahead of time), rename a folder or move a torrent, links to the page at
  the root of the share; start-up with hundreds of torrents (parallel, in the background, late magnets).
- Batch add of magnet links, merged folders for torrents with the same name, crash watchdog (3bf3a1c).

### Changed
- Supervisor rework (30f91c1): piece hashes in a table on disk, status computed in the background; supervisor memory
  13.9 GB to 0.9 GB, page and API from no answer within 120 s to immediate.
- Confirmations and name entry happen in the page (1d2ff37, 74e2f59): `confirm()` and `prompt()` pop-ups were blocked
  silently in embedded browsers.
- Sizes in TB above 1,000 GB (490d050).
- The core container stops with a 60 s grace period (30f91c1).
- Settings: `ATHENAFS_DORMANT_AFTER`, `ATHENAFS_HEALTH_PROBE`, `ATHENAFS_HEALTH_PARALLEL` added; `add_timeout` 45 s to 10 s.

### Fixed
- Distribyted crashed at start-up with many torrents (`concurrent map read and map write`) and served a folder from
  another one whose name it merely started with (3bf3a1c).
- A killed Distribyted left a dead FUSE mount and the share down; stale mounts are now cleared (30f91c1).
- Start-up failure introduced and fixed on 2026-10-21 (1d32791).

## Initial commit (185fcfc, 2026-10-21)
Patched Distribyted with a status and erase page, Samba share, cache cap, `up.sh` and `down.sh`.
