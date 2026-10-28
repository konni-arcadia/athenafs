# athenafs

**Athena Filesystem.** Torrents as a read-only network folder. Files are downloaded only when they are read, the cache has
a size limit, and a web page (with a JSON API) adds, searches, caches and cleans. A Mac or a Windows PC reads the folder over
SMB like any other share. Built for collections of hundreds of torrents: tested with 1,051 torrents, 313 TB, 1.76 million files.

## How this was made
athenafs is a personal project. Most of the code, the patches to Distribyted, the research and the documentation were
written by an AI coding assistant (Claude Code) working under my direction. I set the goals, made the decisions, and
tested it myself as a user would, from a Mac and a Windows PC on my own network; the figures in `docs/` come from real
runs on a test host, each tied to the commit it measured. I use athenafs myself.

## How it works
- **core**: [Distribyted](https://github.com/distribyted/distribyted) v0.15.0, built from source with local patches
  (`core/Dockerfile`, `core/patches/`, each one commented), mounts every torrent as a folder through FUSE. A supervisor
  (`core/admin.py`) runs it, generates its configuration and serves the web page and the API.
- **smb**: Samba shares that FUSE mount, read-only.
- **On demand:** reading a file downloads the pieces it needs; they stay in the cache until the cap pushes the oldest out.
- **Dormant torrents:** listing folders needs no BitTorrent client (the trees come from the `.torrent` files). A torrent
  enters the client only when one of its files is opened and leaves it 10 minutes after the last one is closed, so hundreds
  of torrents cost what the few being read cost.
- **Health check:** dormant torrents are woken in turn, a few at a time, to count their peers; the page and the API show
  when a torrent looks dead.
- **Gentleman mode (on by default):** every 6 h the trackers are asked how many seeders and leechers each torrent has.
  A few torrents in difficulty (1 or 2 seeders, leechers waiting) get a slot: their rarest pieces are downloaded into a
  separate part of the cache and seeded, under an upload cap, until they have 3 seeders again or for 24 h, within the
  memory that waiting cache requests leave.
- **Folders:** torrents with the same folder name share one folder (a collection split in parts appears once); a folder can
  be renamed, a torrent moved.
- **Ready views:** for a folder you choose, a sibling folder (`<folder>_READY` by default) shows its files that are
  completely in the cache, archives (zip, 7z, rar) opened as folders and other files as they are. Browsing it never
  downloads anything, whatever a file browser enumerates: put files in the cache from the page, then open them over SMB.
  Hide the folder itself from the share and the share shows only what is ready (for a player or another device); the page
  and the API keep the whole catalogue.
- Why things are the way they are, with the measurements behind them: `docs/findings.md`. Performance per commit:
  `docs/measurements.md`.

## Requirements
A Linux host (amd64 or arm64) with Docker (Compose v2), FUSE (`/dev/fuse`) and `findmnt`; `sudo` only on hosts whose `/`
is not a shared mount (systemd hosts have it shared). Docker Desktop on macOS or Windows cannot share the FUSE mount
with the host or the SMB container, so it is not supported. The core container needs extra privileges for FUSE: read
Security first.

## Quick start
```sh
git clone https://github.com/konni-arcadia/athenafs.git && cd athenafs
git checkout "$(git describe --tags --abbrev=0)"   # the latest release
cp .env.example .env      # optional: every setting is in it (see Configuration)
sh scripts/up.sh          # prepares the host mount point, gets the images, starts core and smb
```
On a release, `up.sh` downloads that release's images from `ghcr.io` (built by this repository's CI for amd64 and arm64,
with a signed build provenance: `gh attestation verify oci://ghcr.io/konni-arcadia/athenafs-core:<version> -R
konni-arcadia/athenafs`, GitHub CLI 2.49 or later). Anywhere else (`main`, local changes) it builds them from source, which takes a few minutes;
`ATHENAFS_BUILD=1` always builds.
Open `http://localhost:8080/`, paste a magnet link (or the URL of a .torrent file, or pick a .torrent file) and add it. The
torrent appears as a folder in the SMB share and under `data/mount/fs/` on the host.
```sh
sh scripts/down.sh        # stops everything and releases the host mount point
```
Always start and stop with these scripts: they prepare the mount point, record the commit the code was built from, and
clear a FUSE mount left behind by a crash.

## Configuration
Everything is set in `.env` (copy `.env.example`); every setting is optional.

| Setting | Default | What |
|---|---|---|
| `DATA` | `./data` | Runtime data: cache, state, mount point. |
| `ATHENAFS_BIND`, `ATHENAFS_PORT` | `127.0.0.1`, `8080` | Address of the web page and API. `0.0.0.0` opens it to the network. |
| `ATHENAFS_PASSWORD` | none | Password of the page and API (basic authentication, user `admin`). |
| `ATHENAFS_PAGE_URL` | `http://<host>.local:<port>/` | Address of the page as clients reach it (links at the root of the share). |
| `ATHENAFS_SMB_HOST` | host of the page URL | Host name in the `smb://` and `\\host\share` locations given by the API. |
| `ATHENAFS_DORMANT_AFTER` | `600` | Seconds without an open file before a torrent leaves the client; `0` = never. |
| `ATHENAFS_HEALTH_PROBE`, `ATHENAFS_HEALTH_PARALLEL` | `60`, `2` | Health check: seconds per torrent, torrents at a time; `0` = off. |
| `ATHENAFS_GENTLEMAN`, `ATHENAFS_GENTLEMAN_SLOTS`, `ATHENAFS_GENTLEMAN_SPACE_PCT` | `on`, `3`, `5` | Gentleman mode: on or off, torrents helped at most, part of the cache cap reserved for them. Defaults: the page's Settings override them. |
| `ATHENAFS_AWAKE_MEMORY_MB`, `ATHENAFS_MEMORY_CEILING_MB` | `2000`, `8000` | Memory for the torrents kept awake by choice, estimated at about 3.5 kB per piece: torrents of cache requests waiting for a seeder first, gentleman mode with what is left. Above the ceiling (Distribyted's real memory), nothing new is woken and they are put back to sleep one at a time. Defaults: the page's Settings override them. |
| `ATHENAFS_UPLOAD_LIMIT_KBPS` | `1000` | Upload cap for everything, in kB/s (`0` = none). Also in the page's Settings. |
| `ATHENAFS_EXTRA_TRACKERS` | none | Trackers (comma-separated) added to magnets that have none; a magnet without tracker also gets the trackers its folder's other magnets use. |
| `ATHENAFS_ADD_TIMEOUT`, `ATHENAFS_READ_TIMEOUT` | `10`, `60` | Seconds to wait for a magnet's metadata at start; for a stuck read before it fails. |
| `ATHENAFS_ARCHIVES` | `off` | `on`: zip, 7z and rar open as folders (expensive, see The SMB share). |
| `SMB_BIND`, `SMB_PORT` | `127.0.0.1`, `445` | Address of the share. `0.0.0.0` shares it on the network. |
| `SMB_SHARE`, `SMB_USER`, `SMB_PASS` | `torrents`, `tor`, `rent` | Share name and account (test credentials: change them). |

The cache cap is set on the page (fixed size, or a share of the disk to keep free).

## The web page
- **Add** magnet links or URLs of .torrent files (one per line, a batch at once) or a .torrent file, optionally into a named
  folder. **Remove** a torrent with or without its cached data.
- **Search** every file name of every torrent, with each file's state (cached, partial, not cached, downloading, queued,
  caching, waiting, failed) and the last health check of its torrent. Click a folder in a result to list everything under it
  (`folder:path`). Order (as in the torrents, name, largest or smallest first) and filter (all, cached, not cached).
  **Cache** reads a file, or every result, into the cache ahead of time ("Cache all" follows the order and the filter);
  the rows follow their state while files are queued or being read.
- **Largest in the cache**, by file, folder or torrent, with **Erase** and links to open the item over SMB.
- **Tiles**: cache, disk, transfer, torrents, and memory: Distribyted's memory against its ceiling, and the torrents kept
  awake by choice against their budget.
- **Activity**: rates per torrent, files receiving data, the queue of files being cached, the requests waiting for a
  seeder, the gentleman slots (pieces held against their budget).
- **Cache requests**: every Cache asked for, kept across restarts (the queue resumes after one), with its state (queued,
  caching with progress, waiting for a seeder, done, failed, cancelled), **Cancel** and **Retry**. A file that gets no data
  waits as long as needed: its torrent is kept awake with the pieces it needs, within a memory budget (Settings); torrents
  left out are woken 10 minutes now and then (after 15 min, 30, 1 h, then every 2 h) and at once when a health check or the
  trackers report a seeder. The header shows how many requests wait and how many arrived in the last 24 h. A filter on
  the path, and **Clear finished** (forgets done, cancelled and failed requests).
- **Folders**: torrents per folder (mounted, awake, health), **Rename** a folder (an existing name merges the two) or
  **Move** one torrent; **hide it from the share** or show it again (immediate, no restart); turn its **ready view** on or
  off, or rename it; for the whole folder at once: **Erase its cache**, **Remove folder** (with its cached data) or
  **Remove, keep data**.
- **Cache**: what is stored per torrent as a tree, with **Erase** on a file, a folder, a torrent or everything (a piece
  shared with a file that was not chosen is kept, and the page says so).
- **Stop caching** (header): nothing new is downloaded until you resume, also after a restart. What is in the cache
  stays available over SMB and the API; a file that is not cached fails at once instead of downloading; Cache requests
  are refused, queued and waiting ones pause and resume afterwards; gentleman mode takes no slot. No restart.
- **Settings**: cache cap; gentleman mode (slots, space, upload cap); memory for the torrents kept awake and the ceiling
  of Distribyted's memory (applied without restart).
- Adding, removing, renaming and erasing restart the core (a few seconds): the share is unavailable meanwhile and reads in
  progress fail.

## The API
JSON over HTTP, documented on the server at `/api/docs` (and `/api/openapi.json`).
- Status: `GET /api/status`, `GET /api/torrents`, `GET /api/torrents/{hash}`, `GET /api/activity`.
- Files: `GET /api/search?q=words` (or `q=folder:path`; `sort=name|largest|smallest`, `status=cached|not cached`) and `GET /api/file?path=…` give each file's state, its torrent's
  health, and where to open it: `smb_url` (Finder) and `unc_path` (Windows).
- Cached files: `GET /api/cached` (optional `folder`, `format=ndjson`) lists every file completely in the cache in one
  answer, kept up to date every 30 s, with a `generation` that is also its ETag (`If-None-Match` answers 304 when nothing
  changed). In the search, `status=cached` draws from the same list.
- Space: `GET /api/usage?by=file|folder|torrent` lists what takes the most room in the cache.
- Folders: `GET /api/folders`, `POST /api/ready-view` (`folder`, `on`, `name`), `POST /api/folder-visibility`
  (`folder`, `hidden`), `POST /api/folder-remove` (`folder`, `erase`). Items of a hidden folder have no `smb_url` or
  `unc_path`. To erase a folder's cache and keep its torrents: `POST /api/erase` with `path=<folder>`.
- Cache requests: `GET /api/requests?state=all|open|waiting|failed|done|cancelled` (and `q`, words in the path),
  `POST /api/requests/retry` and `POST /api/requests/cancel` (`path`, repeatable, or `all=true`),
  `POST /api/requests/clear` (forgets the finished ones). Search and file results carry their request.
  `GET /api/waiting`: the torrents kept awake or probed for them, the memory budget, the ceiling, and Distribyted's memory
  over the last 24 h.
- Caching: `POST /api/caching` (`stopped=true|false`); the state is `caching_stopped` in `GET /api/status`.
- Actions: `POST /api/prefetch` (read into the cache; `path`, or `q` with `sort` and `status`), `POST /api/prefetch/cancel`, `POST /api/erase` (a file, folder or
  torrent).

POST requests need an `Origin` header naming the page's host or `X-Athenafs: 1` (anti-CSRF), for example:
```sh
curl -s 'http://localhost:8080/api/search?q=readme&limit=5'
curl -s -H 'X-Athenafs: 1' --data-urlencode 'path=folder/file.zip' http://localhost:8080/api/prefetch
```
Without `ATHENAFS_PASSWORD`, anyone who reaches the page can add torrents and erase the cache. A URL given to the page is
fetched only if it is http(s) and resolves to public addresses.

## The SMB share
Read-only, one share (`torrents` by default). Its root also holds two links to the web page: `.webloc` for macOS, `.url`
for Windows.
- Mac: Finder > Go > Connect to Server > `smb://<host>/torrents`, or `mount_smbfs //tor:rent@<host>/torrents <dir>`.
- Windows: `\\<host>\torrents` in the Explorer address bar, or `net use \\<host>\torrents /user:tor rent`.

**If this host already serves SMB shares** (its own Samba or ksmbd), port 445 is usually taken on every address and the
share cannot start there; `scripts/up.sh` detects it, explains, and starts nothing. Two ways, both set in `.env` and
leaving the host's own SMB server as it is (athenafs does not touch the host's configuration):
- **Another port**, `SMB_PORT=4445` (or any free port). Macs connect with `smb://<host>:4445/torrents`. Windows' client
  uses port 445 only, except in recent versions: `net use \\<host>\torrents /TCPPORT:4445`.
- **A second IP address on the host** for the share, `SMB_BIND=<that address>`: every client keeps port 445. The host's
  SMB server must then listen on its own address only (Samba: `interfaces` and `bind interfaces only = yes`), which is a
  change to the host's configuration, made by you.

Archives (zip, 7z, rar) are shown as ordinary files by default. Distribyted can show them as folders, but opening each one
downloads its headers (4 to 10 MB measured), so browsing hundreds of them would download gigabytes. For a big archive, copy
it first, then extract it locally. A solid 7z read through the share needs a cache cap larger than the archive.

## Security
Read this before exposing anything beyond the machine itself.

**What is exposed, and the defaults**
- The page and the API listen on `127.0.0.1` and the share on `127.0.0.1` by default: nothing is reachable from the network
  until `ATHENAFS_BIND` or `SMB_BIND` says so.
- Exposing the page without `ATHENAFS_PASSWORD` lets anyone on that network add torrents (your connection then downloads
  and uploads what they chose, under your IP address), erase the cache and change the settings. The password is sent with
  basic authentication over plain HTTP: use it on a network you trust, or put the page behind an HTTPS reverse proxy.
- The share is read-only, with the test account `tor` / `rent` unless `SMB_USER` and `SMB_PASS` are set.
- `scripts/up.sh` prints a warning, and the page and `/api/status` show it, when the page is exposed without a password or
  the share with the test account.

**Privileges**
- The core container needs FUSE: `/dev/fuse`, the `SYS_ADMIN` capability and AppArmor unconfined (Docker's default
  profile forbids mounting), plus `DAC_OVERRIDE` to write in `DATA` (owned by your user). Every other capability is dropped
  and `no-new-privileges` is set. It is still the part to watch: it talks to the internet (peers, trackers, URLs given to
  the page) with more rights than a normal container, so a flaw in Distribyted or its BitTorrent library would be easier
  to turn into an escape.
- The Samba container keeps `CHOWN`, `SETUID` and `SETGID` (to create its account and switch to it), nothing else, and
  `no-new-privileges`. It only sees the mount, read-only.
- `sudo` is only needed on hosts whose `/` is not a shared mount (systemd hosts have it shared): `up.sh` then makes `DATA/mount`
  a shared bind mount of itself, and `down.sh` releases it.
- Run it on a machine you trust, not on a host shared with untrusted users or services.

**Guards in place**
- POST requests must come from the page (matching `Origin`) or carry `X-Athenafs: 1`: other web sites cannot act through
  your browser (anti-CSRF).
- A URL given to the page is fetched only if it is http(s) and every address it resolves to (and every redirect) is
  public, at most 10 MB, and parsed as a torrent before anything is kept.
- Torrent and file names are escaped on the page; folder names are sanitised (`A-Z a-z 0-9 . _ -`); a file path from a
  torrent that would leave the share (`..`) is never read.

**What is yours to decide**
- BitTorrent shows your IP address to every peer of the torrents that are awake (being read, woken for a health check,
  or helped by the gentleman mode). What you add, and whether you route it through a VPN, is your responsibility.
- The gentleman mode is on by default: right after installation, athenafs downloads pieces of the torrents you added that
  are in difficulty and uploads them to other peers, under your IP address. Turn it off in Settings (or
  `ATHENAFS_GENTLEMAN=off`) if you do not want to seed. Upload is capped (1 MB/s by default, `ATHENAFS_UPLOAD_LIMIT_KBPS`).

## Performance
With 1,051 torrents (1,036 with their metadata; 313 TB, 15.9 million pieces, 1.76 million files; measured 2026-10-22):
the share is mounted at once and every torrent is in its folder about 85 s after a start; at rest the core uses about 10 %
of a core and 3.2 GB, the supervisor 0.9 GB; page, API and search answer in hundredths of a second. Gentleman mode with
3 slots raises the core to about 19 % and 4.1 GB. Before the supervisor rework and dormant torrents, 637 torrents needed
14 GB and two cores, and the page did not answer within 120 s.

Measurements are bound to commits: commit, `sh scripts/up.sh`, then `sh scripts/measure.sh "note"`, which restarts the
core, times the start-up, samples CPU, memory and page timings, and appends a row to `docs/measurements.md` (it refuses
uncommitted changes or a container built from another commit).

## Layout
```
core/                     patched Distribyted, supervisor, web page and API (admin.py), base configuration
core/patches/             the patches to Distribyted, applied in core/Dockerfile
smb/                      Samba container
scripts/up.sh down.sh     start and stop (host mount point, commit of the build, stale mounts)
scripts/measure.sh        measurement of the running commit (with measure_dataset.py)
scripts/measure-upload.sh traffic under upload caps (tc), used for the early tests
scripts/smoke-image.sh    smoke test of a core image on any Docker host (Docker Desktop on a Mac included)
experiments/btfs/         the rejected first candidate, kept for the record
docs/findings.md          what was measured and why things are the way they are
docs/measurements.md      one row of measurements per commit
CHANGELOG.md              notable changes, and what to check when upgrading
CONTRIBUTING.md SECURITY.md  how to report a bug, propose a change, report a vulnerability
.github/workflows/        secret scan on each push and pull request; images built and published on each release
data/                     runtime data, not in git: torrents/ (optional .torrent files), state/, mount/
```

## Known limits
- Dormant torrents do not seed, and the first read of one waits for peers (a few seconds, up to the read timeout).
- The upload cap applies to the whole client; there is no download cap.
- The first read after a restart can fail after the read timeout (60 s) while peers are found; retry.
- Read-ahead is wide: a few times more data can be received than is read.
- The health check counts peers announced on the DHT and trackers; a dormant torrent seldom connects to them, so "peers
  announced" is not proof they answer.
- Browsing and copying were checked from Finder and from Windows (listing, copies, checksums); Finder and Explorer
  previews and thumbnails were not, and a preview that reads a file wakes its torrent.
- Nothing can be pinned in the cache: the oldest pieces leave first when the cap is reached.
- The patches to Distribyted are local; they are not proposed upstream.
- arm64: the core image was checked on an Apple Silicon Mac (OrbStack) with `scripts/smoke-image.sh` (start, a torrent
  mounted, a file read twice), without the share; the whole stack has not been run on an arm64 Linux host yet.

## Contributing
Issues and pull requests are welcome: see `CONTRIBUTING.md`. Security issues: `SECURITY.md` (private reporting, not a
public issue).

## Licence
Copyright the athenafs contributors. GNU General Public License version 3 or (at your option) any later version
(`GPL-3.0-or-later`): see `LICENSE`.

The patch files `core/patches/*.patch` modify [Distribyted](https://github.com/distribyted/distribyted) and contain
parts of its code, which is licensed under the GPL version 3 only: those files are `GPL-3.0`. The Distribyted built by
`core/Dockerfile` is Distribyted v0.15.0 (commit `58d471c`), modified by the athenafs contributors since 2026-10-21;
each change is described in `core/Dockerfile`, and its date is in `CHANGELOG.md`.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
