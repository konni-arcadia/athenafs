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
- **No doubled name:** a multi-file torrent served in a folder named after it showed `BigCollection/BigCollection/…`.
  Patch `core/patches/flatten_named_folder.patch`: when the folder's name is the torrent's name (sanitised like `admin.py`
  does), the torrent's own top-level directory is skipped. A folder with another name keeps it (`librivox/1601_0903_librivox/…`).
- A source cannot be moved to another folder from the page: remove it ("Remove, keep data") and add it again with the name.

Measured: 64/64 mounted; `BigCollection/T-En Collection` holds 61 sub-folders coming from different torrents; a file
read twice gives the same checksum; the Samba container sees the same tree.

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
