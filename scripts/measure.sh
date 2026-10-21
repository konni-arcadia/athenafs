#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Measures the running service and appends one row to docs/measurements.md, bound to the commit it was built from.
# Restarts the core service (the start-up is part of the measurement). Refuses a dirty tree or a container built from
# another commit: commit, run scripts/up.sh, then measure, then commit the new row on its own.
#   sh scripts/measure.sh ["note"]
set -eu
cd "$(dirname "$0")/.."
if [ -f .env ]; then set -a; . ./.env; set +a; fi
note=${1:-}
head=$(git rev-parse --short HEAD)
[ -z "$(git status --porcelain -- . ':!docs/measurements.md')" ] || { echo "uncommitted changes: commit first" >&2; exit 1; }
running=$(docker exec athenafs-core printenv ATHENAFS_COMMIT 2>/dev/null || echo none)
[ "$running" = "$head" ] || { echo "the running core is $running, HEAD is $head: run sh scripts/up.sh first" >&2; exit 1; }

logcount() { docker logs athenafs-core 2>&1 | grep -c "$1" || true; }
since() { echo $(( $(date +%s) - $1 )); }

# start-up: restart the core, time the mount and the time until no torrent has been added for 90 s (they arrive in
# bursts with pauses of 20 to 40 s), at most 15 minutes
docker restart -t 60 athenafs-core >/dev/null   # enough time to unmount cleanly (Docker's default 10 s is not)
t0=$(date +%s)
# mounted = the folder can be listed (a stale mount is in /proc/mounts too, but fails)
until docker exec athenafs-core sh -c 'ls /mnt/torrents/fs >/dev/null 2>&1' 2>/dev/null; do sleep 1; done
t_mount=$(since $t0)
last=-1; quiet=0; t_added=0
while [ $quiet -lt 90 ] && [ $(since $t0) -lt 900 ]; do
  n=$(logcount 'torrent added')
  if [ "$n" != "$last" ]; then last=$n; quiet=0; t_added=$(since $t0); else quiet=$((quiet + 2)); fi
  sleep 2
done

# at rest: 60 s more, then CPU (average of 5 samples), memory, page timings (median of 3)
sleep 60
cpu=$(for i in 1 2 3 4 5; do docker stats --no-stream --format '{{.CPUPerc}}' athenafs-core; sleep 2; done | tr -d % |
      awk '{s+=$1} END {printf "%.0f", s/NR}')
rss() { docker exec athenafs-core sh -c "awk '/VmRSS/ {printf \"%.1f\", \$2/1048576}' /proc/$1/status"; }
rss_core=$(rss "$(docker exec athenafs-core pidof distribyted)")
rss_admin=$(rss 1)
timing() {
  for i in 1 2 3; do curl -s -o /dev/null -m 120 -w '%{time_total}\n' "http://localhost:${ATHENAFS_PORT:-8080}$1" || echo 999; done |
    sort -n | sed -n 2p | awk '{ if ($1 >= 120) printf ">120"; else printf "%.2f", $1 }'   # >120: timed out
}
t_api=$(timing /api); t_page=$(timing /); t_act=$(timing /api/activity); t_search=$(timing '/api/search?q=text')

docker cp scripts/measure_dataset.py athenafs-core:/tmp/measure_dataset.py
set -- $(docker exec athenafs-core python3 /tmp/measure_dataset.py)
mounted=$(curl -s -m 120 "http://localhost:${ATHENAFS_PORT:-8080}/api" | python3 -c 'import json,sys; print(sum(s["status"].startswith("mounted") for s in json.load(sys.stdin)["sources"]))')

f=docs/measurements.md
[ -f "$f" ] || cat > "$f" <<'HEAD'
# Measurements

One row per measurement, made by `scripts/measure.sh` on the test host, bound to the commit the running code was built
from. Rows are only ever added. The dataset changes between rows (torrents are added): compare like with like.

- **dataset**: sources / torrents with metadata / pieces / files / total size of those torrents.
- **mount**: seconds from the restart of the core container to the FUSE mount. **added**: seconds until no torrent has
  been added for 90 s (all known torrents in their folders; at most 900). **mounted**: sources mounted at the end.
- **CPU**: core container at rest (60 s after the start-up), average of 5 samples, % of one core.
- **RSS**: resident memory in GB of Distribyted and of the supervisor (admin.py).
- **times**: seconds, median of 3: `/api`, `/` (page), `/api/activity`, `/api/search?q=text`.

| date | commit | dataset | mount s | added s | mounted | CPU % | RSS core GB | RSS admin GB | /api s | page s | activity s | search s | note |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
HEAD
echo "| $(date -u +%Y-%m-%dT%H:%MZ) | $head | $1 / $2 / $3 / $4 / $5 TB | $t_mount | $t_added | $mounted | $cpu | $rss_core | $rss_admin | $t_api | $t_page | $t_act | $t_search | $note |" >> "$f"
tail -1 "$f"
