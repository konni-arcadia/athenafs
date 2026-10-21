#!/usr/bin/env python3
"""Distribyted supervisor + status and management page.

Why a supervisor: changing what Distribyted serves (adding or removing a torrent) or erasing cached pieces while it runs
breaks reads (it still believes the pieces are complete, `unexpected EOF`). So every change is made with the service
stopped: stop, apply the change, restart (~10 s). The pieces of a torrent are found through their hashes, which are listed
in its .torrent file (for a magnet, in the metadata that the patched Distribyted saves once it has been resolved).

The torrents served are kept in /data/sources.json; the Distribyted configuration actually used is generated from the base
configuration (/config/config.yaml, everything above `routes:`) plus those sources.
"""
import base64, bisect, hashlib, hmac, html, http.server, ipaddress, json, os, re, secrets, shutil, signal, socket, subprocess
import threading, urllib.error, urllib.parse, urllib.request

CFG = os.environ.get("DISTRIBYTED_CONFIG", "/config/config.yaml")
DATA = "/data/metadata"
CACHE = f"{DATA}/cache/completed"
COMPL = f"{DATA}/piece-completion"
SEEDS = "/torrents"                    # read-only .torrent files shipped with the setup
ADDED = "/data/torrents-added"         # .torrent files added through the page
METAINFO = os.environ.get("DISTRIBYTED_METAINFO_DIR", "/data/metainfo")   # metadata of resolved magnets
SOURCES = "/data/sources.json"
SETTINGS = "/data/settings.json"
GENERATED = "/data/config.generated.yaml"
API = "http://127.0.0.1:4444/api"
MOUNT = "/mnt/torrents/fs"
MAX_TORRENT = 10_000_000               # bytes accepted for a .torrent file


def _password():
    try:
        return open("/run/admin-password").read().strip()
    except OSError:
        return ""


PASSWORD = _password()   # empty = no authentication (local network); the origin of POST requests is still checked
lock = threading.Lock()
state = {"state": "starting", "proc": None, "message": ""}


# ---------------------------------------------------------------- bencode / torrent files

def bdec(b, i=0):
    """Minimal bencode decoder: returns (value, next index)."""
    c = b[i:i + 1]
    if c == b"i":
        j = b.index(b"e", i); return int(b[i + 1:j]), j + 1
    if c == b"l":
        i += 1; r = []
        while b[i:i + 1] != b"e":
            v, i = bdec(b, i); r.append(v)
        return r, i + 1
    if c == b"d":
        i += 1; r = {}
        while b[i:i + 1] != b"e":
            k, i = bdec(b, i); v, i = bdec(b, i); r[k] = v
        return r, i + 1
    j = b.index(b":", i); n = int(b[i:j]); return b[j + 1:j + 1 + n], j + 1 + n


def parse_torrent(raw):
    """Parse the bytes of a .torrent file. Returns a dict (name, total, piece hashes, piece length, files, info-hash)."""
    d, _ = bdec(raw)
    info = d[b"info"]
    # info-hash = sha1 of the bencoded info dict: re-read it as it is in the file
    start = raw.index(b"4:info") + 6
    _, end = bdec(raw, start)
    ih = hashlib.sha1(raw[start:end]).hexdigest()
    p = info[b"pieces"]
    plist = [p[i:i + 20].hex() for i in range(0, len(p), 20)]
    if b"files" in info:
        fl, off = [], 0
        for f in info[b"files"]:
            path = "/".join(c.decode("utf-8", "replace") for c in f[b"path"])
            fl.append((path, off, f[b"length"])); off += f[b"length"]
        total = off
    else:
        fl, total = [(info[b"name"].decode("utf-8", "replace"), 0, info[b"length"])], info[b"length"]
    return {"hash": ih, "name": info[b"name"].decode("utf-8", "replace"), "total": total, "hs": set(plist),
            "plist": plist, "pl": info[b"piece length"], "files": fl}


def torrent_pieces():
    """info-hash (hex) -> parsed torrent, for every known .torrent (shipped, added, or saved from a magnet)."""
    out = {}
    for d in (SEEDS, ADDED, METAINFO):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".torrent"):
                continue
            try:
                t = parse_torrent(open(os.path.join(d, fn), "rb").read())
                t["fn"] = os.path.join(d, fn)
                out.setdefault(t["hash"], t)
            except Exception as e:  # unreadable file: ignore it
                print("unreadable torrent", fn, e)
    return out


def magnet_hash(uri):
    """Info-hash (hex) of a magnet link, or None if it is not a valid btih magnet."""
    if not uri.startswith("magnet:?"):
        return None
    for v in urllib.parse.parse_qs(uri[8:]).get("xt", []):
        if v.lower().startswith("urn:btih:"):
            h = v[9:]
            if re.fullmatch(r"[0-9a-fA-F]{40}", h):
                return h.lower()
            if re.fullmatch(r"[A-Za-z2-7]{32}", h):
                return base64.b32decode(h.upper()).hex()
    return None


# ---------------------------------------------------------------- sources (what is served)

def sanitize(name):
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return (s or "torrent")[:60]


def source_hash(src):
    if src["kind"] == "magnet":
        return magnet_hash(src["value"])
    try:
        return parse_torrent(open(src["value"], "rb").read())["hash"]
    except Exception:
        return None


def load_sources():
    try:
        return json.load(open(SOURCES))
    except (OSError, ValueError):
        pass
    # first run: take the routes of the base configuration
    out, route = [], None
    in_routes = False
    for line in open(CFG):
        if line.startswith("routes:"):
            in_routes = True; continue
        if not in_routes:
            continue
        m = re.match(r"\s*-\s*name:\s*(\S+)", line)
        if m:
            route = m.group(1); continue
        m = re.match(r'\s*-\s*torrent_path:\s*"?([^"\s]+)"?', line)
        if m and route:
            out.append({"id": secrets.token_hex(4), "route": route, "kind": "seed", "value": m.group(1)})
        m = re.match(r'\s*-\s*magnet_uri:\s*"?([^"\s]+)"?', line)
        if m and route:
            out.append({"id": secrets.token_hex(4), "route": route, "kind": "magnet", "value": m.group(1)})
    save_sources(out)
    return out


def save_sources(sources):
    tmp = SOURCES + ".tmp"
    with open(tmp, "w") as f:
        json.dump(sources, f, indent=1)
    os.replace(tmp, SOURCES)


def unique_route(sources, wanted):
    base, n, taken = sanitize(wanted), 1, {s["route"] for s in sources}
    name = base
    while name in taken:
        n += 1; name = f"{base}-{n}"
    return name


def generate_config(sources):
    """Base configuration (everything above `routes:`) + one route per source."""
    base = []
    for line in open(CFG):
        if line.startswith("routes:"):
            break
        base.append(line)
    cap = effective_cap_mb(load_settings())
    base = [re.sub(r"(global_cache_size:\s*)\d+", rf"\g<1>{cap}", l) if "global_cache_size:" in l else l for l in base]
    out = "".join(base).rstrip("\n") + "\n"
    if not sources:
        out += "routes: []\n"
    else:
        out += "routes:\n"
        for s in sources:
            key = "magnet_uri" if s["kind"] == "magnet" else "torrent_path"
            out += f"  - name: {s['route']}\n    torrents:\n      - {key}: {json.dumps(s['value'])}\n"
    with open(GENERATED, "w") as f:
        f.write(out)


# ---------------------------------------------------------------- cache cap and disk space

MARGIN_MB = 1000   # never let the cap use the last 1 GB of the disk


def base_cap_mb():
    """The cap written in the base configuration (global_cache_size)."""
    for line in open(CFG):
        m = re.match(r"\s*global_cache_size:\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return 2048


def load_settings():
    d = {"mode": "manual", "cache_mb": base_cap_mb(), "reserve_pct": 20}
    try:
        d.update(json.load(open(SETTINGS)))
    except (OSError, ValueError):
        pass
    return d


def save_settings(d):
    with open(SETTINGS, "w") as f:
        json.dump(d, f)


def disk():
    """(total, free) bytes of the filesystem that holds the cache."""
    u = shutil.disk_usage("/data")
    return u.total, u.free


def max_cap_mb():
    """Largest cap that still leaves a margin: what is free now plus what the cache already occupies."""
    return max(0, int((disk()[1] + sum(cache_files().values())) / 1e6) - MARGIN_MB)


def effective_cap_mb(st):
    """Fixed: the chosen size. Automatic: keep reserve_pct % of the whole disk free once the cache is full."""
    if st["mode"] == "auto":
        reserve_mb = int(disk()[0] * st["reserve_pct"] / 100 / 1e6)
        return max(256, min(max_cap_mb(), max_cap_mb() + MARGIN_MB - reserve_mb))
    return int(st["cache_mb"])


# ---------------------------------------------------------------- Distribyted process

def cache_files():
    try:
        return {h: os.path.getsize(os.path.join(CACHE, h)) for h in os.listdir(CACHE)}
    except OSError:
        return {}


def api(path):
    try:
        with urllib.request.urlopen(API + path, timeout=3) as r:
            return json.load(r)
    except Exception:
        return None


def start():
    generate_config(load_sources())
    state["proc"] = subprocess.Popen(["/bin/distribyted", "--config", GENERATED, "--http-port", "4444"])
    state["state"] = "running"


def stop():
    p = state.get("proc")
    if p and p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
    # only unmount if a FUSE filesystem is still there: MOUNT's parent is also the host's bind mount (rshared),
    # an unconditional umount would propagate to the host and remove it.
    for line in open("/proc/mounts"):
        c = line.split()
        if len(c) > 2 and c[1] == MOUNT and c[2].startswith("fuse"):
            subprocess.run(["umount", "-l", MOUNT], capture_output=True)
    state["proc"] = None


def exclusive(fn):
    """Run fn() with the service stopped, under the lock, then restart. fn returns the message to display."""
    with lock:
        state["state"] = "stopping, applying the change, restarting…"
        try:
            stop()
            state["message"] = fn() or ""
        except Exception as e:
            state["message"] = f"error: {e}"
        finally:
            start()


# ---------------------------------------------------------------- erasing cached pieces

def is_padding(path):
    """Padding files (piece alignment): no real data, neither shown nor blocking."""
    return "/.____padding_file/" in "/" + path or path.startswith(".____padding_file")


def pieces_to_erase(t, path):
    """(pieces to erase, number of pieces kept because shared with an unchosen file).
    path '' = the whole torrent; otherwise a file, or a folder (prefix). A piece straddling two files
    is only erased if every file it covers is chosen."""
    fl, pl, plist = t["files"], t["pl"], t["plist"]
    chosen = {i for i, (c, o, n) in enumerate(fl) if path == "" or c == path or c.startswith(path + "/")}
    starts = [o for _, o, _ in fl]; ends = [o + n for _, o, n in fl]

    def owners(p):
        i = bisect.bisect_right(ends, p * pl); r = set()
        while i < len(fl) and starts[i] < (p + 1) * pl:
            if fl[i][2] > 0 and ends[i] > p * pl and not is_padding(fl[i][0]):
                r.add(i)
            i += 1
        return r

    cand = set()
    for i in chosen:
        c, o, n = fl[i]
        if n > 0:
            cand.update(range(o // pl, (o + n - 1) // pl + 1))
    ok = {p for p in cand if owners(p) <= chosen}
    return {plist[p] for p in ok}, len(cand) - len(ok)


def delete_pieces(hashes):
    """Delete cached pieces and reset the piece-completion database. Returns (count, bytes)."""
    files = cache_files()
    n = sz = 0
    for h in hashes & set(files):
        try:
            os.remove(os.path.join(CACHE, h)); n += 1; sz += files[h]
        except OSError:
            pass
    shutil.rmtree(COMPL, ignore_errors=True)  # otherwise Distribyted believes the pieces are complete
    os.makedirs(COMPL, exist_ok=True)
    return n, sz


def erase(ih, path=""):
    """ih = info-hash of a torrent, or 'all'. path: file or folder inside the torrent ('' = the whole torrent)."""
    tp = torrent_pieces()
    if ih != "all" and ih not in tp:
        state["message"] = "unknown torrent (no .torrent file): nothing erased"; return
    if ih != "all" and path and not any(c == path or c.startswith(path + "/") for c, _, _ in tp[ih]["files"]):
        state["message"] = "unknown path in this torrent: nothing erased"; return

    def job():
        if ih == "all":
            n, sz = delete_pieces(set(cache_files())); what, kept = "everything", 0
        else:
            wanted, kept = pieces_to_erase(tp[ih], path)
            n, sz = delete_pieces(wanted); what = tp[ih]["name"] + ("/" + path if path else "")
        rest = f"; {kept} edge piece(s) kept, shared with neighbouring files that were not chosen" if kept else ""
        return f"erased: {n} pieces, {sz / 1e6:.0f} MB ({what}){rest}"
    exclusive(job)


def apply_settings(mode, cache_gb, reserve_pct):
    """Validate and apply the cache cap settings (restarts the service)."""
    try:
        cache_mb, reserve = int(float(cache_gb) * 1000), float(reserve_pct)
    except ValueError:
        state["message"] = "invalid number: settings unchanged"; return
    if mode not in ("manual", "auto") or not (1 <= reserve <= 90):
        state["message"] = "invalid settings (the free-space reserve must be between 1 and 90 %): unchanged"; return
    top = max_cap_mb()
    if mode == "manual" and not (256 <= cache_mb <= top):
        state["message"] = (f"a fixed cap must be between 0.3 GB and {top / 1000:.1f} GB right now "
                            f"(free space + current cache − 1 GB): unchanged"); return
    new = {"mode": mode, "cache_mb": cache_mb, "reserve_pct": reserve}

    def job():
        save_settings(new)
        generate_config(load_sources())
        return f"cache cap now {effective_cap_mb(new) / 1000:.1f} GB ({'automatic' if mode == 'auto' else 'fixed'})"
    exclusive(job)


# ---------------------------------------------------------------- adding and removing torrents

def check_public_url(url):
    """Only http(s) URLs whose host resolves to public addresses (this page is open on the local network)."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("not an http(s) URL")
    for fam, _, _, _, sa in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80)):
        ip = ipaddress.ip_address(sa[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("the host resolves to a non-public address")


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_torrent(url):
    check_public_url(url)
    op = urllib.request.build_opener(_CheckedRedirect)
    with op.open(urllib.request.Request(url, headers={"User-Agent": "distribyted-admin"}), timeout=20) as r:
        data = r.read(MAX_TORRENT + 1)
    if len(data) > MAX_TORRENT:
        raise ValueError("file too large")
    return data


def add_torrent_bytes(raw, wanted_route=""):
    """Add a .torrent file (bytes). Returns the message."""
    try:
        t = parse_torrent(raw)
    except Exception:
        return "not a valid .torrent file: nothing added"
    sources = load_sources()
    if any(source_hash(s) == t["hash"] for s in sources):
        return f"already present: {t['name']}"
    sid = secrets.token_hex(4)
    os.makedirs(ADDED, exist_ok=True)
    fn = os.path.join(ADDED, sid + ".torrent")
    with open(fn, "wb") as f:
        f.write(raw)
    sources.append({"id": sid, "route": unique_route(sources, wanted_route or t["name"]), "kind": "file", "value": fn})
    save_sources(sources)
    return f"added: {t['name']} ({t['total'] / 1e6:.0f} MB)"


def add_link(link, wanted_route=""):
    """Add a magnet link or the URL of a .torrent file. Starts in a thread, restarts the service."""
    link = link.strip()
    if link.startswith("magnet:"):
        h = magnet_hash(link)
        if not h:
            state["message"] = "not a valid magnet link (needs xt=urn:btih:…): nothing added"; return
        sources = load_sources()
        if any(source_hash(s) == h for s in sources):
            state["message"] = "already present: that magnet"; return
        dn = urllib.parse.parse_qs(link[8:]).get("dn", [""])[0]

        def job():
            srcs = load_sources()
            srcs.append({"id": secrets.token_hex(4), "route": unique_route(srcs, wanted_route or dn or h[:8]),
                         "kind": "magnet", "value": link})
            save_sources(srcs)
            return f"added magnet {dn or h[:8]}: waiting for its metadata (up to the add timeout)"
        exclusive(job)
        return
    try:
        raw = fetch_torrent(link)
    except Exception as e:
        state["message"] = f"could not fetch that URL: {e}"; return
    exclusive(lambda: add_torrent_bytes(raw, wanted_route))


def remove_source(sid, erase_data):
    srcs = load_sources()
    src = next((s for s in srcs if s["id"] == sid), None)
    if not src:
        state["message"] = "unknown source: nothing removed"; return

    def job():
        h = source_hash(src)
        tp = torrent_pieces()
        msg = f"removed {src['route']}"
        if erase_data:
            t = tp.get(h) if h else None
            if t:
                n, sz = delete_pieces(pieces_to_erase(t, "")[0])
                msg += f"; erased {n} pieces, {sz / 1e6:.0f} MB"
            else:
                msg += "; its data could not be attributed (no metadata), use “Erase everything” if needed"
        else:
            msg += "; data kept"
        # files that only this source needed
        if src["kind"] == "file":
            try: os.remove(src["value"])
            except OSError: pass
        if h:   # the metadata saved by Distribyted for this torrent (it is saved again if the torrent is served again)
            try: os.remove(os.path.join(METAINFO, h + ".torrent"))
            except OSError: pass
        save_sources([s for s in load_sources() if s["id"] != sid])
        return msg
    exclusive(job)


# ---------------------------------------------------------------- status

def tree(t, files):
    """Folders/files of a torrent that have cached data (size ≈ cached pieces covering the file)."""
    plist, pl = t["plist"], t["pl"]
    cb = [0]
    for h in plist:
        cb.append(cb[-1] + files.get(h, 0))
    root = {"name": "", "path": "", "size": 0, "children": {}}
    for path, o, n in t["files"]:
        if n <= 0 or is_padding(path):
            continue
        a, b = o // pl, (o + n - 1) // pl
        sz = cb[b + 1] - cb[a]
        if sz <= 0:
            continue
        nd = root; nd["size"] += sz; cur = ""
        for comp in path.split("/"):
            cur = f"{cur}/{comp}" if cur else comp
            nd = nd["children"].setdefault(comp, {"name": comp, "path": cur, "size": 0, "children": {}})
            nd["size"] += sz
    return root


def snapshot():
    tp = torrent_pieces(); files = cache_files()
    routes = api("/routes") or []
    live = {t["hash"]: (r["name"], t) for r in routes for t in r.get("torrentStats", [])}
    items = []
    for ih, t in tp.items():
        mine = t["hs"] & set(files)
        sz = sum(files[h] for h in mine)
        route, st_ = live.get(ih, ("?", {}))
        if route == "?" and sz == 0:
            continue  # .torrent present but not mounted, nothing cached
        items.append({"hash": ih, "name": t["name"], "route": route, "total": t["total"], "cache": sz,
                      "pieces": f"{len(mine)}/{len(t['hs'])}", "peers": st_.get("peers", 0),
                      "tree": tree(t, files) if sz else None})
    srcs = []
    for s in load_sources():
        h = source_hash(s)
        if h in live:
            status = "mounted"
        elif s["kind"] == "magnet" and h not in tp:
            status = "waiting for metadata"
        else:
            status = "not mounted"
        srcs.append({"id": s["id"], "route": s["route"], "kind": s["kind"], "hash": h, "status": status,
                     "name": tp[h]["name"] if h in tp else (urllib.parse.parse_qs(s["value"][8:]).get("dn", [""])[0] if s["kind"] == "magnet" else "")})
    st = api("/status") or {}
    total_d, free_d = disk()
    unknown = sum(files.values()) - sum(i["cache"] for i in items)
    return {"state": state["state"], "message": state["message"], "total_cache": sum(files.values()),
            "capacity": st.get("cacheCapacity"), "items": items, "sources": srcs, "unknown": unknown,
            "disk_total": total_d, "disk_free": free_d, "max_cap_mb": max_cap_mb(), "settings": load_settings(),
            "network_rx": network()[0], "network_tx": network()[1]}


def network():
    """Bytes received and sent by the container since it started (eth0 counters)."""
    try:
        r = int(open("/sys/class/net/eth0/statistics/rx_bytes").read())
        t = int(open("/sys/class/net/eth0/statistics/tx_bytes").read())
        return r, t
    except OSError:
        return 0, 0


def mb(n):
    return f"{n / 1e6:,.0f} MB".replace(",", " ") if n < 1e9 else f"{n / 1e9:.2f} GB"


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Mounted torrents: status</title>
<style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--acc:#b3261e}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181a;--fg:#e8e8e8;--mut:#9a9a9a;--line:#33373a;--acc:#ff8a80}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0;padding:16px;max-width:980px;margin:auto}}
h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:16px;margin:20px 0 4px}}.mut{{color:var(--mut)}}
table{{width:100%;border-collapse:collapse;margin:8px 0 16px}}th,td{{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}}
td.n{{text-align:right;white-space:nowrap}}button{{font:inherit;padding:6px 10px;border:1px solid var(--acc);color:var(--acc);background:none;border-radius:6px;cursor:pointer}}
button:disabled{{opacity:.4;cursor:default}}.msg{{padding:8px 10px;border:1px solid var(--line);border-radius:6px;margin:8px 0}}
.wrap{{overflow-x:auto}}details{{margin:2px 0}}summary{{cursor:pointer}}
.tree{{margin:4px 0 8px 14px}}.row{{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:2px 0}}
.row form,summary form,td form{{display:inline;margin:0}}.row button,summary button{{padding:2px 8px;font-size:13px}}
.sz{{white-space:nowrap;color:var(--mut)}}
input[type=text],input[type=number]{{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg)}}
.add{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}}.add .l{{flex:1 1 380px}}.add .f{{flex:0 1 180px}}
</style>
<h1>Mounted torrents</h1>
<div class=mut>Add a magnet link, the URL of a .torrent file, or a .torrent file. Adding or removing restarts the service (~10 s).</div>
<form class=add method=post action=/add>
  <input class=l type=text name=link placeholder="magnet:?xt=urn:btih:…  or  https://…/file.torrent" required>
  <input class=f type=text name=route placeholder="folder name (optional)">
  <button type=submit>Add</button>
</form>
<div class=add><span class=mut>or a file:</span> <input type=file id=file accept=".torrent"
  onchange="const f=this.files[0];if(!f)return;fetch('/upload',{{method:'POST',headers:{{'X-Filename':encodeURIComponent(f.name)}},body:f}}).then(()=>location.reload())"></div>
{capform}
<main id=m>
<div class=mut>Service: <b>{state}</b>. Cache on disk: <b>{cache}</b>{cap}. Container network since it started: <b>{rx}</b> received, <b>{tx}</b> sent. Refreshes every 5 s.</div>
<div class=mut>Disk: <b>{dfree}</b> free of {dtotal}. Largest cache cap possible now: <b>{dmax}</b> (free space + what is already cached − 1 GB).</div>
{msg}
<h2>Sources</h2>
<div class=wrap><table>
<tr><th>Folder</th><th>Torrent</th><th>Status</th><th></th></tr>
{sources}
</table></div>
<h2>Cache</h2>
<div class=wrap><table>
<tr><th>Torrent</th><th class=n>Size</th><th class=n>Cached</th><th></th></tr>
{rows}
</table></div>
{unknown}
<form method=post action=/erase><input type=hidden name=h value=all>
<button {dis} onclick="return confirm('Erase ALL local data? The service restarts (a few seconds) and reads in progress fail.')">Erase everything</button></form>
<p class=mut>Erasing or removing restarts the service: the mounts are unavailable for a few seconds and reads in progress fail.
It does not delete the .torrent files of the setup or the configuration.</p>
</main>
<script>
setInterval(async()=>{{try{{
  const o=new Set([...document.querySelectorAll('details[open]')].map(d=>d.dataset.k));
  const t=await (await fetch('/',{{cache:'no-store'}})).text();
  const n=new DOMParser().parseFromString(t,'text/html').getElementById('m'); if(!n)return;
  n.querySelectorAll('details').forEach(d=>{{if(o.has(d.dataset.k))d.open=true}});
  document.getElementById('m').replaceWith(n);
}}catch(e){{}}}},5000)
</script>"""


def node(ih, n, dis):
    """HTML of one folder or file of the tree, with its Erase button."""
    button = (f"<form method=post action=/erase><input type=hidden name=h value={ih}><input type=hidden name=p value=\"{html.escape(n['path'], quote=True)}\">"
              f"<button {dis} onclick=\"return confirm('Erase the local data of {html.escape(n['name'], quote=True)}?')\">Erase</button></form>")
    if not n["children"]:
        return f"<div class=row><span>{html.escape(n['name'])}</span><span class=sz>≈ {mb(n['size'])} {button}</span></div>"
    kids = sorted(n["children"].values(), key=lambda x: -x["size"])
    body = "".join(node(ih, e, dis) for e in kids[:300])
    more = f"<div class=mut>… {len(kids) - 300} more</div>" if len(kids) > 300 else ""
    return (f"<details data-k=\"{ih}/{html.escape(n['path'], quote=True)}\"><summary>{html.escape(n['name'])}/ "
            f"<span class=sz>≈ {mb(n['size'])} {button}</span></summary><div class=tree>{body}{more}</div></details>")


def render():
    s = snapshot(); dis = "" if s["state"] == "running" else "disabled"
    rows = ""
    for i in s["items"]:
        rows += (f"<tr><td><b>{html.escape(i['name'])}</b><br><span class=mut>{html.escape(i['route'])} · {i['pieces']} pieces · {i['peers']} peers</span></td>"
                 f"<td class=n>{mb(i['total'])}</td><td class=n>{mb(i['cache'])}</td>"
                 f"<td><form method=post action=/erase><input type=hidden name=h value={i['hash']}>"
                 f"<button {dis} onclick=\"return confirm('Erase the local data of this whole torrent?')\">Whole torrent</button></form></td></tr>")
        if i["tree"]:
            kids = sorted(i["tree"]["children"].values(), key=lambda x: -x["size"])
            body = "".join(node(i["hash"], e, dis) for e in kids[:300])
            more = f"<div class=mut>… {len(kids) - 300} more at the top level</div>" if len(kids) > 300 else ""
            rows += (f"<tr><td colspan=4><details data-k=\"{i['hash']}\"><summary>Files with cached data</summary>"
                     f"<div class=tree>{body}{more}</div></details></td></tr>")
    if not rows:
        rows = "<tr><td colspan=4 class=mut>Nothing cached.</td></tr>"
    srows = ""
    for x in s["sources"]:
        srows += (f"<tr><td>{html.escape(x['route'])}</td><td>{html.escape(x['name'] or (x['hash'] or '')[:12])}<br><span class=mut>{x['kind']}</span></td>"
                  f"<td>{x['status']}</td><td class=n><form method=post action=/remove><input type=hidden name=id value={x['id']}>"
                  f"<button {dis} name=erase value=1 onclick=\"return confirm('Remove this torrent and erase its local data?')\">Remove</button> "
                  f"<button {dis} name=erase value=0 onclick=\"return confirm('Remove this torrent and keep its local data?')\">Remove, keep data</button></form></td></tr>")
    if not srows:
        srows = "<tr><td colspan=4 class=mut>No torrent yet: add one above.</td></tr>"
    msg = f"<div class=msg>{html.escape(s['message'])}</div>" if s["message"] else ""
    cap = f" of a {s['capacity']} MB limit" if s["capacity"] else ""
    unk = (f"<p class=mut>{mb(s['unknown'])} of cache belongs to no known torrent: "
           f"only “Erase everything” removes it.</p>") if s["unknown"] > 1e6 else ""
    st = s["settings"]
    capform = ("<form class=add method=post action=/settings><span class=mut>Cache cap:</span>"
               f"<label><input type=radio name=mode value=manual {'checked' if st['mode'] == 'manual' else ''}> fixed</label>"
               f"<input type=number name=cache_gb step=0.5 min=0.5 value={st['cache_mb'] / 1000:g} style=\"width:90px\"> GB"
               f"<label><input type=radio name=mode value=auto {'checked' if st['mode'] == 'auto' else ''}> automatic: keep</label>"
               f"<input type=number name=reserve_pct step=1 min=1 max=90 value={st['reserve_pct']:g} style=\"width:70px\"> % of the disk free"
               "<button type=submit>Apply (restarts)</button></form>")
    return PAGE.format(capform=capform, dfree=mb(s["disk_free"]), dtotal=mb(s["disk_total"]), dmax=mb(s["max_cap_mb"] * 1e6),
                       state=s["state"], cache=mb(s["total_cache"]), cap=cap, msg=msg, rows=rows, sources=srows, unknown=unk,
                       dis=dis, rx=mb(s["network_rx"]), tx=mb(s["network_tx"]))


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def authorized(self):
        """Basic auth (user "admin") only if a password is provided. For a POST, the origin must be the page itself (anti-CSRF)."""
        a = self.headers.get("Authorization", "")
        ok = not PASSWORD
        if PASSWORD and a.startswith("Basic "):
            try:
                u, _, m = base64.b64decode(a[6:]).decode().partition(":")
                ok = hmac.compare_digest(u, "admin") and hmac.compare_digest(m, PASSWORD)
            except Exception:
                ok = False
        if ok and self.command == "POST":
            o = self.headers.get("Origin") or self.headers.get("Referer") or ""
            ok = bool(o) and o.split("//", 1)[-1].split("/", 1)[0] == self.headers.get("Host", "")
        if not ok:
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Mounted torrents"')
            self.send_header("Content-Length", "0"); self.end_headers()
        return ok

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == "/api":
            body = json.dumps(snapshot()).encode(); ct = "application/json"
        else:
            body = render().encode(); ct = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if not self.authorized():
            return
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_TORRENT:
            self.send_response(413); self.send_header("Content-Length", "0"); self.end_headers(); return
        raw = self.rfile.read(n)
        go = lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start()
        if self.path == "/upload":
            name = urllib.parse.unquote(self.headers.get("X-Filename", "")).rsplit(".torrent", 1)[0]
            go(lambda: exclusive(lambda: add_torrent_bytes(raw, name)))
        else:
            q = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            one = lambda k: (q.get(k) or [""])[0]
            if self.path == "/erase" and (one("h") == "all" or re.fullmatch(r"[0-9a-f]{40}", one("h"))):
                go(erase, one("h"), one("p"))
            elif self.path == "/settings":
                go(apply_settings, one("mode"), one("cache_gb") or "0", one("reserve_pct") or "20")
            elif self.path == "/add" and one("link"):
                go(add_link, one("link"), one("route"))
            elif self.path == "/remove" and re.fullmatch(r"[0-9a-f]{8}", one("id")):
                go(remove_source, one("id"), one("erase") != "0")
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()


def main():
    start()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H)
    signal.signal(signal.SIGTERM, lambda *a: (stop(), os._exit(0)))
    srv.serve_forever()


main()
