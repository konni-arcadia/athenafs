# athenafs

**Athena Filesystem.** Torrents as a read-only network folder. Content is downloaded only when a file is read, the cache
has a size limit, and a web page adds, removes and cleans torrents. A Mac or a Windows PC reads the folder over SMB like any
other share.

- **core**: [Distribyted](https://github.com/distribyted/distribyted) v0.15.0 built from source with a few local patches
  (`core/Dockerfile`, `core/patches/`), run under a small supervisor (`core/admin.py`) that serves the web page.
- **smb**: Samba, sharing the FUSE mount read-only.
- Why these choices, with the measurements behind them: `docs/findings.md`.

## Requirements
A Linux host with Docker (Compose v2), FUSE (`/dev/fuse`), `sudo` for the host mount point, and `findmnt`. The core container
runs with `SYS_ADMIN` and AppArmor unconfined, which FUSE needs: run it on a machine you trust.

## Quick start
```sh
cp .env.example .env      # optional: change ports, bind addresses, SMB credentials, data location
sh scripts/up.sh          # prepares the host mount point, builds, starts core and smb
```
Then open `http://localhost:8080/`, paste a magnet link (or the URL of a .torrent file, or pick a .torrent file) and add it.
The torrent appears as a folder under `data/mount/fs/<folder>/` on the host and in the SMB share.

```sh
sh scripts/down.sh        # stops everything and releases the host mount point
```

## The web page
- **Add** magnet links or URLs of .torrent files (one per line, a whole batch at once), or a .torrent file; **remove** with or
  without erasing its cached data. Torrents with the same folder name share one folder.
- **Folders**: one line per folder of the share; **Rename** a folder (an existing name merges them) or **Move** one torrent.
- **Activity**: download and upload rates per torrent, and the files that received data in the last 30 s.
- **Search** over every file name of every torrent (built from the .torrent files, nothing is downloaded), with each file's
  state: cached, partial, not cached, downloading, queued or caching. A folder in a result lists everything under it
  (`folder:path`); **Cache** reads a file, or all the results, into the cache ahead of time (refused when larger than the
  cache cap). Progress and **Cancel all** are under Activity.
- **Cache**: what is stored per torrent, as a tree of files and folders, with **Erase** on a file, a folder, a whole torrent,
  or everything. A piece shared with a neighbouring file that is not chosen is kept, and the page says so.
- **Cache cap**: shows the free disk space and the largest cap that is possible now. The cap is either fixed (in GB) or
  automatic (keep a percentage of the disk free, computed at each start).
- Every change restarts the core service (a few seconds): the mounts are unavailable meanwhile and reads in progress fail.
  With hundreds of torrents the folders then fill in over a few minutes.
- **Largest in the cache**: by file, folder or torrent, with **Erase** per line and links to open the item over SMB.
- **API** (JSON), documented at `/api/docs` (and `/api/openapi.json`): status (`/api/status`, `/api/torrents`), search with
  each file's state and its `smb://` and `\\host\share\…` locations (`/api/search`, `/api/file`), what takes the most
  room in the cache (`/api/usage`), and actions (`POST /api/prefetch`, `POST /api/erase`). POST requests need an `Origin`
  header naming the page's host or `X-Athenafs: 1`, e.g.
  `curl -H 'X-Athenafs: 1' --data-urlencode 'path=folder/file' http://localhost:8080/api/prefetch`.
  The SMB host name in the locations is the host of `ATHENAFS_PAGE_URL` unless `ATHENAFS_SMB_HOST` is set.
- There is **no authentication**. By default it listens on `127.0.0.1`; `ATHENAFS_BIND=0.0.0.0` opens it to the local network,
  where anyone can add torrents and erase the cache. POST requests must come from the page itself (anti-CSRF).
  A URL given to the page is fetched only if it is http(s) and resolves to public addresses.

## The SMB share
Read-only, one share `torrents`, account `tor` / `rent` by default (test credentials: set `SMB_USER` and `SMB_PASS`).
Its root also holds two links to the web page (`.webloc` for macOS, `.url` for Windows); set `ATHENAFS_PAGE_URL` in `.env`
if `http://<this host>.local:<port>/` is not how the network reaches it.
By default it listens on `127.0.0.1` only; `SMB_BIND=0.0.0.0` shares it on the local network.
- Mac: `mount_smbfs //tor:rent@<host>/torrents <dir>`, or Finder > Go > Connect to Server > `smb://<host>/torrents`.
- Windows: `net use \\<host>\torrents /user:tor rent`, or the Explorer address bar.

Archives (zip, 7z, rar) are shown as ordinary files by default (`ATHENAFS_ARCHIVES=off`). Distribyted can show them as
folders and extract them itself, but opening each archive downloads its headers (4 to 10 MB measured), so a recursive listing
or a "Get Info" over hundreds of archives would download gigabytes. For a big archive: copy it first, then extract locally.
A solid 7z read through the mount needs a cache cap larger than the archive.

## Layout
```
core/                  patched Distribyted + the supervisor and web page (admin.py)
smb/                   Samba container
scripts/up.sh down.sh  start and stop (they prepare the host mount point)
scripts/measure-upload.sh   measures traffic under upload caps (tc); used for the tests only
experiments/btfs/      the rejected first candidate, kept for the record
docs/findings.md       what was measured and why things are the way they are
data/                  runtime data, not in git: torrents/ (optional .torrent files), state/ (cache, sources, settings), mount/
```

## Known limits
- Upload is not capped: the client takes part in the swarm like any other. Outgoing traffic measured at about 1 % of incoming,
  mostly acknowledgements; real piece uploads were not separated from it.
- The first read after a restart waits for the swarm and can fail after the read timeout (60 s); retry.
- Read-ahead is wide: a few times more data can be received than is read.
- Finder and Windows Explorer themselves (previews, thumbnails) were not tested, only equivalent commands.
- The patches to Distribyted are local; they are not proposed upstream yet.
- Torrents are dormant until one of their files is opened (`ATHENAFS_DORMANT_AFTER`, 600 s after the last close; `0` keeps
  every torrent in the BitTorrent client): a dormant torrent does not seed, and its first read waits for peers. With 636
  torrents (258 TB) the core uses about 1 GB and 9 % of a core at rest; see `docs/measurements.md`.

## Licence
Not chosen yet. Distribyted is GPL-3.0, and the patches in `core/` are derivative work of it, so any publication must be
GPL-compatible. The repository is private for now.
