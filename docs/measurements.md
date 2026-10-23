# Measurements

One row per measurement, made by `scripts/measure.sh` on the test host, bound to the commit the running code was built
from. Rows are only ever added. The dataset changes between rows (torrents are added): compare like with like.

- **dataset**: sources / torrents with metadata / pieces / files / total size of those torrents.
- **mount**: seconds from the restart of the core container to the FUSE mount. **added**: seconds until no torrent has
  been added for 90 s (all known torrents in their folders; at most 900; 30 s before 2026-10-21T14:00Z). **mounted**: sources mounted at the end.
- **CPU**: core container at rest (60 s after the start-up), average of 5 samples, % of one core.
- **RSS**: resident memory in GB of Distribyted and of the supervisor (admin.py).
- **times**: seconds, median of 3 (`>120`: timed out): `/api`, `/` (page), `/api/activity`, `/api/search?q=text`.

Corrections (rows are not rewritten):
- The search time used another query word before 2026-10-23 (5,511 matches against 5,139 for `text` on the same
  dataset, same time measured side by side: 0.022 to 0.024 s for both), so the column stays comparable.
- dbae52b: "mount 0" is wrong. The restart (Docker's 10 s stop timeout) killed Distribyted and left a dead FUSE mount that
  the script took for the new one; the share was in fact down. Fixed in c6cc571 (60 s grace, stale mounts cleared, the
  script waits for a mount that can be listed).

| date | commit | dataset | mount s | added s | mounted | CPU % | RSS core GB | RSS admin GB | /api s | page s | activity s | search s | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-21T13:43Z | dbae52b | 642 / 636 / 12479029 / 442784 / 258.3 TB | 0 | 91 | 636 | 209 | 8.5 | 13.9 | >120 | >120 | 0.56 | 13.73 | baseline before supervisor rework (/api and page: no answer within 120 s; recorded empty by the first version of the script, corrected) |
| 2026-10-21T13:59Z | c6cc571 | 642 / 636 / 12479029 / 442784 / 258.3 TB | 2 | 10 | 345 | 108 | 5.5 | 0.8 | 0.00 | 0.00 | 0.00 | 0.01 | supervisor: piece table, background status. NOT VALID: the end of loading was detected too early (30 s without an addition; 345/636 mounted), so CPU and RSS were taken while loading |
| 2026-10-21T14:14Z | 1a5745c | 642 / 636 / 12479029 / 442784 / 258.3 TB | 1 | 210 | 636 | 85 | 13.8 | 0.9 | 0.00 | 0.00 | 0.00 | 0.01 | supervisor: piece table, background status (same service code as c6cc571) |
| 2026-10-21T14:38Z | 7d98f57 | 642 / 636 / 12479029 / 442784 / 258.3 TB | 1 | 10 | 636 | 9 | 1.0 | 0.8 | 0.00 | 0.00 | 0.00 | 0.01 | dormant torrents (idle 600 s) |
| 2026-10-21T16:43Z | 5b7ade8 | 642 / 636 / 12479029 / 442784 / 258.3 TB | 0 | 8 | 636 | 9 | 1.1 | 1.0 | 0.00 | 0.00 | 0.00 | 0.01 | health check (2 at a time, 60 s; probes running during the rest sample) |
| 2026-10-21T17:02Z | 681bc8d | 642 / 637 / 12512828 / 446609 / 258.9 TB | 0 | 8 | 637 | 13 | 1.1 | 0.9 | 0.00 | 0.00 | 0.00 | 0.01 | API: status, usage, locations, erase by path |
| 2026-10-21T20:12Z | 518083a | 642 / 637 / 12512828 / 446609 / 258.9 TB | 0 | 9 | 637 | 10 | 1.1 | 0.7 | 0.00 | 0.00 | 0.00 | 0.01 | settings through .env; inline rename |
| 2026-10-21T21:57Z | e09bd14 | 642 / 637 / 12512828 / 446609 / 258.9 TB | 0 | 8 | 637 | 19 | 1.1 | 0.7 | 0.01 | 0.00 | 0.00 | 0.01 | security: capabilities dropped, no-new-privileges |
| 2026-10-22T13:03Z | a067cc6 | 642 / 637 / 12512828 / 446609 / 258.9 TB | 0 | 16 | 637 | 23 | 1.4 | 0.4 | 0.00 | 0.00 | 0.00 | 0.02 | archive views (BigCollection_ZIP on, 732 complete archives) |
| 2026-10-22T16:22Z | 6c144b3 | 642 / 637 / 12512828 / 446609 / 258.9 TB | 1 | 19 | 637 | 10 | 1.8 | 0.4 | 0.00 | 0.00 | 0.00 | 0.02 | baseline before the ~1000-torrent import |
| 2026-10-22T17:23Z | ce84e48 | 1051 / 1036 / 15900913 / 1760399 / 312.6 TB | 0 | 83 | 1036 | 18 | 3.4 | 0.9 | 0.00 | 0.00 | 0.00 | 0.02 | after the import (1051 sources); folder trackers, resolved magnets sleep |
| 2026-10-22T17:42Z | 1305ec8 | 1051 / 1036 / 15900913 / 1760399 / 312.6 TB | 0 | 85 | 1036 | 10 | 3.2 | 0.9 | 0.00 | 0.00 | 0.00 | 0.02 | folder UX (one line + Manage panel); cache waits through restarts |
| 2026-10-22T18:54Z | 2cbb5e2 | 1051 / 1036 / 15900913 / 1760399 / 312.6 TB | 0 | 63 | 1036 | 19 | 4.1 | 0.9 | 0.00 | 0.00 | 0.00 | 0.02 | gentleman mode on (3 slots, 1 GB memory budget, upload cap 1 MB/s) |
