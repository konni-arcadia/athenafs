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
- **Add** a magnet link, the URL of a .torrent file, or a .torrent file; **remove** with or without erasing its cached data.
- **Cache**: what is stored per torrent, as a tree of files and folders, with **Erase** on a file, a folder, a whole torrent,
  or everything. A piece shared with a neighbouring file that is not chosen is kept, and the page says so.
- **Cache cap**: shows the free disk space and the largest cap that is possible now. The cap is either fixed (in GB) or
  automatic (keep a percentage of the disk free, computed at each start).
- Every change restarts the core service (~10 s): the mounts are unavailable meanwhile and reads in progress fail.
- There is **no authentication**. By default it listens on `127.0.0.1`; `ATHENAFS_BIND=0.0.0.0` opens it to the local network,
  where anyone can add torrents and erase the cache. POST requests must come from the page itself (anti-CSRF).
  A URL given to the page is fetched only if it is http(s) and resolves to public addresses.

## The SMB share
Read-only, one share `torrents`, account `tor` / `rent` by default (test credentials: set `SMB_USER` and `SMB_PASS`).
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

## Licence
Not chosen yet. Distribyted is GPL-3.0, and the patches in `core/` are derivative work of it, so any publication must be
GPL-compatible. The repository is private for now.
