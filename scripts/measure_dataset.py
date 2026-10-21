# SPDX-License-Identifier: GPL-3.0-or-later
"""Size of the dataset served: run inside the core container by scripts/measure.sh (after the timings, since parsing
every .torrent costs memory and CPU). Prints: sources torrents pieces files terabytes."""
import json, os, sys

sys.argv = ["admin"]
src = open("/app/admin.py").read().rsplit("\nmain()", 1)[0]
g = {"__name__": "measure"}
exec(compile(src, "/app/admin.py", "exec"), g)
tp = g["torrent_pieces"]()
sources = g["load_sources"]()
known = {g["source_hash"](s) for s in sources} & set(tp)
ts = [tp[h] for h in known]
print(len(sources), len(ts), sum(t["npieces"] if "npieces" in t else len(t["plist"]) for t in ts), sum(len(t["files"]) for t in ts),
      round(sum(t["total"] for t in ts) / 1e12, 1))
