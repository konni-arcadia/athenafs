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
- 99f3c5a: "mount 0" is wrong. The restart (Docker's 10 s stop timeout) killed Distribyted and left a dead FUSE mount that
  the script took for the new one; the share was in fact down. Fixed in 30f91c1 (60 s grace, stale mounts cleared, the
  script waits for a mount that can be listed).

| date | commit | dataset | mount s | added s | mounted | CPU % | RSS core GB | RSS admin GB | /api s | page s | activity s | search s | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-21T13:43Z | 99f3c5a | 642 / 636 / 12479029 / 442784 / 258.3 TB | 0 | 91 | 636 | 209 | 8.5 | 13.9 | >120 | >120 | 0.56 | 13.73 | baseline before supervisor rework (/api and page: no answer within 120 s; recorded empty by the first version of the script, corrected) |
| 2026-10-21T13:59Z | 30f91c1 | 642 / 636 / 12479029 / 442784 / 258.3 TB | 2 | 10 | 345 | 108 | 5.5 | 0.8 | 0.00 | 0.00 | 0.00 | 0.01 | supervisor: piece table, background status. NOT VALID: the end of loading was detected too early (30 s without an addition; 345/636 mounted), so CPU and RSS were taken while loading |
| 2026-10-21T14:14Z | a1a779a | 642 / 636 / 12479029 / 442784 / 258.3 TB | 1 | 210 | 636 | 85 | 13.8 | 0.9 | 0.00 | 0.00 | 0.00 | 0.01 | supervisor: piece table, background status (same service code as 30f91c1) |
| 2026-10-21T14:38Z | e9c3b55 | 642 / 636 / 12479029 / 442784 / 258.3 TB | 1 | 10 | 636 | 9 | 1.0 | 0.8 | 0.00 | 0.00 | 0.00 | 0.01 | dormant torrents (idle 600 s) |
| 2026-10-21T16:43Z | 1d2ff37 | 642 / 636 / 12479029 / 442784 / 258.3 TB | 0 | 8 | 636 | 9 | 1.1 | 1.0 | 0.00 | 0.00 | 0.00 | 0.01 | health check (2 at a time, 60 s; probes running during the rest sample) |
| 2026-10-21T17:02Z | 30ee655 | 642 / 637 / 12512828 / 446609 / 258.9 TB | 0 | 8 | 637 | 13 | 1.1 | 0.9 | 0.00 | 0.00 | 0.00 | 0.01 | API: status, usage, locations, erase by path |
| 2026-10-21T20:12Z | 74e2f59 | 642 / 637 / 12512828 / 446609 / 258.9 TB | 0 | 9 | 637 | 10 | 1.1 | 0.7 | 0.00 | 0.00 | 0.00 | 0.01 | settings through .env; inline rename |
| 2026-10-21T21:57Z | 7213a7a | 642 / 637 / 12512828 / 446609 / 258.9 TB | 0 | 8 | 637 | 19 | 1.1 | 0.7 | 0.01 | 0.00 | 0.00 | 0.01 | security: capabilities dropped, no-new-privileges |
