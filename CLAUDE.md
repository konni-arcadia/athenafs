# athenafs: instructions for Claude

Torrents as a read-only network folder (patched Distribyted + supervisor/web page + Samba). Read `README.md` first, then
`docs/findings.md` for why things are the way they are.

## Rules
- **Everything in this repository is written in English**: code, comments, UI text, commit messages, documentation, any
  message sent from this workshop. This is the owner's rule (2026-10-21) and has no exception.
- This is a workshop with its own repository. It knows nothing about the owner's other projects and holds no personal data,
  machine names, addresses or secrets. Test credentials (`tor` / `rent`) are the one deliberate default.
- Write for a reader who is not the owner: no private hostnames, no home-network IP addresses in committed files.
- Commit only when asked. Pushing to GitHub is outward-facing: ask first.
- If `local/RULES.md` exists, read it before working and follow it.

## Working here
- Start and stop with `sh scripts/up.sh` / `sh scripts/down.sh`. They prepare the host mount point (bind mount of itself with
  shared propagation). Do not `docker compose up` by hand without `DATA` resolving to an absolute path you intend.
- Changes to `core/admin.py` or to the patches need `sh scripts/up.sh` (it rebuilds); check with `curl localhost:8080/api`.
- `core/admin.py` runs Distribyted as a child process. Anything that changes what is served or what is cached is done with
  Distribyted **stopped** (erasing cached pieces live breaks reads with `unexpected EOF`). Keep it that way.
- The FUSE mount is in the sub-folder `mount/fs`, never directly on the bind-mounted `mount/`: an `umount` inside the container
  would otherwise propagate to the host and remove the bind.
- Patches to Distribyted live in `core/Dockerfile` and `core/patches/`. Each has a comment saying why; if one is changed, re-check
  the Samba share from a Mac (the link-count patch is what lets Samba open the share root).
- Never run a recursive listing over a share with `ATHENAFS_ARCHIVES=on` just to look around: each archive opened downloads
  megabytes.

## Tests worth running after a change
Mount lists the folders; `curl localhost:8080/api` shows `"state": "running"`; add and remove a small torrent from the page;
read a file twice (the second read must receive about nothing); erase it and read again (it must download again); mount the
share from another machine and copy a file, comparing a checksum.

## Measurements
Performance work is measured, and every measurement is bound to a commit: commit the change, `sh scripts/up.sh` (it
records the commit in the container), then `sh scripts/measure.sh "note"` (it refuses a dirty tree or a container built
from another commit) and commit the new row of `docs/measurements.md` on its own. The dataset is in each row: torrents keep
being added, so compare like with like.
