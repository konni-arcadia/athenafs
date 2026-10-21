# athenafs

**Athena Filesystem.** Torrents as a read-only network folder. Files are downloaded only when they are read, the cache has
a size limit, and a web page (with a JSON API) adds, searches, caches and cleans. A Mac or a Windows PC reads the folder over
SMB like any other share. Built for collections of hundreds of torrents: tested with 637 torrents, 259 TB, 447,000 files.

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
- **Folders:** torrents with the same folder name share one folder (a collection split in parts appears once); a folder can
  be renamed, a torrent moved.
- Why things are the way they are, with the measurements behind them: `docs/findings.md`. Performance per commit:
  `docs/measurements.md`.

## Requirements
A Linux host with Docker (Compose v2), FUSE (`/dev/fuse`) and `findmnt`; `sudo` only on hosts whose `/` is not a shared
mount (systemd hosts have it shared). The core container needs extra privileges for FUSE: read Security first.

## Quick start
```sh
cp .env.example .env      # optional: every setting is in it (see Configuration)
sh scripts/up.sh          # prepares the host mount point, builds, starts core and smb
```
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
| `ATHENAFS_ADD_TIMEOUT`, `ATHENAFS_READ_TIMEOUT` | `10`, `60` | Seconds to wait for a magnet's metadata at start; for a stuck read before it fails. |
| `ATHENAFS_ARCHIVES` | `off` | `on`: zip, 7z and rar open as folders (expensive, see The SMB share). |
| `SMB_BIND`, `SMB_PORT` | `127.0.0.1`, `445` | Address of the share. `0.0.0.0` shares it on the network. |
| `SMB_SHARE`, `SMB_USER`, `SMB_PASS` | `torrents`, `tor`, `rent` | Share name and account (test credentials: change them). |

The cache cap is set on the page (fixed size, or a share of the disk to keep free).

## The web page
- **Add** magnet links or URLs of .torrent files (one per line, a batch at once) or a .torrent file, optionally into a named
  folder. **Remove** a torrent with or without its cached data.
- **Search** every file name of every torrent, with each file's state (cached, partial, not cached, downloading, queued,
  caching) and the last health check of its torrent. Click a folder in a result to list everything under it
  (`folder:path`). **Cache** reads a file, or every result, into the cache ahead of time.
- **Largest in the cache**, by file, folder or torrent, with **Erase** and links to open the item over SMB.
- **Activity**: rates per torrent, files receiving data, the queue of files being cached.
- **Folders**: torrents per folder (mounted, awake, health), **Rename** a folder (an existing name merges the two) or
  **Move** one torrent.
- **Cache**: what is stored per torrent as a tree, with **Erase** on a file, a folder, a torrent or everything (a piece
  shared with a file that was not chosen is kept, and the page says so).
- **Settings**: cache cap.
- Adding, removing, renaming and erasing restart the core (a few seconds): the share is unavailable meanwhile and reads in
  progress fail.

## The API
JSON over HTTP, documented on the server at `/api/docs` (and `/api/openapi.json`).
- Status: `GET /api/status`, `GET /api/torrents`, `GET /api/torrents/{hash}`, `GET /api/activity`.
- Files: `GET /api/search?q=words` (or `q=folder:path`) and `GET /api/file?path=…` give each file's state, its torrent's
  health, and where to open it: `smb_url` (Finder) and `unc_path` (Windows).
- Space: `GET /api/usage?by=file|folder|torrent` lists what takes the most room in the cache.
- Actions: `POST /api/prefetch` (read into the cache), `POST /api/prefetch/cancel`, `POST /api/erase` (a file, folder or
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
- BitTorrent shows your IP address to every peer of the torrents that are awake (being read, or woken for a health
  check). What you add, and whether you route it through a VPN, is your responsibility.
- Upload is not capped while a torrent is awake.

## Performance
With 637 torrents (259 TB, 12.5 million pieces): every torrent in its folder 10 s after a start, the core at about 10 % of
a core and 1.1 GB at rest, the supervisor 0.7 GB, page, API and search answering in hundredths of a second. Before the
supervisor rework and dormant torrents the same set needed 14 GB, two cores and did not answer within 120 s.

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
experiments/btfs/         the rejected first candidate, kept for the record
docs/findings.md          what was measured and why things are the way they are
docs/measurements.md      one row of measurements per commit
data/                     runtime data, not in git: torrents/ (optional .torrent files), state/, mount/
```

## Known limits
- Dormant torrents do not seed, and the first read of one waits for peers (a few seconds, up to the read timeout).
- Upload is not capped while a torrent is awake: it takes part in the swarm like any other client.
- The first read after a restart can fail after the read timeout (60 s) while peers are found; retry.
- Read-ahead is wide: a few times more data can be received than is read.
- The health check counts peers announced on the DHT and trackers; a dormant torrent seldom connects to them, so "peers
  announced" is not proof they answer.
- Finder browsing and copying were checked; Finder and Explorer previews and thumbnails were not, and a preview that reads a
  file wakes its torrent. Windows was not tested.
- Nothing can be pinned in the cache: the oldest pieces leave first when the cap is reached.
- The patches to Distribyted are local; they are not proposed upstream.

## Licence
Copyright the athenafs contributors. GNU General Public License version 3 or (at your option) any later version
(`GPL-3.0-or-later`): see `LICENSE`. Distribyted, which the patches in `core/patches/` modify, is itself GPL-3.0.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty
of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
