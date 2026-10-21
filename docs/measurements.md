# Measurements

One row per measurement, made by `scripts/measure.sh` on the test host, bound to the commit the running code was built
from. Rows are only ever added. The dataset changes between rows (torrents are added): compare like with like.

- **dataset**: sources / torrents with metadata / pieces / files / total size of those torrents.
- **mount**: seconds from the restart of the core container to the FUSE mount. **added**: seconds until no torrent has
  been added for 30 s (all known torrents in their folders). **mounted**: sources mounted at the end.
- **CPU**: core container at rest (60 s after the start-up), average of 5 samples, % of one core.
- **RSS**: resident memory in GB of Distribyted and of the supervisor (admin.py).
- **times**: seconds, median of 3 (`>120`: timed out): `/api`, `/` (page), `/api/activity`, `/api/search?q=text`.

| date | commit | dataset | mount s | added s | mounted | CPU % | RSS core GB | RSS admin GB | /api s | page s | activity s | search s | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-21T13:43Z | 99f3c5a | 642 / 636 / 12479029 / 442784 / 258.3 TB | 0 | 91 | 636 | 209 | 8.5 | 13.9 | >120 | >120 | 0.56 | 13.73 | baseline before supervisor rework (/api and page: no answer within 120 s; recorded empty by the first version of the script, corrected) |
