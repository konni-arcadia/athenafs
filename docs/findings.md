# athenafs: findings of the October 2026 tests

What was measured while building athenafs: the candidate tools (btfs, Distribyted), the patches that were needed, the web page,
the SMB share, and how a Mac and a Windows PC behave against it. Measurements were taken on a Debian 13 host on Wi-Fi, with a
Mac (macOS 26) and a Windows 11 PC as clients. Figures are from single runs on a home network: orders of magnitude, not benchmarks.

## Network baseline (test host, 2026-10-20)

Wi-Fi 5 GHz, channel 104, 540 Mbit/s negotiated, signal -52 dBm. Cloudflare: 38 MB/s down (~305 Mbit/s),
8 MB/s up (~65 Mbit/s). Gateway ping 2 ms, 0 % loss. archive.org: 3.4 MB/s, 3 s to first byte.
The Wi-Fi is not the limiting factor. (An earlier note about "3 kB/s" was a measurement error: the URL returned a 404.)

## Torrents used (metadata read on archive.org, 2026-10-20)

An Internet Archive item has its torrent at `https://archive.org/download/<id>/<id>_archive.torrent`. These torrents
fall back on the archive itself as a web seed, so they work without peers.

| Role | Item | Content | Size |
|---|---|---|---|
| modest zip, multi-file | `1601_0903_librivox` | 39 files: mp3, ogg, m4b, 1 zip (814 KB) in the torrent; public domain | 69 MB |
| many files, 7z of all sizes | `stackexchange` | 766 entries (incl. padding files), 371 `.7z` from 0.1 MB to 37 GB; licence to confirm (CC BY-SA according to Stack Exchange, the item's field is empty) | 99 GB |
| one big file, real swarm | Debian netinst | 1 iso | 792 MB |

Rejected: popular "public domain" 7z/RAR items on the Internet Archive (games, leaks): the licence label is
unreliable. No clean RAR found; one would have to be made (`rar` is in non-free).
The 9.4 MB zip described in the Librivox metadata is a derived file and is not in the torrent.

## Result 1: btfs (`experiments/btfs/`) — not retained

- Works: image, FUSE (`/dev/fuse`, `SYS_ADMIN`, apparmor unconfined), `allow_other`, `rshared` propagation; the 792 MB iso
  is listed from the host, read-only.
- Fails: reading one byte did not finish in 5 minutes; `unzip` on the 814 KB zip stayed blocked for more than 10 minutes
  while the container had received 27 MB (read-ahead far beyond the 814 KB asked) and then nothing.
- The blocked process stays in state D (uninterruptible FUSE read): `timeout` could not kill it, only stopping the container
  released it. Over SMB this would freeze Finder/Explorer.
- `--max-upload-rate=1` seemed to hold (0.8 MB sent for 27 MB received) but the measurement was short.

## Result 2: Distribyted v0.15.0 — retained

Configuration in `core/config.yaml`; cache limit (`global_cache_size`) and read timeout (`read_timeout: 60`)
exist, so no read freezes: a stuck read returns an error.

- Mount in a few seconds. A `.torrent` file gives an instant listing (383 entries for `stackexchange`); a magnet would first
  need the metadata.
- Archives: by default Distribyted shows a zip/7z/rar as a *folder* and extracts it itself (`cp -r name.zip/. dest/`).
  Zip 814 KB: content copied in 11 s. 7z `security.meta` (4.9 MB → 27 MB) in 7 s, `math.meta` (52.8 MB → 303 MB) in 35 s.
- 778 MB solid 7z (`tex.stackexchange.com.7z`, 4.9 GB uncompressed), full read:
  - cache limit 512 MB: **fails** at ~3.5 GB read, `sevenzip: lzma2: unexpected EOF` (`Input/output error`), 1.2 GB received for a 778 MB archive;
  - cache limit 2048 MB, empty cache at start: **works**, 136 s, 860 MB received, 745 MB cache, no error.
  - So **the cache limit must exceed the archive size** for a solid 7z. A 30 GB 7z needs ~30 GB of cache: the mount saves no disk in that case.
- Read-ahead is wide: 41 MB received for ~1.4 MB asked on a 69 MB torrent.
- After the container stops, the cache stays on disk (`sudo du`: 1.4 GB in `data/state/`, 27 MB for btfs). Nothing erases it
  by itself: use the web page below or `sudo rm -rf` (plan a dedicated volume with a purge in a real deployment).

### Upload

Decision of the owner (2026-10-20): **no upload cap** in the setup. Distribyted has no upload setting; `scripts/measure-upload.sh` caps the
container's outgoing traffic from the host (`tc tbf` on the container's `eth0`) and was only used to measure:

| Cap | Received | Download speed | Sent |
|---|---|---|---|
| none | 203 MB | 1180 kB/s | 1.8 MB (10 kB/s) |
| 128 kbit/s | 266 MB | 790 kB/s | 3.8 MB (11 kB/s) |
| 64 kbit/s | 241 MB | 850 kB/s | 1.8 MB (6 kB/s) |
| 24 kbit/s | 119 MB | 280 kB/s | 1.3 MB (3 kB/s); read not finished in 420 s |

Each cap was measured on a different archive and the swarm varies, so the comparison is noisy. Without a cap, outgoing
traffic is ~0.9 % of incoming: the order of magnitude of TCP acknowledgements and requests. I did not tell it apart from real piece
seeding (no packet capture). Usable floor: ~64 kbit/s (below, acknowledgements are throttled and the download collapses).
The container publishes no torrent port (Docker NAT), so there is no incoming connection.

## Web status and erase page (`core/admin.py`)

The page is served by `core/admin.py` on port 8080 (`http://<host>:8080/`; the same content as JSON on `/api`). How to start it: see the README.

- **Shows:** service state, disk cache and limit, container network received/sent since start (Distribyted's per-torrent
  counters stay at 0 and are not used), and for each mounted .torrent: size, cached pieces and MB, peers. Each torrent has a tree
  "Files with cached data" (folders collapsible, size ≈ cached pieces covering the file; the tree keeps its open folders across
  the 5 s refresh).
- **Actions:** "Erase" on a file or a folder, "Whole torrent", "Erase everything", each with a confirmation. Does not touch the
  .torrent files or the configuration.
- **How:** Distribyted stores its cache by piece hash; a torrent's hashes are in its .torrent. Erasing is done **with the
  service stopped** (erasing live breaks reads: "unexpected EOF", wrong cache counter): stop, delete the pieces, empty the
  piece-completion database, restart. ~10 s unavailable, reads in progress fail. A supervisor (`admin.py`) in the same
  container does stop/erase/restart, so no Docker socket is handed out.
- **Rule:** a piece straddling two files is erased only if **every** file it covers is chosen; otherwise it is kept (the message
  says how many) so that the neighbour is not damaged. Padding files (`.____padding_file/*`) are ignored.
- **Measured:** Librivox torrent 9.3 MB → erased → 0 → re-read in 8 s (re-downloaded), `stackexchange` untouched; "Erase
  everything": 1.4 GB → 264 KB on disk. Per file: after reading 3 files (conlang, security.meta, math.meta), erasing `math.meta`
  alone removed 13 pieces / 55 MB; re-reading the two others received 7 kB (served from the cache); re-reading `math.meta`
  received 63 MB. Folder logic checked on a synthetic torrent (edge pieces kept when the neighbour is not chosen), not yet on a
  real torrent with sub-folders.
- **Limits:** one click = one restart (no multi-select); a file's size is approximate (includes shared edge pieces); at most
  300 entries shown per level. Small files that share a piece with their neighbours cannot be erased one by one: erasing
  `Sintel.es.srt` (a subtitle in a torrent of 11 files) erased 0 pieces and said "1 edge piece kept"; the neighbours stayed
  served from the cache. Choose the neighbours too (or the whole folder/torrent) to free those pieces.
- **Access:** port 8080 is published on all of the host's interfaces, **without a password** (the owner's decision; anyone on the
  local network can erase the cache). A first version had Basic authentication; a file `/run/admin-password` (mounted by hand) re-enables it.
  POST requests still require an origin identical to the page (anti-CSRF). Reached from a Mac (checked over SSH).
  No `ufw` firewall detected on the host.
- **Trap found:** mounting FUSE directly on the host-bound mount point makes an `umount` inside the container also remove
  the host's bind (`rshared` propagation); hence the `fs` sub-folder.

## Managing torrents from the page (2026-10-21)

The page also adds and removes torrents (top of the page, and the "Sources" table). The list is kept in
`/data/sources.json` (inside `data/state/`); on each start `admin.py` generates the Distribyted configuration from
`core/config.yaml` (everything above `routes:`) plus that list. The first run imports the routes of `core/config.yaml`.

- **Add:** magnet links or URLs of .torrent files (one per line, several in one go: one restart for the batch), or a .torrent
  file chosen on the page. Optional folder name (default: the
  torrent's name). One folder per torrent under the share root. A duplicate (same info-hash) is refused.
- **Remove:** "Remove" (also erases its cached data) or "Remove, keep data". Removing a torrent does not delete the .torrent
  files shipped in `/torrents` (read-only), only the ones added through the page.
- **Magnets and the cache:** Distribyted keeps no metadata for magnets, so a local patch (`core/patches/metainfo_dump.go`) saves the
  metadata of every torrent as soon as it is obtained, to `/data/metainfo/<info-hash>.torrent`. That lets the page attribute a
  magnet's cached pieces to it (shown in the tree, erasable, removed with the torrent). Before this, magnet pieces were
  "unknown" and only "Erase everything" removed them. The same file also starts the magnet afterwards: once it is saved (and
  parses to the right info-hash), the generated configuration uses it as `torrent_path` instead of the magnet link. Measured
  with 64 torrents (62 magnets): about 2 minutes to mount when every magnet was looked up again at each start, 3 s from the
  saved files.
- **Safety of the URL option:** the page is open on the local network, so a URL is fetched only if it is http(s) and its host
  (and every redirect) resolves to a public address; at most 10 MB; the content is parsed as a torrent before being kept.
  A URL on the local network was refused.
- **Measured:** URL of a .torrent (Big Buck Bunny, archive.org) added and mounted; a .torrent file posted (Debian netinst);
  a magnet of the same Debian torrent refused as a duplicate; an invalid magnet refused; a real magnet (Sintel, CC-BY) resolved
  and mounted within seconds, 11 files in a real sub-folder, its cache attributed (131 kB, "unknown" stayed 0), and its removal
  erased exactly its pieces and its saved metadata. The test torrents were then removed.
- **Limits:** every add/remove restarts the service (~10 s). Each magnet whose metadata has never been found delays the mount
  by `add_timeout` (45 s in `core/config.yaml`) at every start, until it is found once. Folder names are sanitised (`A-Z a-z 0-9 . _ -`). No progress display for
  a download; no way to rename a folder (remove and add again).

## Many torrents: two Distribyted bugs (2026-10-21)

Adding 62 magnets at once (64 torrents in all) exposed two bugs in Distribyted v0.15.0, both patched in `core/Dockerfile`:
- **Crash at start-up:** `fatal error: concurrent map read and map write` in `fs/storage.go`. `Torrent.load()` filled its file
  table under a *read* lock, so two first accesses at the same time (start-up and a FUSE request) wrote the same map. The
  longer load with many torrents makes the overlap likely. Patch `core/patches/torrent_load_lock.patch`: write lock in
  `load()`, read lock in `Open`/`ReadDir`.
- **Folders that cannot be opened:** a path was routed to the first filesystem whose name it merely *starts with*, so
  `name-13` could be served by `name-1` or `name` (Go map order is random): 38 of the 64 folders listed but failed with
  "No such file or directory". Patch `core/patches/fs_prefix_match.patch`: a path belongs to a filesystem only if it is that
  path or lies under it. This affects any two folder names where one starts the other, not only batches.
- **The page said "running" over a dead process.** `admin.py` now has a watchdog: if Distribyted exits on its own it says so and
  restarts it, and gives up after 3 exits in 10 minutes. Checked by killing the process (`kill -9`): restarted, all mounted.

After the patches: 64/64 mounted, every top-level folder opens, no crash over several restarts.

## Several torrents in one folder (2026-10-21)

A collection split into many torrents (here 62 parts, all named `BigCollection`, all starting with the same directories)
was served as 62 folders `BigCollection`, `BigCollection-2`… Distribyted can serve several torrents in one route and
overlays their file trees, so:
- **A new source joins an existing folder of the same name** instead of getting a `-2` suffix. The folder name is the one typed
  on the page (it applies to the whole batch), otherwise the torrent's name (`dn=` for a magnet, 8 characters of the hash
  without it). Same name means merged, whatever the content: two unrelated torrents with the same generic name are merged too.
- **Same path in two torrents of a folder:** Distribyted serves only one of them (the first loaded, which is not stable). The
  page counts these on the source's line. The 62 parts here share no path (4,834 distinct paths, about 836 GB).
- **No doubled name:** a multi-file torrent showed its own top-level directory inside its folder
  (`BigCollection/BigCollection/…`). Patch `core/patches/flatten_single_root.patch`: when every torrent of a folder is a
  multi-file torrent with the same top-level directory (one torrent, or a collection in parts), that level is skipped,
  whatever the folder is called (a first version compared the folder's name with the torrent's, which a rename would have
  broken). A folder holding torrents with different roots keeps one sub-folder per root. `admin.py` (`share_path`) applies
  the same rule for the paths it shows.
- **Rename and move:** "Rename" on a folder renames it in the share (one restart); a name that already exists merges the two.
  "Move" on one torrent puts it in another folder. A renamed folder leaves a forwarding entry (`/data/aliases.json`): a torrent
  added later without a folder name, whose own name is the old one, goes to the new folder, so the next parts of a renamed
  collection join it. A folder name typed on the page is taken as is.

Measured: 64/64 mounted; `BigCollection/T-En Collection` holds 61 sub-folders coming from different torrents; a file
read twice gives the same checksum; the Samba container sees the same tree.

## Hundreds of torrents (2026-10-21)

With 526 sources (524 parts of one collection), starting was the problem, not serving:
- Distribyted added the torrents of its configuration **one by one, before mounting anything**. Each unknown magnet waited
  up to `add_timeout`, each .torrent file took a fraction of a second: 462 new magnets meant tens of minutes without a share,
  and 520 known torrents about 100 s.
  Patch `core/patches/parallel_load.patch`: added in parallel (128 at a time) **and in the background**; the share is mounted
  at once and each folder fills in as its torrents arrive. The same patch makes an empty folder exist (empty) instead of
  "No such file or directory" while it waits.
- A magnet still without metadata after `add_timeout` was dropped until the next restart. Patch
  `core/patches/late_metadata.patch`: it keeps waiting in the background and joins its folder when the metadata arrives
  (measured: 107 magnets arrived late this way, 520/526 without a restart). `add_timeout` is now 10 s.
- **The real limit is the BitTorrent client.** Every torrent runs its own DHT announcer and one scraper per tracker, all
  behind the client's single lock. Goroutine dump with 526 torrents: 486 DHT announcers and about 300 tracker tasks waiting
  for that lock. Adding them all then takes about 1.5 minutes after each start (the stats call takes seconds meanwhile, and
  the container uses about 1.7 cores); at rest it uses about 15 % of a core and 4.8 GB of memory. Nothing is wrong, it is just a lot of torrents for one client. Solved by dormant torrents (below).
- The supervisor got slow with them too (9 s per refresh): magnet links were parsed again at every refresh and conflicts
  recomputed over every file. Both are cached now (0.14 s). The page lists at most 200 torrents per folder (all are in `/api`).

## Performance with 636 torrents: supervisor and dormant torrents (2026-10-21)

Measured with `scripts/measure.sh`, one row per commit in `docs/measurements.md` (dataset: 636 torrents, 12.5 million
pieces, 443,000 files, 258 TB).
- **Supervisor** (ac8c357): piece hashes as one 20-byte-per-piece block per torrent instead of lists and sets of strings;
  a piece -> (torrent, index) table in SQLite (1.2 GB, built once in about 3.5 minutes, then per torrent) so that status,
  cache tree, activity and search only look at the cached pieces; status and page computed every 3 s in the background.
  Supervisor memory 13.9 GB -> 0.9 GB, `/api` and page from no answer in 120 s to instant, search 13.7 s -> 0.01 s.
- **Dormant torrents** (bb49ec7, patch `dormant_torrents.patch`): a torrent given as a .torrent file is served from its
  metadata, folders list without the client, and the torrent enters the client only when one of its files is opened; it
  leaves again 600 s (`ATHENAFS_DORMANT_AFTER`) after its last file was closed. Distribyted 13.8 GB -> 1.0 GB, CPU at rest
  85 % -> 9 %, every torrent in its folder 10 s after a start (210 s before). Checked: listing never wakes a torrent; a read
  wakes it (37 peers, a 20-60 MB file in 15 s including the wake); it went back to sleep 80 s after the read with a 60 s
  delay; read again afterwards it came from the cache (3 kB received). The storage `Close` of a dropped torrent is a no-op,
  the cache is shared. Dormant torrents do not seed. Magnets still without metadata stay in the client until it arrives.
  The share was checked from a Mac with this patch (browsing and copying: fine).
- **Health check** (8b16084, patch `health_check.patch`): dormant torrents are woken in turn, 2 at a time for 60 s
  (`ATHENAFS_HEALTH_PARALLEL`, `ATHENAFS_HEALTH_PROBE`, 0 = off), without opening a file, so nothing is downloaded; the most
  peers known, connected and seeders seen are kept in SQLite by the supervisor, which also writes the order of the next
  checks (never or least recently checked first). A full round of 636 torrents takes about 5 h. A torrent that wants no
  data seldom connects: first test, 71 of 88 had peers announced (13 to 77) and none connected, so "peers announced" is the
  signal and only "no peer found at all" counts as dead (warning when such files are queued for the cache). Cost measured:
  none visible (CPU at rest 9 %, Distribyted 1.1 GB, with two probes running).
- **Incident:** a later edit dropped a module-level line of `admin.py` (`PAGE_URL`); the core exited at start and the
  share was down until the fix (58de59e). The test build predated that edit: rebuild after the last edit.
- **Incident:** Docker's default 10 s stop timeout killed Distribyted mid-stop and left a dead FUSE mount (the share was
  down). `stop_grace_period: 60s`, stale FUSE mounts cleared by the supervisor and `up.sh`.

## Activity, search and other page features (2026-10-21)

- **Rates:** Distribyted's per-torrent figures are deltas between two API calls, reset by every caller, and report 0 peers
  when `/status` was called less than 2 s before `/routes`. Patch `core/patches/stats_totals.patch` adds cumulative byte
  counters and always fills peers; `admin.py` samples them every 2 s (it is the only regular caller) and computes the rates.
- **Files receiving data:** each new piece in the cache is mapped to its torrent and file(s) (a piece can span several);
  the page and `/api/activity` list the files that received data in the last 30 s. Measured: a 311 MB read at about
  17 MB/s from 14 peers, the right file shown, plus the neighbour sharing its first piece.
- **Search:** SQLite FTS5 with the trigram tokenizer (`/data/search.db`), built from the .torrent files only: nothing is
  downloaded, archives are not opened. Reindexed per torrent when one is added, removed, moved, or when its layout changes.
  Substring match, every word must match, case-insensitive; a word shorter than 3 characters falls back to a plain scan.
  Synthetic test with 2 million paths: indexing 31 s (421 MB), a precise query 5 to 20 ms, a broad one (125,000 matches,
  count capped at 10,000) about 0.4 s, a short-word scan about 0.5 s. `GET /api/search?q=…&limit=…&offset=…`.
- **State in the search and "add to cache":** each result says cached / partial (pieces present over pieces covering the
  file) / not cached, or downloading (pieces arrived in the last 30 s), queued or caching. "Cache" reads the file through the
  mount in the supervisor (2 readers, 1 MB reads, 3 attempts), so Distribyted downloads it as for any client; no patch.
  A batch (a file, every result of a search, or `folder:path`) is refused when it would not fit in 90 % of the cache cap:
  its first files would be evicted before the last are read. Measured: a 188 MB file cached in about 90 s, then read again
  while the container received under 1 MB (background traffic of 520 torrents). `folder:BigCollection` (first 10,000
  files, 2.8 TB) refused against a 478 GB cap.
- **Links at the root of the share:** `athenafs web page.webloc` (macOS) and `athenafs web page.url` (Windows), pointing to
  `ATHENAFS_PAGE_URL` (by default `http://<this host>.local:<port>/`, set by `scripts/up.sh`). Distribyted serves the files
  of `$DISTRIBYTED_ROOT_FILES` at the root of the mount (`core/patches/root_files.go`), read at each start.
- **Build:** Go modules are downloaded in their own layer before the patches; two builds had failed on a DNS/IPv6 hiccup of
  the host while downloading them.

## API (2026-10-21)

`/api/docs` lists the endpoints (one table in `admin.py`, `API_DOC`, also served as `/api/openapi.json`).
- **Locations:** every file, folder or torrent root comes with `smb_url` (percent-encoded, opens in the Finder) and
  `unc_path` (Windows), built from `ATHENAFS_SMB_HOST` or the host of `ATHENAFS_PAGE_URL`, and the share name (`torrents`).
- **Usage:** `/api/usage?by=file|folder|torrent` adds the cached pieces per file (a piece spanning two files is split by
  bytes), per share folder cut at `depth` levels, or per torrent; computed from the cached pieces only.
- **Erase by share path:** a file or folder of the share is mapped back to (torrent, path inside it) through the index; a
  folder that holds whole torrents erases them whole; several torrents are erased with one restart. Checked: a sub-folder
  of a large torrent maps to that torrent and its inner folder, `librivox` to the whole torrent, a file to itself; a file erased through the API
  went from cached to partial (its two edge pieces are shared with neighbouring files).
- **CSRF:** POST requests accept a matching `Origin` (the page) or `X-Athenafs: 1` (scripts); without either, 401. A third-
  party site cannot add a custom header without the browser asking this server first, which it does not allow.
- **Confirmations:** `confirm()` pop-ups were blocked silently in an embedded browser, so "Cache all" did nothing; every
  confirmation is now a second click on the same button, and the 5 s refresh waits while one is armed.

## Security hardening (2026-10-21)

- **Capabilities measured, not guessed.** Both containers start from `cap_drop: [ALL]` with `no-new-privileges`. Core:
  `SYS_ADMIN` (FUSE mount) and `DAC_OVERRIDE` (without it: `Permission denied` writing `/data`, which belongs to the host
  user); a read waking a dormant torrent, an erase and a clean stop all worked with that. Samba, removing one capability at
  a time from a generous set: without `CHOWN` or `SETGID` the container exits (account creation), without `SETUID` reads
  fail; `DAC_OVERRIDE`, `FOWNER` and `NET_BIND_SERVICE` were not needed (Docker lets port 445 be opened inside the
  container). Kept: `CHOWN`, `SETUID`, `SETGID`. A wrong password is still refused.
- **sudo:** the self bind mount of `DATA/mount` is only needed where `/` is not shared; this host (systemd) has it shared,
  and `up.sh` now starts without sudo. The dead-FUSE-mount cleanup moved to the core (it unmounts at start and that
  propagates to the host).
- **Warnings:** `up.sh` detects the page exposed without a password and the share exposed with `tor` / `rent`, prints
  them and passes them to the page and `/api/status`.
- **Path guard:** a torrent can name a file `../x`; the prefetch reader refuses a path that leaves the share.
- AppArmor stays unconfined for the core: a profile allowing only the FUSE mount would need loading on the host (root),
  which is not "easy to avoid". WebDAV (built into Distribyted) would remove FUSE and its privileges altogether; not done.

## Importing ~400 magnets without trackers (2026-10-22)

The import of 409 magnets brought `BigCollection` to 1049 torrents (1051 sources). Their metadata came in at about 3 a
minute, then 1 in 2 minutes. Cause: 351 of the waiting magnets had no tracker (`xt` and `dn` only), so the DHT was their
only way to find peers, and the client sends at most 25 DHT queries a second (`dht.DefaultSendLimiter`), for everything:
a DHT lookup takes tens to hundreds of queries, the health checks and awake torrents use the same budget. Behind it, the
client also opens at most 100 connections at a time and 10 new ones a second (library defaults, kept by Distribyted).
The 640 older magnets of the folder all carried the same 38 trackers.
- **Folder trackers:** when the configuration is generated, a magnet without tracker gets the trackers found in at least
  half of its folder's magnets, plus `ATHENAFS_EXTRA_TRACKERS`; `sources.json` is not changed. Distribyted saves resolved
  torrents with those trackers, so later wake-ups benefit too. Measured right after: 93 magnets resolved in 3 minutes,
  against 1 in the 2 minutes before.
- Not done: raising the DHT query limit (a Go patch) and the connection limits; to consider if trackerless magnets of a
  folder with no trackers to borrow ever need it.
- **Resolved magnets stayed awake:** a magnet whose metadata arrives while running was kept in the client until the next
  restart (only torrents loaded from a .torrent at start were dormant): 343 awake after the import, Distribyted at 8.6 GB
  and 27 % CPU. Patch `sleep_after_metadata.patch`: the metadata is saved and the torrent put to sleep at once. After the
  restart that deployed it: 1036 mounted, 0 awake, Distribyted 3.7 GB, 14 % CPU.
- **Incident:** the Dockerfile check after the `dumpMetainfo` sed counted every `dumpMetainfo(t)` in the file and failed
  once the new patch added a legitimate second call; the rebuild loop then waited for a service that was not starting, and
  the share was down about 12 minutes. The check now counts only the line the sed writes. Lesson: a build that fails must
  stop the wait, not extend it.

## Cache cap and disk space (2026-10-21)

The cap on the cache (`global_cache_size`, in MB, in `core/config.yaml`) is now shown against the disk and can be changed from the
page (form at the top, "Cache cap"). The disk figures are those of the filesystem holding the cache (`shutil.disk_usage('/data')`
inside the container, i.e. the host's root filesystem: 687 GB free of 971 GB when measured).

- **Shown:** disk free / total, the largest cap possible now, the cap in force.
- **Largest cap possible** = free space + what the cache already occupies − 1 GB margin (the cache's own bytes count as
  available, since replacing them frees them).
- **Fixed:** a cap in GB, accepted from 0.3 GB up to that maximum; anything above is refused with the real maximum in the message.
- **Automatic:** keep a percentage of the **whole disk** free once the cache is full: cap = free space + cache − (reserve % of the
  disk's total size). Default 20 %, accepted from 1 to 90 %. It is computed **at each (re)start** and then fixed while the
  service runs: the free space can shrink in between if something else fills the disk. (A first version used a reserve in GB;
  a percentage follows the size of the disk, so it replaced it.)
- **Stored** in `/data/settings.json` (overrides `core/config.yaml`, which keeps the initial default). Applying restarts the service.
- **Measured:** fixed 30 GB → 30 000 MB in force; fixed 5000 GB → refused ("between 0.3 GB and 686.1 GB right now");
  automatic with a 100 GB reserve (first version) → 587 099 MB in force (687.1 + 0.35 − 100); automatic with 20 % →
  492.8 GB in force on a 971 GB disk with 687 GB free (687.1 + 0.35 − 194.3); 95 % refused ("between 1 and 90 %").
- **Remember:** a solid 7z needs a cap larger than the archive; the page now shows how far the cap can go.

## SMB share (`smb/`)

Samba in a container, read-only, port 445, account **`tor` / `rent`** by default (`SMB_USER`/`SMB_PASS`; guest access is off).
The share `torrents` is the FUSE mount (`data/mount/fs`), bound with `rslave` propagation so it sees the remounts made when the
service restarts. From a Mac: `mount_smbfs //tor:rent@<host-ip>/torrents <dir>` (or Finder, Go > Connect to Server,
`smb://<host-ip>/torrents`, user `tor`). From Windows: `net use \\\\<host-ip>\\torrents /user:tor rent`, or Explorer.

Two problems had to be fixed in Distribyted (local patches in `core/Dockerfile`):
1. **Samba refused the share** (`NT_STATUS_OBJECT_PATH_NOT_FOUND`; the Mac saw "No such file or directory"): Distribyted's
   FUSE `Getattr` leaves the link count at 0 and Samba treats a 0-link object as deleted. Patch: folders 2, files 1.
   (The same share on an ordinary folder mounted fine from the Mac.)
2. **Opening archives as folders is too expensive for a file browser** (next section). Patch: `DISTRIBYTED_ARCHIVES=off`
   makes .zip/.rar/.7z ordinary files.

Samba options worth keeping: `veto files` for what Finder/Explorer drop or read (`.DS_Store`, `._*`, `.Spotlight-V100`,
`Thumbs.db`, `desktop.ini`, …), oplocks off, `vfs_fruit` for the Mac.

### Traffic triggered by a browser, measured from the Mac (macOS 26.5, `mount_smbfs` over SSH, each test alone, cache emptied first)

Archives opened as folders (default Distribyted):

| Action on the Mac | Received by the torrent |
|---|---|
| `ls` of names only (383 entries) | 9 kB |
| `ls -l` of the 383 entries (stat of each) | 18 kB |
| `ls -l` inside **one** 2.4 MB 7z | 4.9 MB |
| `ls -lR` over 10 of the 7z | **101 MB** (~10 MB per archive) |
| `mdls` (Spotlight metadata, "Get Info") on 3 archives | 18 MB |
| QuickLook thumbnail (`qlmanage -t`) of a 0.5 MB 7z | 6.5 MB, and the command ran 209 s |

Estimate: a recursive listing, indexing or folder-size computation over a torrent with hundreds of archives costs gigabytes.

Archives as ordinary files (`DISTRIBYTED_ARCHIVES=off`, the setting now in the compose file):

| Action on the Mac | Received by the torrent |
|---|---|
| `ls -lR` of the **whole share** (435 lines) | 26 kB (19 s) |
| `mdls` on 3 archives | 4 kB |
| QuickLook thumbnail of a 0.5 MB 7z | 151 kB (but the command still ran until my 200 s guard) |
| QuickLook thumbnail of a 110 kB jpg | 3 kB (file already cached, not a fair measure) |

Also checked: writing to the share is refused ("Read-only file system"); a cached file reads in 0.4 s.

### Tested as a user would (Mac and Windows, archives as ordinary files, 2026-10-21)

| Test | Result |
|---|---|
| Mac: extract the 814 KB zip straight from the share (`unzip`, then `ditto -x -k` = Archive Utility) | works; 15 s cold (1.1 MB received), 1 s from cache |
| Mac: copy a 156 MB 7z, check SHA-1 against the Internet Archive's | exact size and SHA-1; 90 s (~1.7 MB/s), 228 MB received by the torrent (read-ahead overhead ~46 %) |
| Mac: copy a 179 MB 7z, **service restarted in the middle** (erase from the web page at ~52 MB) | `cp` fails with an error (exit 1) instead of hanging; partial file left; the share is usable again once the service is back **without remounting**; a new copy then succeeds (78 s, SHA-1 exact) |
| Windows 11: `dir`/`Get-ChildItem -Recurse` over the whole share (422 entries) | 5 kB received, under 1 s |
| Windows: copy the zip, `Expand-Archive` | works (819 426 bytes extracted); the first attempt failed after 152 s, right after a service restart (see "cold start" below) |
| Windows: copy a 131 MB 7z, SHA-1 | exact size and SHA-1; 60 s (~2.2 MB/s), 165 MB received |

**Cold start:** just after the service (re)starts, the first read waits for the swarm; one Windows read ran 152 s and then
failed (`read_timeout: 60` in `core/config.yaml` applies per read). A retry worked. Plan for this after an erase/restart.

**Credentials (2026-10-21, the owner's choice):** the guest share worked from Windows only because Windows presented the
logged-in user (`net use … /user:guest` was refused by policy, error 1272). Guest access is now off and the share needs the
account `tor` / `rent`. Checked: Mac mounts with `tor`/`rent`, refuses guest and a wrong password, write still refused
(read-only); Windows connects with `tor`/`rent` and refuses a wrong password. These are test credentials for the local
network only.

### Not tested yet
Finder and Windows Explorer themselves (previews, column view, "Get Info" on a big folder, thumbnails), the 30 GB 7z
case over SMB (copy first, then extract locally), a real torrent with sub-folders for the folder-level erase.

## Decisions and open points
- Retained: Distribyted. Rejected: btfs (uninterruptible blocked reads), kept in `experiments/btfs/`.
- No upload cap. No password on the status page by default for a trusted network (it binds to localhost unless `ATHENAFS_BIND`
  is changed). SMB account `tor` / `rent` by default (test credentials).
- Keep `ATHENAFS_ARCHIVES=off` (archives as ordinary files) for any share used by a file browser; switch to `on` only for direct
  tooling on the host (`cp -r archive.zip/. dest/`).
- Trap found during the tests: a `docker compose up` run without the data directory variable made Docker create empty
  directories at the host root; the compose file now has a default for `DATA`.

**Where to pick up (2026-10-23, after a long session; read this first):**
- Running: commit `dd43b35` (code `4fd91e9`), 1051 sources (1040 mounted, 11 without metadata), ready view
  `BigCollection_READY` on, gentleman mode on with 3 slots (`dde7a316`, `363d8498` since 2026-10-22 18:00 UTC,
  `32f20bd8` since 18:18 UTC). Nothing needs this session: service, health checks and gentleman mode run on their own.
- Next, in this order:
  0. Done (2026-10-23, not committed yet): cache requests kept in SQLite, waiting for a seeder instead of failing,
     torrents kept awake within a shared memory budget and a ceiling; see "Cache requests" in the open points. To watch:
     the first real arrival, `GET /api/waiting` (`memory_log`) and the `wait_log` table after a few days, to see how often
     seeders show up and whether 2000 MB and 8000 MB are right.
  1. Gentleman mode: understand "pieces held > pieces asked for" (`torrent/gentleman.go` in `core/patches/gentleman.patch`:
     `Complete` counts every complete piece of the torrent, `Wanted` the ones set to normal priority; check whether the
     client fetches more, e.g. through the readahead of a reader or the first-piece request) and the memory growth of
     helped torrents over hours (measure Distribyted's RSS against the pieces and peers of each helped torrent).
  2. Check the first rotation: slots end after 24 h (from about 2026-10-23 18:00 UTC) or at 3 seeders; released torrents
     must cancel their requests, sleep, and stay out for 24 h (`/data/gentleman_state.json`, `GET /api/gentleman`).
  3. Done: the repository is public since 2026-10-23, release `v0.1.0` (tag on 56f47aa).
  4. Ideas below (AppArmor, Windows test, patches upstream; WebDAV mode shelved by the owner).
- How to check the state: `curl -s localhost:8080/api/status`, `/api/gentleman`, `/api/folders`; Distribyted's memory:
  `docker exec athenafs-core sh -c 'grep VmRSS /proc/$(pidof distribyted)/status'`. Measure with `scripts/measure.sh`
  after committing (see CLAUDE.md).

Open points (2026-10-22):
- **Release, stage 1:** done: base images pinned by digest, Distribyted by commit, `CHANGELOG.md`, release `v0.1.0`
  published with the repository on 2026-10-23 (install tested from a fresh clone with no `.env` the same day). The `apk`
  and `apt` packages installed during the build are not pinned. Stage 2 (images built by CI and published to a
  registry) only if other people install it; a published image must point to its corresponding source (GPL), which the
  public repository and its pinned commits provide, and carry the licences of what it contains.
- **WebDAV mode (idea, shelved by the owner on 2026-10-23: not to be started unless asked):** Distribyted has a WebDAV server; serving through it instead of FUSE + Samba would remove
  `/dev/fuse`, `SYS_ADMIN` and AppArmor unconfined. To try first: Finder over WebDAV on the full set. The prefetch reader
  reads through the FUSE mount and would need another path. Windows' WebDAV client is weak (50 MB file limit by default).
- **AppArmor:** a profile allowing only the FUSE mount instead of `unconfined` (needs loading on the host).
- **Not tested:** Windows (SMB and the `\\host\share` locations), Finder and Explorer previews (a preview that reads a
  file wakes its torrent).
- **Watch:** CPU at rest measured 9 to 19 % across the last rows with the same code paths (health probes running); if it
  keeps rising, look at it.
- **Browsing archives over SMB (idea, agreed with the owner 2026-10-22, not started):** show the content of zip, 7z and rar
  archives only for archives that are **100 % in the cache**, so that no client (Finder previews, Explorer thumbnails,
  `find`, backups) can trigger a download by enumerating them. Opening a partly cached archive would cost at least one
  piece (16 to 64 MB on the large collections) and wake its torrent; a folder of 700 zips walked by a client would be about
  11 GB.
  - **Per folder, set on the page** (Folders section): normal view on/off, archive view on/off, name of the archive view
    (default `<folder>_ZIP` or similar, editable, sanitised, refused if it collides). The archive view is a sibling folder
    at the root of the share, same tree, complete archives only, each opened as a folder. Applying a change restarts the
    core, like a rename.
  - **"Local library" mode:** normal view off, archive view on: the share shows only what is ready (for a player or another
    device), while the page and the API keep the whole catalogue to choose and cache from.
  - In the normal view an archive stays an ordinary file: nothing ever changes type (Finder and Explorer handle it badly).
  - Renaming a folder renames its archive view too, unless that name was customised (confirmed by the owner).
  - The supervisor already knows which files are complete (the "cached" state) and would write the list per folder for
    Distribyted, which already opens archives and would get a patch for the views and the filter. An archive evicted from
    the cache leaves its view at the next refresh; a read in progress just downloads what is missing (the rule decides what
    is listed, not what may be read). Solid 7z/rar are fine since they are local; reading a large compressed member stays
    slow (deflate from the member's start).
  - Not wanted for now: feeding the files inside archives to the search (the owner is not convinced).
  - Workflow: search on the page, "Cache", then browse the archive view over SMB.
  - Before building it: measure Finder on an archive view with a few hundred complete archives.
  - **First attempt (2026-10-22), with Distribyted's own archive mode, not usable:** the 706 small zips of one
    collection (296 MB) were cached with "Cache all" (131 s, no failure), then the core was restarted with
    `ATHENAFS_ARCHIVES=on`. Over 8 minutes the share root never listed (`ls` stuck in uninterruptible wait), Distribyted
    used about 1.3 cores and 3.2 GB, and 17 MB were received (health checks included: no download storm, but no answer).
    That mode makes every zip of every torrent an archive filesystem (about 447,000 files here) and every lookup walks all
    of them (`getFileFromFs` is a linear scan over the filesystems, the same scan patched for folder prefixes). So the
    archive view has to be our own: only the complete archives, with an indexed lookup, never Distribyted's global mode.
    The Finder measurement will be made on a first prototype of that view.
  - **Prototype (2026-10-22), built and kept:** patches `fs_indexed_lookup` (a lookup walks up the path's parents instead
    of scanning every mounted filesystem) and `archive_views` (one filesystem per view, built from the list
    `/data/archive-views.json` that the supervisor writes every 30 s; Distribyted's own zip, 7z and rar readers; an
    archive's file is closed 30 s after its last read so its torrent can go back to sleep). The supervisor finds the complete
    archives from the cached pieces (cheap) and keeps the per-folder settings in `/data/archive_view_settings.json`.
    Measured with the 706 cached zips of that collection (732 complete archives in the view in all): the share root
    answers at once; after a start the view climbs to 732/732 in about 45 s (torrents are still being added when it is
    first built, so it retries until every archive opens); a recursive SMB walk of the 706 zips, each opened, took 1.0 s and
    received 20 kB (background); a file read through the view is identical (SHA-256) to the one in the original zip; the
    owner's Finder test from a Mac (views, opening zips, Quick Look, copying a file) worked and received 6.3 MB in 2.6 min,
    the background rate of the health checks, with no piece downloaded. Turn on, rename, turn off and the view following
    a folder rename were checked from the page and the API.
  - **Ready views and hidden folders (2026-10-22), agreed and built:** the view shows every complete file, not only the
    archives (otherwise a hidden folder's complete disc images or manuals would vanish from the share); default name
    `<folder>_READY` (the prototype's `_ZIP` setting migrated). "Normal view: hidden" is applied by Samba only: the
    supervisor writes `/data/smb/hidden-folders.txt`, the Samba entrypoint rebuilds its `veto files` line (the static
    preview/index list plus those names) and runs `smbcontrol smbd reload-config` within 3 s; the folder stays in the mount,
    so search, Cache (which reads through the mount) and its ready view keep working, and the API gives its items no SMB
    address. Checked with `librivox` (entirely cached: m4b, xml, gz and one zip): its ready view showed 34 files, the zip as a
    folder and the rest as they are; hidden, the share root listed `librivox_READY` but not `librivox`, opening the folder or
    a file in it failed (`NT_STATUS_OBJECT_NAME_NOT_FOUND`, `NT_STATUS_OBJECT_PATH_NOT_FOUND`), Cache still read a file of
    it, and "Show in the share" from the page brought it back within seconds. Known quirk: Samba hides by name, so a
    sub-folder anywhere with exactly the hidden folder's name is hidden too.
  - Later, only if needed: an archive view as a separate SMB share with its own account. The share name itself is already
    a setting (`SMB_SHARE`).
- **Page, folder rows (agreed 2026-10-22, built in 58d9305, merged in 18dd99d):** one compact row per folder (name, torrents, size, status, and
  badges only when relevant: hidden, ready view, waiting for metadata, no peer) and one "Manage" button opening a panel in
  three blocks: "In the share" (normal view shown/hidden, ready view and its name and links, one sentence on the local
  library mode), "Folder" (rename, browse, the list of torrents), and a "Danger zone" framed apart (erase its cache,
  remove, remove keeping the data). Rename moves into the panel (owner's choice). Long explanations once in the panel,
  not as tooltips on every button. To do after the ~1000-torrent import is measured.
- **Gentleman mode (built 2026-10-22; owner's choices: rarest pieces directly, on by default, settings on the page):**
  - **Swarm data:** the supervisor scrapes the trackers itself (BEP 15 UDP, 70 info-hashes per request, and HTTP scrape)
    every 6 h, up to 6 trackers per torrent, keeping the best answer. First run: 58 s, 4 of 38 trackers answered, covering
    636 of 1036 torrents: 14 with no seeder (all with leechers waiting), 13 with 1, 22 with 2, 41 with 3-4, 546 with 5 or
    more; 35 "in difficulty" (1-2 seeders, leechers waiting), the most striking with 1 seeder and 769 leechers.
  - **Choice:** 1 or 2 seeders and at least one leecher (0 seeders: nothing left to fetch), ranked by leechers per seeder;
    3 slots; a torrent leaves at 3 seeders or after 24 h and waits 24 h before it can come back.
  - **In Distribyted** (patch `gentleman`): a helped torrent must be dormant; it is woken with a cache of its own
    (`/data/metadata/gentleman-cache`, capacity 5 % of the cache cap, a second file cache: normal use never evicts it and
    it never evicts normal use) and held awake. One piece is asked for so that peers are contacted; every 2 minutes the
    connected peers' bitfields are counted and the rarest pieces not held yet are asked for, within the budget. Released
    torrents cancel their requests and sleep after the usual delay; their pieces stay in the gentleman cache until
    evicted by others. The supervisor's own cache figures do not include that cache.
  - **Upload cap** (patch `upload_limit`): a rate limiter on the whole client, 1 MB/s by default.
  - **First run (2026-10-22):** the three torrents chosen were among the largest of the collection (475,087 pieces of
    64 MB, about 30 TB; 177,754 and 99,994 pieces of 16 MB). Within 10 minutes each was connected to 30 to 50 peers
    (none before), 115 to 463 rarest pieces were asked for each, 470 MB had arrived in the gentleman cache, nothing
    uploaded yet. But Distribyted grew from about 3.4 to 6.0 GB: an awake torrent costs about **3.5 kB of memory per
    piece** (2.6 GB for these 753,000 pieces), so the "20 MB per awake torrent" average of small torrents does not hold.
  - **After 8.6 hours (2026-10-23 morning, commit 4fd91e9):** 14.4 GB uploaded in all (1.7, 6.1 and 6.6 GB for
    `dde7a316`, `363d8498`, `32f20bd8`); gentleman cache 21.3 GB of 23 GB; two of the three now show 0 seeders and 581 and
    13 leechers at the trackers, so the rare pieces held here may be among the only copies left. **Not understood yet:**
    two torrents hold more pieces than were asked for (780 and 549 for 455): the client downloads beyond the pieces set
    to normal priority, or the "held" count includes pieces it should not; the separate cache caps the space, but the
    budget logic depends on it. **Memory creeps up:** Distribyted 4.1 GB at the measurement, 5.5 GB 8 hours later, CPU 21 %
    (three torrents talking to 1 to 46 peers continuously); not covered by the 3.5 kB per piece estimate.
  - **"Pieces held > pieces asked for", explained (2026-10-23):** the page showed 1193/441, 128/441 and 350/441. On disk
    the gentleman cache (cap 22.2 GB) held 1062, 85 and 164 complete pieces (21 GB); the counters added up to 1671 (28 GB).
    Two causes. (1) "Held" was the client's in-memory state: a piece the cache had evicted stayed complete for it. (2) The
    budget counted the pieces asked for since the torrent was woken, not those held: each restart (six that day) asked
    for 441 more rarest pieces on top of what earlier runs had left, so `dde7a316` (16 MB pieces, 50 peers, fast) reached
    2.4 times its budget and, the cache being shared, evicted the other two slots' pieces. Fixed in the patch: each held
    piece is checked against the storage (`Piece.UpdateCompletion`), and the budget covers held + asked-and-not-arrived.
    After the fix the counters matched the disk (1067, 86, 158) and `dde7a316`, over its budget, asks for nothing more.
    The page shows "N rare pieces held (size of budget) · M on the way". To check later: the two others ask for pieces
    once their peers are back (the slots were woken again at 04:48 UTC by the rebuild).
  - **Memory budget** (`ATHENAFS_GENTLEMAN_MEMORY_MB`, 1000 by default, on the page; since 2026-10-23 the budget shared with
    waiting cache requests, `ATHENAFS_AWAKE_MEMORY_MB`, gentleman getting what they leave): candidates are taken in order of
    priority while their estimated cost (pieces x 3.5 kB) fits; a torrent too large for what is left is skipped. Lowering
    the budget releases the most recent slots that no longer fit. The 3 slots stay a maximum.
- **Cache requests (built 2026-10-23, owner's report):** a "Cache" that failed (a 3.7 GB zip with no reachable
  seeder) left no trace once the 3 lines under Activity rolled over or the core restarted. Every request is now a row of
  `/data/requests.db` (apart from `search.db`, which may be deleted and rebuilt), one per file: queued, caching (progress
  from memory), waiting, done, failed, cancelled. At start, what was queued or being read is queued again in the same
  order; done and cancelled rows older than 30 days are dropped. Search and `/api/file` show the request; a "Cache
  requests" section (filters, Cancel, Retry) and `GET /api/requests`, `POST /api/requests/retry|cancel`. Checked: a
  small file done, a core restart in the middle of reads (both resumed), Cancel on a running read.
  - **Waiting instead of failing (owner's decision, same day):** a first version failed a request after 3 reads of 60 s
    and retried it after a scrape (every 6 h), at most 3 times. The owner objected: nobody knows when a seeder shows up,
    and a failure is only seen by looking. Now a read that gets no data puts the request in **waiting**, for as long as
    needed; the owner sees how long each has waited and cancels what is not worth it. "Failed" is only for files that
    cannot be read at all (gone from the share, a path leaving it). Requests that failed under the first version were
    turned into waiting ones.
  - **How a request waits** (`plan_waiting`, every 30 s, and patch `wanted`): its torrent is **kept awake** with the pieces
    it needs at normal priority, on the normal cache, with no file open (Distribyted rereads `/data/wanted.json` every
    30 s and holds the listed torrents), so it downloads them as soon as a peer that has them connects; the request is done
    when all its pieces are in the cache (the header then shows "N arrived" for 24 h). The memory of the torrents kept
    awake by choice is one **shared budget** (2000 MB by default, estimated at 3.5 kB per piece), waiting requests first,
    cheapest torrents first (the most requests for the memory), gentleman mode with what is left. A torrent too large for
    what is left is **probed**: woken 10 min after 15 min, 30 min, 1 h, then every 2 h, and at once when a health check
    (connected seeders) or a scrape reports a seeder after its last probe; at most 2 probes at a time. Not done through
    reads in FUSE: dozens of reads blocked 60 s each could starve the share's own reads.
  - **Memory ceiling** (8000 MB by default, Settings, applied without restart): Distribyted's real memory (VmRSS). Between
    90 % and 100 % nothing new is woken; above it, one at a time every 25 min, a gentleman slot (the most recent) then the
    largest listening torrent is put back to sleep (it is probed instead). Distribyted also gets `GOMEMLIMIT` = the
    ceiling, so Go's collector works harder and returns memory near it (read at start only).
  - **Does the memory come back when a torrent sleeps? (measured 2026-10-23, before building the ceiling)** Waking the
    largest torrent (645,036 pieces, by a Cache request on a 34 kB file) took Distribyted from 4.44 to 5.92 GiB (about
    2.4 kB per piece, below the 3.5 kB estimate). It slept 10 min after the read ended (03:57 UTC); its memory came back in
    steps as Go returned it: 6.00, 5.50 (90 s later), 5.06, 4.61 GiB (9 min later). So a ceiling works, with a lag of about
    20 min between "release" and the memory falling (idle delay plus Go's return); hence 25 min between two put-to-sleep.
  - Checked on the running service: the 3.7 GB zip (31 MB estimate) kept awake, 18 peers, 0 seeders, 219 pieces
    missing; a 34 kB file on the 645,000-piece torrent (2.26 GB estimate, over the budget) probed then asleep
    until its next probe; gentleman's budget became 1968 MB; Settings applied without restart. On a copy of the tables
    (file writes redirected): a waiting request whose pieces are cached becomes done with its arrival time; at 9 GB the
    most recent gentleman slot is dropped, then (no slot left) the listening torrent stops listening; at 7.5 GB nothing is
    woken again; at 4 GB it listens again; a seeder reported by a health check after the last probe starts a probe at once.
    Not seen yet: a real arrival, a real ceiling crossing. The tables `wait_log` (every 5 min: peers, seeders and pieces
    missing per waiting torrent) and `mem_log` (Distribyted's memory with the torrents awake, listening, probed, helped)
    are there to see how often seeders show up and to set the budget from data.
  - **Outage during the work (about 10 min):** `GOMEMLIMIT=8000MB` is not valid for Go (it wants `MiB`): Distribyted
    exited at start; the check after the rebuild read the supervisor's state ("running"), not the mount. Check the mount.
- **Search results follow their status (owner's report, 2026-10-23):** after "Cache all" the rows kept saying "not
  cached": the result list was drawn once, and redrawn only after the request returned (about 2 s with the search).
  Now a click marks the rows "queued" at once, and while rows are queued, caching or downloading the list is fetched
  again every 5 s (not for waiting rows: a search costs about 1.4 s and a file can wait for days). Checked in the page:
  queued at the click, caching when a read slot freed, with no click. The owner's own "Cache all" (3 files, 517 MB) left
  no row in the request table; it was clicked during a rebuild, so it most likely reached the service while it stopped.
- **Page additions (owner's requests, 2026-10-23):** Cache requests: a filter field (words in the path, `q` in
  `/api/requests`) and "Clear finished" (forgets done, cancelled and failed requests; never queued, running or waiting
  ones; `POST /api/requests/clear`). Search: an order (as in the torrents, name, largest or smallest first) and a status
  filter (all, cached, not cached), also in `/api/search` (`sort`, `status`) and followed by "Cache all", which queues in
  that order (with a status filter every match up to 10,000 is looked at before the page is cut). A Memory tile:
  Distribyted's memory against the ceiling (orange above 90 %), and what is kept awake by choice against the budget
  (the split between waiting requests and gentleman in its tooltip). Tiles wrap from 200 px.
- **Published images (2026-10-23, owner's go):** `images.yml` builds core and smb for amd64 and arm64 on native runners
  (`ubuntu-24.04-arm`, free for public repositories; no QEMU: the Go build with cgo takes 5 min per architecture), combines
  them into one image each on ghcr.io and signs a provenance attestation; the base images were already pinned to
  multi-architecture digests. Packages are public (anonymous pulls answer 200). `up.sh` pulls on a clean checkout of a
  release tag, builds otherwise. Checked: the published `sha-a6456dd` images list both platforms, both attestations name
  `images.yml` and the commit; `scripts/smoke-image.sh` passed on amd64 here and on arm64 on an Apple Silicon Mac
  (OrbStack, which provides `/dev/fuse`; Docker Desktop on macOS cannot run the whole stack: no shared propagation to
  the host). Same SHA-256 for the same file on both. Not run yet: the whole stack (share included) on an arm64 Linux host.
- **Not wanted yet:** pinning files in the cache (protecting them from eviction).
- **Patches upstream:** the patches are local; the crash and prefix fixes would be worth proposing to Distribyted.
