#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Distribyted supervisor + status and management page.

Why a supervisor: changing what Distribyted serves (adding or removing a torrent) or erasing cached pieces while it runs
breaks reads (it still believes the pieces are complete, `unexpected EOF`). So every change is made with the service
stopped: stop, apply the change, restart (~10 s). The pieces of a torrent are found through their hashes, which are listed
in its .torrent file (for a magnet, in the metadata that the patched Distribyted saves once it has been resolved).

The torrents served are kept in /data/sources.json; the Distribyted configuration actually used is generated from the base
configuration (/config/config.yaml, everything above `routes:`) plus those sources.
"""
import base64, bisect, collections, functools, math, struct, hashlib, sqlite3, hmac, html, http.server, ipaddress, json, os, re, secrets, shutil, signal, socket, subprocess
import threading, time, urllib.error, urllib.parse, urllib.request

CFG = os.environ.get("DISTRIBYTED_CONFIG", "/config/config.yaml")
DATA = "/data/metadata"
CACHE = f"{DATA}/cache/completed"
COMPL = f"{DATA}/piece-completion"
SEEDS = "/torrents"                    # read-only .torrent files shipped with the setup
ADDED = "/data/torrents-added"         # .torrent files added through the page
INDEX = "/data/search.db"              # file-name index for the search (rebuilt from the .torrent files if deleted)
METAINFO = os.environ.get("DISTRIBYTED_METAINFO_DIR", "/data/metainfo")   # metadata of resolved magnets
SOURCES = "/data/sources.json"
SETTINGS = "/data/settings.json"
ALIASES = "/data/aliases.json"         # renamed folder -> its new name, followed by later adds
GENERATED = "/data/config.generated.yaml"
API = "http://127.0.0.1:4444/api"
MOUNT = "/mnt/torrents/fs"
MAX_TORRENT = 10_000_000               # bytes accepted for a .torrent file


def _password():
    """ATHENAFS_PASSWORD (from .env), else the file /run/admin-password (a Docker secret, for example)."""
    if os.environ.get("ATHENAFS_PASSWORD"):
        return os.environ["ATHENAFS_PASSWORD"]
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
    p = bytes(info[b"pieces"])   # piece hashes, 20 bytes each: kept as one block (a list of strings costs ~10x more)
    if b"files" in info:
        fl, off = [], 0
        for f in info[b"files"]:
            path = "/".join(c.decode("utf-8", "replace") for c in f[b"path"])
            fl.append((path, off, f[b"length"])); off += f[b"length"]
        total = off
    else:
        fl, total = [(info[b"name"].decode("utf-8", "replace"), 0, info[b"length"])], info[b"length"]
    trackers = [t.decode("utf-8", "replace") for tier in d.get(b"announce-list", []) for t in tier if isinstance(t, bytes)]
    if not trackers and isinstance(d.get(b"announce"), bytes):
        trackers = [d[b"announce"].decode("utf-8", "replace")]
    return {"hash": ih, "name": info[b"name"].decode("utf-8", "replace"), "total": total, "multi": b"files" in info,
            "pieces": p, "npieces": len(p) // 20, "pl": info[b"piece length"], "files": fl, "trackers": trackers}


def piece(t, i):
    """Hash (hex, the name of its file in the cache) of piece i of torrent t."""
    return t["pieces"][20 * i:20 * i + 20].hex()


def files_of_piece(t, i, size):
    """(file index, bytes of piece i in that file) for every file the piece overlaps; size = the piece's size."""
    o = i * t["pl"]; k = max(0, bisect.bisect_right(t["offsets"], o) - 1)
    while k < len(t["files"]) and t["files"][k][1] < o + size:
        _, fo, fl = t["files"][k]
        part = min(o + size, fo + fl) - max(o, fo)
        if part > 0:
            yield k, part
        k += 1


_parsed = {}   # path -> (mtime, size, parsed torrent): a .torrent is parsed again only when it changes


def torrent_pieces():
    """info-hash (hex) -> parsed torrent, for every known .torrent (shipped, added, or saved from a magnet)."""
    out, seen = {}, set()
    for d in (SEEDS, ADDED, METAINFO):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".torrent"):
                continue
            path = os.path.join(d, fn); seen.add(path)
            try:
                st = os.stat(path)
                c = _parsed.get(path)
                if not c or c[:2] != (st.st_mtime, st.st_size):
                    t = parse_torrent(open(path, "rb").read())
                    t["fn"] = path
                    t["offsets"] = [o for _, o, _ in t["files"]]
                    c = _parsed[path] = (st.st_mtime, st.st_size, t)
                out.setdefault(c[2]["hash"], c[2])
            except Exception as e:  # unreadable file: ignore it
                print("unreadable torrent", fn, e)
    for path in set(_parsed) - seen:
        del _parsed[path]
    return out


def flat_folders(tp, sources=None):
    """folder -> True when its torrents' own top-level directory is skipped in the share: every torrent of the folder is a
    multi-file torrent with the same top-level name (same rule as core/patches/flatten_single_root.patch)."""
    roots = {}
    for s in load_sources() if sources is None else sources:
        t = tp.get(source_hash(s))
        if t:
            roots.setdefault(s["route"], set()).add(t["name"] if t["multi"] else None)
    return {r: len(n) == 1 and None not in n for r, n in roots.items()}


def share_path(route, t, path, flat):
    """Where a file of torrent t appears in the share."""
    if t["multi"] and not flat:
        return f"{route}/{t['name']}/{path}"
    return f"{route}/{path}" if t["multi"] else f"{route}/{t['name']}"


@functools.lru_cache(maxsize=100_000)
def magnet_dn(uri):
    return urllib.parse.parse_qs(uri[8:]).get("dn", [""])[0]


@functools.lru_cache(maxsize=100_000)
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


def load_aliases():
    try:
        return json.load(open(ALIASES))
    except (OSError, ValueError):
        return {}


def route_for(wanted, explicit=False):
    """Folder of a new source. A folder that already exists is joined, not renamed: torrents with the same folder name are
    served merged in one folder (Distribyted overlays the file trees of the torrents of a route). A name that was not typed
    on the page (the torrent's own name) follows the renames made since, so a later part of a renamed collection joins it."""
    r = sanitize(wanted)
    if not explicit:
        aliases, seen = load_aliases(), set()
        while r in aliases and r not in seen:
            seen.add(r); r = aliases[r]
    return r


def rename(new, route="", sid=""):
    """Rename a folder (route) or move one source (sid) to another folder. An existing name merges. Restarts the service."""
    new = sanitize(new)
    srcs = load_sources()
    moving = [s for s in srcs if (s["route"] == route if route else s["id"] == sid)]
    if not moving:
        state["message"] = "unknown folder or source: nothing renamed"; return
    old = moving[0]["route"]
    if all(s["route"] == new for s in moving):
        state["message"] = f"already in {new}"; return
    merged = any(s["route"] == new for s in srcs if s not in moving)

    def job():
        ids = {s["id"] for s in moving}
        cur = load_sources()
        for s in cur:
            if s["id"] in ids:
                s["route"] = new
        save_sources(cur)
        if route:   # its settings follow (the ready view's name too, unless it was customised)
            fs_ = load_folder_settings()
            if old in fs_ and not any(s["route"] == old for s in cur):
                v = fs_.pop(old)
                if v.get("view") == view_default(old):
                    v["view"] = view_default(new)
                if new not in fs_:
                    fs_[new] = v
                save_folder_settings(fs_)
                write_hidden_folders()
        if route:   # a whole folder: later adds of the old name follow
            a = {k: (new if v == old else v) for k, v in load_aliases().items()}
            a[old] = new
            a.pop(new, None)
            with open(ALIASES + ".tmp", "w") as f:
                json.dump({k: v for k, v in a.items() if k != v}, f, indent=1)
            os.replace(ALIASES + ".tmp", ALIASES)
            return f"{'merged' if merged else 'renamed'} folder {old} {'into' if merged else 'to'} {new}"
        return f"moved a torrent from {old} to {'the existing folder ' if merged else ''}{new}"
    exclusive(job)


def saved_metainfo(h):
    """Path of the metadata saved for a magnet once resolved, if it is complete and matches; else None.
    Starting a magnet from it skips the metadata lookup (up to add_timeout per magnet at every start)."""
    fn = os.path.join(METAINFO, f"{h}.torrent")
    try:
        return fn if h and parse_torrent(open(fn, "rb").read())["hash"] == h else None
    except Exception:
        return None


def folder_trackers(sources):
    """folder -> trackers for its magnets that have none: the trackers found in at least half of the folder's magnets
    that do have some (a collection split in parts usually shares them), plus ATHENAFS_EXTRA_TRACKERS (comma-separated).
    Without a tracker a magnet can only find peers through the DHT, which the client limits to 25 queries a second for
    everything: 400 trackerless magnets took hours to get their metadata."""
    extra = [t.strip() for t in os.environ.get("ATHENAFS_EXTRA_TRACKERS", "").split(",") if t.strip()]
    per = {}
    for s in sources:
        if s["kind"] == "magnet":
            tr = urllib.parse.parse_qs(s["value"][8:]).get("tr", [])
            c = per.setdefault(s["route"], [collections.Counter(), 0])
            if tr:
                c[0].update(set(tr)); c[1] += 1
    out = {}
    for route, (c, n) in per.items():
        common = [t for t, k in c.most_common() if k * 2 >= n] if n else []
        out[route] = common + [t for t in extra if t not in common]
    return out


def with_trackers(magnet, trackers):
    """The magnet link with trackers added when it has none (the stored source is not changed)."""
    if not trackers or "tr" in urllib.parse.parse_qs(magnet[8:]):
        return magnet
    return magnet + "".join("&tr=" + urllib.parse.quote(t, safe="") for t in trackers)


def generate_config(sources):
    """Base configuration (everything above `routes:`) + one route per source."""
    base = []
    for line in open(CFG):
        if line.startswith("routes:"):
            break
        base.append(line)
    cap = effective_cap_mb(load_settings())
    base = [re.sub(r"(global_cache_size:\s*)\d+", rf"\g<1>{cap}", l) if "global_cache_size:" in l else l for l in base]
    for key, env in (("add_timeout", "ATHENAFS_ADD_TIMEOUT"), ("read_timeout", "ATHENAFS_READ_TIMEOUT")):   # .env overrides
        v = os.environ.get(env, "")
        if v.isdigit():
            base = [re.sub(rf"({key}:\s*)\d+", rf"\g<1>{v}", l) for l in base]
    out = "".join(base).rstrip("\n") + "\n"
    if not sources:
        out += "routes: []\n"
    else:
        out += "routes:\n"
        routes = {}   # one route per folder, holding every source of that folder (insertion order kept)
        ftr = folder_trackers(sources)
        for s in sources:
            key, value = ("magnet_uri" if s["kind"] == "magnet" else "torrent_path"), s["value"]
            if s["kind"] == "magnet" and (saved := saved_metainfo(magnet_hash(value))):
                key, value = "torrent_path", saved
            elif s["kind"] == "magnet":
                value = with_trackers(value, ftr.get(s["route"]))
            routes.setdefault(s["route"], []).append(f"      - {key}: {json.dumps(value)}\n")
        for route, lines in routes.items():
            out += f"  - name: {route}\n    torrents:\n" + "".join(lines)
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


def env_num(name, default):
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def load_settings():
    d = {"mode": "manual", "cache_mb": base_cap_mb(), "reserve_pct": 20,
         # gentleman mode and upload cap: defaults from .env, changed on the page
         "gentle_on": os.environ.get("ATHENAFS_GENTLEMAN", "on").lower() not in ("off", "0", "false", "no"),
         "gentle_slots": int(env_num("ATHENAFS_GENTLEMAN_SLOTS", 3)),
         "gentle_pct": env_num("ATHENAFS_GENTLEMAN_SPACE_PCT", 5),
         # torrents kept awake by choice (waiting cache requests first, then gentleman), estimated; and a ceiling on
         # Distribyted's real memory above which they are put back to sleep
         "awake_mem_mb": int(env_num("ATHENAFS_AWAKE_MEMORY_MB", 2000)),
         "memory_ceiling_mb": int(env_num("ATHENAFS_MEMORY_CEILING_MB", 8000)),
         "caching_stopped": False,   # "Stop caching" on the page: nothing new is downloaded until resumed
         "upload_kbps": int(env_num("ATHENAFS_UPLOAD_LIMIT_KBPS", 1000))}
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
        return {e.name: e.stat().st_size for e in os.scandir(CACHE)}
    except OSError:
        return {}


def api(path, timeout=3):
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


ROOT_FILES = os.environ.get("DISTRIBYTED_ROOT_FILES", "/data/share-root")
HEALTH_ORDER = os.environ.get("DISTRIBYTED_HEALTH_ORDER", "/data/health-order.txt")
health = {}   # info-hash -> {"known", "connected", "seeders", "checked"}: last health check, kept in the index database


def load_health():
    try:
        c = db()
        c.execute("CREATE TABLE IF NOT EXISTS health(hash TEXT PRIMARY KEY, known INTEGER, connected INTEGER, seeders INTEGER, checked INTEGER)")
        for h, k, n, sd, ch in c.execute("SELECT hash, known, connected, seeders, checked FROM health"):
            health[h] = {"known": k, "connected": n, "seeders": sd, "checked": ch}
        c.close()
    except sqlite3.Error as e:
        print("health table:", e)


def save_health(updates):
    c = db()
    with c:
        c.executemany("INSERT OR REPLACE INTO health VALUES (?,?,?,?,?)",
                      [(h, x["known"], x["connected"], x["seeders"], x["checked"]) for h, x in updates.items()])
    c.close()


def write_health_order():
    """Order of the next health checks for Distribyted: never checked first, then the least recently checked."""
    hashes = {source_hash(s) for s in load_sources()} - {None}
    order = sorted(hashes, key=lambda h: health.get(h, {}).get("checked", 0))
    with open(HEALTH_ORDER + ".tmp", "w") as f:
        f.write("\n".join(order) + "\n")
    os.replace(HEALTH_ORDER + ".tmp", HEALTH_ORDER)


def health_text(h):
    """Short text for the page. A dormant torrent woken for its check asks for no data, so the client rarely connects to
    peers: the peers announced (DHT, trackers) are the main signal, connected seeders a bonus."""
    x = health.get(h)
    if not x:
        return "not checked yet"
    ago = time.time() - x["checked"]
    when = f"{ago / 3600:.0f} h ago" if ago >= 3600 else f"{ago / 60:.0f} min ago"
    if x["seeders"]:
        return f"{x['seeders']} seeder{'s' if x['seeders'] > 1 else ''} connected, {x['known']} peers announced, {when}"
    if x["known"]:
        return f"{x['known']} peer{'s' if x['known'] > 1 else ''} announced, {when}"
    return f"no peer found, {when}"


def looks_dead(h):
    x = health.get(h)
    return bool(x) and not x["known"] and not x["connected"]


PAGE_URL = os.environ.get("ATHENAFS_PAGE_URL", "")


def write_root_files():
    """Links to this page at the root of the share: .webloc opens in macOS, .url in Windows."""
    os.makedirs(ROOT_FILES, exist_ok=True)
    for fn in os.listdir(ROOT_FILES):
        os.remove(os.path.join(ROOT_FILES, fn))
    if not PAGE_URL.startswith(("http://", "https://")):
        return
    u = html.escape(PAGE_URL)
    with open(os.path.join(ROOT_FILES, "athenafs web page.webloc"), "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict><key>URL</key>'
                f'<string>{u}</string></dict></plist>\n')
    with open(os.path.join(ROOT_FILES, "athenafs web page.url"), "w", newline="\r\n") as f:
        f.write(f"[InternetShortcut]\nURL={PAGE_URL}\n")


def start():
    unmount_fuse()   # a stale mount from a killed run would hide the new one
    write_root_files()
    try:
        write_health_order()
    except OSError as e:
        print("health order:", e)
    try:
        write_ready_views()   # the views (their names) are created by Distribyted at start-up
        write_hidden_folders()
    except Exception as e:
        print("ready views:", e)
    generate_config(load_sources())
    st = load_settings()
    write_no_download(st)
    env = dict(os.environ,
               DISTRIBYTED_UPLOAD_LIMIT=str(int(st["upload_kbps"]) * 1000),
               DISTRIBYTED_GENTLEMAN_FILE=GENTLEMAN_FILE,
               DISTRIBYTED_GENTLEMAN_SPACE=str(int(gentle_space_bytes(st))),
               DISTRIBYTED_GENTLEMAN_CACHE=GENTLEMAN_CACHE,
               DISTRIBYTED_WANTED_FILE=WANTED_FILE,
               DISTRIBYTED_NO_DOWNLOAD_FILE=NO_DOWNLOAD_FILE,
               # Go's soft memory limit: near the ceiling the garbage collector runs more often and returns memory to the
               # system, so the ceiling (see plan_waiting) acts on garbage before any torrent is put to sleep
               GOMEMLIMIT=f"{int(st['memory_ceiling_mb']) * 1000 // 1024 * 1000 // 1024}MiB")
    state["proc"] = subprocess.Popen(["/bin/distribyted", "--config", GENERATED, "--http-port", "4444"], env=env)
    state["state"] = "running"


def stop():
    p = state.get("proc")
    if p and p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
    unmount_fuse()
    state["proc"] = None


def unmount_fuse():
    """Unmount every FUSE filesystem left at MOUNT (a stale one stays when Distribyted was killed, e.g. by a too short stop
    timeout, and mounts can be stacked). Only FUSE ones: MOUNT's parent is also the host's bind mount (rshared), an
    unconditional umount would propagate to the host and remove it."""
    for _ in range(5):
        if not any(len(c) > 2 and c[1] == MOUNT and c[2].startswith("fuse") for c in (l.split() for l in open("/proc/mounts"))):
            return
        subprocess.run(["umount", "-l", MOUNT], capture_output=True)


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
    fl, pl = t["files"], t["pl"]
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
    return {piece(t, p) for p in ok}, len(cand) - len(ok)


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
    new = dict(load_settings(), mode=mode, cache_mb=cache_mb, reserve_pct=reserve)   # keeps the other settings

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


def add_torrent_bytes(raw, wanted_route="", explicit=False):
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
    sources.append({"id": sid, "route": route_for(wanted_route or t["name"], explicit), "kind": "file", "value": fn})
    save_sources(sources)
    return f"added: {t['name']} ({t['total'] / 1e6:.0f} MB)"


def add_links(text, wanted_route=""):
    """Add magnet links and/or URLs of .torrent files, one per line. Starts in a thread; one restart for the whole batch.
    A folder name puts the whole batch in that one folder (merged); without it, each torrent goes to the folder of its name."""
    links = [l.strip() for l in text.splitlines() if l.strip()]
    known = {source_hash(s) for s in load_sources()}
    magnets, files, skipped = [], [], []
    for link in links:
        if link.startswith("magnet:"):
            h = magnet_hash(link)
            if not h:
                skipped.append(f"not a valid magnet link: {link[:40]}…"); continue
            if h in known:
                skipped.append(f"already present: {h[:8]}"); continue
            known.add(h)
            magnets.append((link, h, urllib.parse.parse_qs(link[8:]).get("dn", [""])[0]))
        else:
            try:   # fetched before stopping the service, so the mounts stay up while downloading
                files.append(fetch_torrent(link))
            except Exception as e:
                skipped.append(f"could not fetch {link[:40]}: {e}")
    if not magnets and not files:
        state["message"] = "; ".join(skipped) or "nothing to add"; return

    def job():
        srcs, added = load_sources(), []
        for link, h, dn in magnets:
            srcs.append({"id": secrets.token_hex(4), "route": route_for(wanted_route or dn or h[:8], bool(wanted_route)),
                         "kind": "magnet", "value": link})
            added.append(dn or h[:8])
        save_sources(srcs)
        msgs = [f"added magnet {a}" for a in added]
        msgs += [add_torrent_bytes(raw, wanted_route, bool(wanted_route)) for raw in files]
        if magnets:
            msgs.append("magnets wait for their metadata (up to the add timeout each)")
        return "; ".join(msgs + skipped)
    exclusive(job)


def remove_folder(route, erase_data):
    """Remove every torrent of a folder, with or without its cached data, with one restart."""
    mine = [x for x in load_sources() if x["route"] == route]
    if not mine:
        state["message"] = "unknown folder: nothing removed"; return

    def job():
        hashes = {source_hash(x) for x in mine} - {None}
        msg = f"removed folder {route} ({len(mine)} torrent(s))"
        if erase_data:
            n, sz = delete_pieces(cached_pieces_of(hashes))
            msg += f"; erased {n} pieces, {sz / 1e6:.0f} MB"
        else:
            msg += "; data kept"
        for x in mine:
            h = source_hash(x)
            if x["kind"] == "file":
                try: os.remove(x["value"])
                except OSError: pass
            if h:
                try: os.remove(os.path.join(METAINFO, h + ".torrent"))
                except OSError: pass
        save_sources([x for x in load_sources() if x["route"] != route])
        st = load_folder_settings()
        if st.pop(route, None) is not None:   # its ready view and visibility go with it
            save_folder_settings(st)
            write_hidden_folders()
        return msg
    exclusive(job)


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

def tree(t, mine):
    """Folders/files of a torrent that have cached data (size ≈ cached pieces covering the file).
    mine = [(piece index, cached bytes)] of this torrent: the work is proportional to what is cached, not to the torrent."""
    per_file = {}
    for i, size in mine:
        for k, _ in files_of_piece(t, i, t["pl"]):
            per_file[k] = per_file.get(k, 0) + size
    root = {"name": "", "path": "", "size": 0, "children": {}}
    for k, sz in sorted(per_file.items()):
        path = t["files"][k][0]
        if t["files"][k][2] <= 0 or is_padding(path):
            continue
        nd = root; nd["size"] += sz; cur = ""
        for comp in path.split("/"):
            cur = f"{cur}/{comp}" if cur else comp
            nd = nd["children"].setdefault(comp, {"name": comp, "path": cur, "size": 0, "children": {}})
            nd["size"] += sz
    return root


_hidden = {"key": None, "value": {}}


def hidden_files(key, tp):
    """source id -> number of its files whose path is also in another torrent of the same folder (one copy is served).
    Recomputed only when the sources change."""
    if key != _hidden["key"]:
        seen = {}
        for sid, route, h in key:
            t = tp[h]
            for p, _, _ in t["files"]:
                seen.setdefault((route, t["name"], p), []).append(sid)
        hidden = {}
        for ids in seen.values():
            if len(ids) > 1:
                for i in ids:
                    hidden[i] = hidden.get(i, 0) + 1
        _hidden.update(key=key, value=hidden)
    return _hidden["value"]


def snapshot():
    tp = torrent_pieces(); files = cache_files()
    by_torrent = {}   # info-hash -> [(piece index, bytes)] of its cached pieces
    for h, owners in piece_owners(files).items():
        for ih, i in owners:
            by_torrent.setdefault(ih, []).append((i, files[h]))
    routes = activity["routes"] if time.time() - activity["at"] < 130 else (api("/routes") or [])
    live = {t["hash"]: (r["name"], t) for r in routes for t in (r.get("torrentStats") or [])}
    items = []
    for ih, t in tp.items():
        mine = by_torrent.get(ih, [])
        sz = sum(n for _, n in mine)
        route, st_ = live.get(ih, ("?", {}))
        if route == "?" and sz == 0:
            continue  # .torrent present but not mounted, nothing cached
        items.append({"hash": ih, "name": t["name"], "route": route, "total": t["total"], "cache": sz,
                      "pieces": f"{len(mine)}/{t['npieces']}", "peers": st_.get("peers", 0),
                      "tree": tree(t, mine) if sz else None})
    srcs, sources = [], load_sources()
    # in a folder shared by several torrents, a path present in more than one is served from only one of them
    hidden = hidden_files(tuple((s["id"], s["route"], source_hash(s)) for s in sources if source_hash(s) in tp), tp)
    for s in sources:
        h = source_hash(s)
        if h in live:
            status = "mounted"
            if hidden.get(s["id"]):
                status += f" ({hidden[s['id']]} files also in another torrent of this folder: one copy is served)"
        elif s["kind"] == "magnet" and h not in tp:
            status = "waiting for metadata"
        else:
            status = "not mounted"
        srcs.append({"id": s["id"], "route": s["route"], "kind": s["kind"], "hash": h, "status": status,
                     "awake": h in live and not live[h][1].get("dormant"),
                     "size": tp[h]["total"] if h in tp else 0,
                     "name": tp[h]["name"] if h in tp else (magnet_dn(s["value"]) if s["kind"] == "magnet" else "")})
    st = api("/status") or {}
    total_d, free_d = disk()
    unknown = sum(files.values()) - sum(i["cache"] for i in items) if pieces_ready["done"] else 0
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


def rate(n):
    if n < 1:
        return "0"
    return f"{n:.0f} B/s" if n < 1e3 else f"{n / 1e3:.0f} kB/s" if n < 1e6 else f"{n / 1e6:.1f} MB/s"


def mb(n):
    if n >= 1e12:
        return f"{n / 1e12:.2f} TB"
    return f"{n / 1e6:,.0f} MB".replace(",", " ") if n < 1e9 else f"{n / 1e9:.2f} GB"


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>athenafs</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1b1d21;--mut:#6b7079;--line:#e3e5ea;--acc:#2f5bd3;--warn:#b3261e;--ok:#1e7d43;--chip:#eef1f6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111315;--card:#1a1d20;--fg:#e6e7e9;--mut:#9aa0a8;--line:#2c3035;--acc:#8fb0ff;--warn:#ff8a80;--ok:#6fd49a;--chip:#252a30}}}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;padding:16px}}
.page{{max-width:1040px;margin:auto}}
header{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:4px 0 14px}}
h1{{font-size:22px;margin:0;letter-spacing:-.01em}}h2{{font-size:15px;margin:0 0 10px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}}
.pill{{padding:2px 10px;border-radius:99px;font-size:13px;background:var(--chip)}}.pill.ok{{color:var(--ok)}}.pill.busy{{color:var(--warn)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:0 0 14px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:0 0 14px}}
.tile{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px}}
.tile b{{display:block;font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}}.tile span{{color:var(--mut);font-size:13px}}
.mut{{color:var(--mut)}}.small{{font-size:13px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}}
tr:last-child>td{{border-bottom:0}}th{{font-size:13px;color:var(--mut);font-weight:500}}
td.n,th.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
button{{font:inherit;font-size:14px;padding:5px 10px;border:1px solid var(--line);color:var(--fg);background:var(--card);border-radius:7px;cursor:pointer}}
button:hover{{border-color:var(--acc)}}button.primary{{background:var(--acc);border-color:var(--acc);color:#fff}}
button.danger{{color:var(--warn)}}button.danger:hover{{border-color:var(--warn)}}button:disabled{{opacity:.4;cursor:default}}
.msg{{padding:10px 14px;border-left:3px solid var(--acc);background:var(--card);border-radius:6px;margin:0 0 14px}}
.wrap{{overflow-x:auto}}details>summary{{cursor:pointer}}
.tree{{margin:4px 0 8px 14px}}.row{{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:2px 0}}
.row form,summary form,td form{{display:inline;margin:0}}.row button,summary button,td button{{padding:2px 8px;font-size:13px}}
.sz{{white-space:nowrap;color:var(--mut)}}
input[type=text],input[type=number],input[type=search],textarea{{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg)}}
.add{{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0}}.add .l{{flex:1 1 380px}}.add .f{{flex:0 1 180px}}
.bar{{height:4px;background:var(--chip);border-radius:2px;margin-top:4px;overflow:hidden}}.bar i{{display:block;height:100%;background:var(--acc)}}
.sub td{{padding:4px 6px 4px 22px;font-size:14px}}
.folders details.folder{{border-bottom:1px solid var(--line)}}.folders details.folder:last-child{{border-bottom:0}}
summary.frow{{display:flex;align-items:center;gap:10px;padding:10px 4px;cursor:pointer;list-style:none;flex-wrap:wrap}}
summary.frow::-webkit-details-marker{{display:none}}.fname{{font-weight:600}}.badges{{display:flex;gap:6px;flex-wrap:wrap;flex:1}}
.badges .pill{{font-size:12px}}.pill.ok{{color:var(--ok)}}.pill.warn{{color:var(--warn)}}.fnum{{white-space:nowrap;font-variant-numeric:tabular-nums}}
.manage{{font-size:13px;border:1px solid var(--line);border-radius:7px;padding:3px 9px}}.manage:after{{content:" \\25BE"}}
details.folder[open] .manage:after{{content:" \\25B4"}}details.folder[open]>summary{{background:var(--chip);border-radius:8px}}
.panel{{display:grid;gap:10px;padding:10px 4px 14px}}.blk{{border:1px solid var(--line);border-radius:8px;padding:10px 12px}}
.blk h3{{margin:0 0 6px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}}
.blk .line{{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:4px 0}}.blk p{{margin:6px 0 0}}
.danger-zone{{border-color:var(--warn)}}.danger-zone h3{{color:var(--warn)}}
.tile.pie{{display:flex;justify-content:space-between;align-items:center;gap:8px}}.tile.pie b{{font-size:20px}}
.donut{{width:64px;height:64px;border-radius:50%;flex:none;position:relative}}
.donut:after{{content:"";position:absolute;inset:14px;border-radius:50%;background:var(--card)}}
.legend{{font-size:12px;color:var(--mut)}}.legend i{{display:inline-block;width:9px;height:9px;border-radius:2px;margin:0 3px 0 6px}}
.histw{{display:flex;gap:6px;margin:4px 0}}.yax{{position:relative;width:64px;height:70px;flex:none;font-size:11px;color:var(--mut)}}
.yax span{{position:absolute;right:0;transform:translateY(-50%);white-space:nowrap}}.hplot{{flex:1;min-width:0}}
.hist{{width:100%;height:70px;display:block}}.xax{{display:flex;justify-content:space-between;font-size:11px;color:var(--mut)}}
a{{color:var(--acc)}}a.seg{{color:inherit;text-decoration:none;border-bottom:1px dotted var(--mut)}}a.seg:hover{{color:var(--acc)}}
.pill.mut{{color:var(--mut)}}.warn{{color:var(--warn)}}
details.sec{{position:relative}}details.sec>summary{{list-style:none;cursor:pointer}}details.sec>summary::-webkit-details-marker{{display:none}}
.secbox{{position:absolute;z-index:5;top:30px;left:0;width:min(560px,90vw);background:var(--card);border:1px solid var(--line);
border-left:3px solid var(--warn);border-radius:8px;padding:10px 14px;box-shadow:0 4px 16px rgba(0,0,0,.12);font-size:14px}}
.secbox ul{{margin:0 0 8px;padding-left:18px}}.secbox li{{margin:4px 0}}.brk{{word-break:break-word}}#sr td.small{{word-break:break-word}}#sr td button{{padding:1px 8px;font-size:12px}}
</style>
<div class=page>
<header><h1>athenafs</h1><span id=pill class="pill {pillcls}">{state}</span><span id=wpill>{waitpill}</span>{secwarn}<span id=cpill>{cachepill}</span></header>
<section class=card><h2>Add torrents</h2>
<form class=add method=post action=/add>
  <textarea class=l name=link rows=2 placeholder="magnet:?xt=urn:btih:…  or  https://…/file.torrent&#10;several: one per line" required></textarea>
  <input class=f type=text name=route placeholder="folder (optional)" title="Folder for every link of the batch. Torrents with the same folder name are merged into one folder.">
  <button class=primary type=submit>Add</button>
</form>
<div class=add><span class="mut small">or a .torrent file:</span> <input type=file id=file accept=".torrent"
  onchange="const f=this.files[0];if(!f)return;fetch('/upload',{{method:'POST',headers:{{'X-Filename':encodeURIComponent(f.name)}},body:f}}).then(()=>location.reload())"></div>
<div class="mut small">Torrents with the same folder name share one folder. Adding or removing restarts the service once per batch; the share is unavailable meanwhile.</div>
</section>
<section class=card><h2>Search files</h2>
<input id=q type=search placeholder="part of a file or folder name; several words: all must match" style="width:100%" autocomplete=off>
<div class=add style="margin-top:6px"><span class="mut small">order</span><select id=qsort><option value="">as in the torrents</option>
<option value=name>name</option><option value=largest>largest first</option><option value=smallest>smallest first</option></select>
<span class="mut small">show</span><select id=qstatus><option value="">all</option><option value=cached>cached</option>
<option value="not cached">not cached</option></select></div>
<div class="mut small" style="margin-top:4px">Click a folder in a result to list everything under it (<code>folder:path</code>); "Cache" reads files into the cache.</div>
<div id=srmsg style="margin-top:8px"></div><div id=sr class="small" style="margin-top:8px"></div>
</section>
<main id=m>
{msg}
<div class=tiles>
<div class="tile pie"><div><span>Cache</span><b>{cache}</b><span>{cap}</span></div>{cachepie}</div>
<div class="tile pie"><div><span>Disk free</span><b>{dfree}</b><span>of {dtotal}</span></div>{diskpie}</div>
<div class=tile><span>Download</span><b>{down}</b><span>upload {up} · received since start {rx}</span></div>
<div class=tile><span>Torrents</span><b>{ntor}</b><span>in {nfold} folders · {nawake} awake</span></div>
{memtile}
</div>
<section class=card><h2>Activity</h2>{act}</section>
<section class=card><h2>Folders</h2><div class=folders>{sources}</div></section>
<section class=card><details data-k=cache-section><summary><b>Cache</b> <span class="mut small">{ncached} torrent(s) with cached
data, {cache} · per torrent and file, with Erase</span></summary><div class=wrap style="margin-top:10px"><table>
<tr><th>Torrent</th><th class=n>Size</th><th class=n>Cached</th><th></th></tr>
{rows}
</table></div>{unknown}</details></section>
</main>
<section class=card><details id=reqs><summary><b>Cache requests</b> <span id=rqsum class="mut small">every "Cache" asked for,
kept across restarts, with Cancel and Retry</span></summary><div style="margin-top:10px">
<div class=add><span class="mut small">show</span>
<label class=small><input type=radio name=rqs value=all checked> all</label>
<label class=small><input type=radio name=rqs value=open> queued and in progress</label>
<label class=small><input type=radio name=rqs value=waiting> waiting for a seeder</label>
<label class=small><input type=radio name=rqs value=failed> failed</label>
<label class=small><input type=radio name=rqs value=done> done</label>
<label class=small><input type=radio name=rqs value=cancelled> cancelled</label>
<button id=rqretryall>Retry all failed</button>
<button id=rqclear class=danger>Clear finished</button></div>
<input id=rqq type=search placeholder="filter: part of the path; several words: all must match" style="width:100%;margin:6px 0" autocomplete=off>
<div class="mut small">A request that gets no data waits for a seeder, as long as needed: its torrent is kept awake with the pieces it needs
(or woken now and then when the memory budget is used, see Settings). Cancel it to give up. "Failed" is only for files that cannot be read at all.</div>
<div id=rqres class="small wrap"></div>
</div></details></section>
<section class=card><details id=usage><summary><b>Largest in the cache</b> <span class="mut small">by file, folder or torrent, with Erase</span></summary><div style="margin-top:10px">
<div class=add><span class="mut small">by</span>
<label class=small><input type=radio name=uby value=file checked> file</label>
<label class=small><input type=radio name=uby value=folder> folder</label>
<label class=small><input type=radio name=uby value=torrent> torrent</label>
<span id=utot class="mut small"></span></div>
<div id=ures class="small wrap"></div>
</div></details></section>
<section class=card><details><summary><b>Settings</b> <span class="mut small">cache cap, erase everything</span></summary>
<div style="margin-top:10px">{capform}
<div class="mut small">Largest cache cap possible now: {dmax} (free space + what is already cached − 1 GB).</div>
<form method=post action=/erase style="margin-top:12px"><input type=hidden name=h value=all>
<button class=danger {dis} data-confirm="Erase ALL local data? The service restarts (a few seconds) and reads in progress fail.">Erase everything</button></form>
<p class="mut small">Erasing or removing restarts the service: the share is unavailable for a few seconds and reads in progress fail.
It does not delete the .torrent files of the setup or the configuration.</p></div></details></section>
<p class="mut small">Refreshes every 5 s · <a href=/api/docs>API documentation</a> · <a href=/api>raw status</a></p>
</div>
<script>
const esc=s=>s.replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const sz=n=>n<1e6?(n/1e3).toFixed(0)+' kB':n<1e9?(n/1e6).toFixed(0)+' MB':n<1e12?(n/1e9).toFixed(2)+' GB':(n/1e12).toFixed(2)+' TB';
const post=async(u,d)=>(await fetch(u,{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:new URLSearchParams(d)}})).json();
const badge={{'cached':'ok','partial':'','not cached':'mut','queued':'busy','caching':'busy','downloading':'busy','failed':'warn','done':'ok','cancelled':'mut','waiting':'busy'}};
const dur=s=>s<90?Math.round(s)+' s':s<5400?Math.round(s/60)+' min':s<129600?(s/3600).toFixed(1).replace('.0','')+' h':(s/86400).toFixed(1).replace('.0','')+' days';
const ago=t=>{{const s=Date.now()/1000-t;return s<90?Math.round(s)+' s ago':s<5400?Math.round(s/60)+' min ago':s<129600?Math.round(s/3600)+' h ago':Math.round(s/86400)+' d ago'}};
let sq='',so=0,st;
function pathHtml(p){{  // each folder of the path lists that folder when clicked
  const parts=p.split('/'); let acc='';
  return parts.map((x,i)=>{{acc+=(i?'/':'')+x; return i<parts.length-1?'<a href=# class=seg data-f="'+esc(acc)+'">'+esc(x)+'</a>':'<b>'+esc(x)+'</b>'}}).join('/');
}}
function row(f){{
  const pct=f.size?Math.min(100,Math.round(f.cached*100/f.size)):0;
  const label=f.status=='partial'?'partial '+pct+'%':f.status;
  const hl='<div class="small '+(f.dead?'warn':'mut')+'">'+esc(f.health_text||'')+'</div>';
  const btn=['cached','queued','caching','waiting'].includes(f.status)?'':'<button class=cache data-p="'+esc(f.path)+'">'+(f.status=='failed'?'Retry':'Cache')+'</button>';
  const rq=!f.request?'':f.status=='failed'?'<div class="small warn">cache request failed '+ago(f.request.updated)+': '+esc(f.request.error)+'</div>'
    :f.status=='waiting'?'<div class="small mut">waiting for a seeder for '+dur(Date.now()/1000-f.request.waiting_since)+' (see Cache requests)</div>':'';
  const lk=f.smb_url?' <span class=mut>· <a href="'+esc(f.smb_url)+'" title="'+esc(f.smb_url)+'">open</a> · <a href=# data-copy="'+esc(f.unc_path)+'" title="'+esc(f.unc_path)+'">copy \\\\\\\\ path</a></span>':' <span class=mut>· hidden from the share</span>';
  return '<tr><td class=small>'+pathHtml(f.path)+lk+hl+rq+'</td><td class=n>'+sz(f.size)+'</td><td class=n><span class="pill '+(badge[f.status]||'')+'">'+esc(label)+'</span></td><td class=n>'+btn+'</td></tr>';
}}
async function runSearch(more,refresh){{  // refresh: the rows already shown again, to follow their status
  const q=document.getElementById('q').value.trim(), out=document.getElementById('sr');
  if(!more&&!refresh){{so=0}} sq=q; if(!q){{out.innerHTML='';return}}
  const n=refresh?Math.min(500,so+100):100, o=refresh?0:so;
  const r=await (await fetch('/api/search?q='+encodeURIComponent(q)+'&limit='+n+'&offset='+o+'&'+new URLSearchParams(sopts()))).json();
  if(q!==sq||(refresh&&document.querySelector('#sr [data-armed]')))return;
  if(refresh){{const t=document.getElementById('srt');if(t)t.innerHTML=r.results.map(row).join('');return}}
  if(r.error){{out.innerHTML='<span class=mut>'+esc(r.error)+'</span>';return}}
  const h=r.results.map(row).join('');
  const shown=so+r.results.length;
  const head='<div id=srh class=add><span class=mut>'+(r.total_capped?'more than '+r.total:r.total)+' file'+(r.total==1?'':'s')+', '+sz(r.total_size)
    +(shown<r.total?' · showing '+shown:'')+'</span>'+(r.total?' <button id=srall data-n="'+r.total+'" data-s="'+r.total_size+'">Cache all '+(r.total_capped?'(first '+r.total+')':'')+'</button>':'')+'</div>';
  if(more){{document.getElementById('srt').insertAdjacentHTML('beforeend',h);document.getElementById('srh').outerHTML=head}}
  else out.innerHTML=head+'<div class=wrap><table id=srt>'+h+'</table></div>';
  document.getElementById('srm')?.remove();
  if(shown<r.total)out.insertAdjacentHTML('beforeend','<button id=srm style="margin-top:8px">More</button>');
}}
const sopts=()=>({{sort:document.getElementById('qsort').value,status:document.getElementById('qstatus').value}});
async function cache(d){{
  // shown as queued at once; the list then follows the real status (see the refresh below)
  document.querySelectorAll('#srt tr').forEach(tr=>{{const b=tr.querySelector('button.cache'),p=tr.querySelector('.pill');
    if(b&&p&&(d.q||b.dataset.p===d.path)){{p.textContent='queued';p.className='pill busy';b.remove()}}}});
  const r=await post('/api/prefetch',d);
  document.getElementById('srmsg').innerHTML='<div class=msg>'+esc(r.error?r.error:(r.queued+' file(s) queued for the cache, '+sz(r.size)+(r.already?' ('+r.already+' already queued)':'')+': progress under Activity'
    +(r.no_peer?'. Warning: '+r.no_peer+' of them belong to torrents where the last health check found no peer at all':'')))+'</div>';
  runSearch(false);
}}
async function loadUsage(){{
  const by=document.querySelector('input[name=uby]:checked').value;
  const r=await (await fetch('/api/usage?limit=20&by='+by)).json();
  document.getElementById('utot').textContent=r.total+' '+by+(r.total==1?'':'s')+' with cached data, '+sz(r.total_cached);
  document.getElementById('ures').innerHTML='<table>'+r.results.map(x=>{{
    const name=by=='torrent'?esc(x.name)+' <span class=mut>'+esc(x.folder)+' · '+x.hash.slice(0,8)+'</span>':pathHtml(x.path);
    const what=by=='torrent'?('hash='+x.hash):('path='+encodeURIComponent(x.path));
    const L=x.smb_url?x:(x.location||{{}});
    const loc=L.smb_url?' <span class=mut>· <a href="'+esc(L.smb_url)+'" title="'+esc(L.smb_url)+'">open</a> · <a href=# data-copy="'+esc(L.unc_path)+'" title="'+esc(L.unc_path)+'">copy \\\\\\\\ path</a></span>':'';
    return '<tr><td class=brk>'+name+loc+'</td><td class=n>'+sz(x.cached)
      +(x.size?' <span class=mut>of '+sz(x.size)+'</span>':'')+'</td><td class=n><button class=danger data-uerase="'+esc(what)+'">Erase</button></td></tr>'}}).join('')+'</table>';
}}
async function loadReqs(){{
  const w=document.querySelector('input[name=rqs]:checked').value;
  const fq=document.getElementById('rqq').value.trim();
  const r=await (await fetch('/api/requests?limit=100&state='+w+'&q='+encodeURIComponent(fq))).json(), c=r.counts;
  if(document.querySelector('#reqs [data-armed]'))return;
  document.getElementById('rqsum').textContent=(c.queued+c.caching)+' open · '+c.waiting+' waiting · '+c.failed+' failed · '+c.done+' done · '+c.cancelled+' cancelled';
  document.getElementById('rqretryall').disabled=!c.failed;
  document.getElementById('rqclear').disabled=!(c.done+c.cancelled+c.failed);
  document.getElementById('rqres').innerHTML=r.results.length?'<table>'+r.results.map(x=>{{
    const pct=x.size?Math.min(100,Math.round(x.read*100/x.size)):0;
    const now=Date.now()/1000, w=x.wait;
    let info=x.state=='caching'?sz(x.read)+' / '+sz(x.size)+'<div class=bar><i style="width:'+pct+'%"></i></div>':x.state+' '+ago(x.updated);
    if(x.state=='waiting'){{
      info='for '+dur(now-x.waiting_since);
      const rcv=w&&w.last_received&&now-w.last_received<180;
      if(w)info+='<br>'+(rcv?'<b>receiving data</b>, '+sz(w.down_rate)+'/s ('+w.peers+' peers, '+w.seeders+' seeder'+(w.seeders==1?'':'s')+')'
        :w.mode=='listening'?'kept awake: '+w.peers+' peer'+(w.peers==1?'':'s')+', '+w.seeders+' seeder'+(w.seeders==1?'':'s')
        :w.mode=='probing'?'probing now ('+w.peers+' peers)':'asleep (memory budget used), next probe in '+dur(Math.max(0,w.next_probe-now)))
        +' · '+(w.pieces_needed-w.missing_pieces)+' of '+w.pieces_needed+' piece'+(w.pieces_needed==1?'':'s')+' here';
    }}
    if(x.state=='done'&&x.arrived&&x.waiting_since)info+='<br>arrived after waiting '+dur(x.arrived-x.waiting_since);
    const err=x.error&&!['done','waiting'].includes(x.state)?'<div class="small '+(x.state=='failed'?'warn':'mut')+'">'+esc(x.error)+'</div>':'';
    const hl='<div class="small '+(x.dead?'warn':'mut')+'">'+esc(x.health_text||'')+'</div>';
    const b=(['failed','cancelled'].includes(x.state)?'<button data-rq=retry data-p="'+esc(x.path)+'">Retry</button> ':'')
      +(['queued','caching','waiting','failed'].includes(x.state)?'<button class=danger data-rq=cancel data-p="'+esc(x.path)+'">Cancel</button>':'');
    return '<tr><td class=brk>'+pathHtml(x.path)+err+hl+'</td><td class=n>'+sz(x.size)+'</td><td class=n><span class="pill '+(badge[x.state]||'')+'">'+esc(x.state)+'</span><div class="small mut">'+info+'</div></td><td class=n>'+b+'</td></tr>'}}).join('')+'</table>'
    +(r.total>r.results.length?'<div class="mut small">showing '+r.results.length+' of '+r.total+'</div>':''):'<span class=mut>'+(fq?'No request matches.':'None.')+'</span>';
}}
document.querySelectorAll('input[name=rqs]').forEach(i=>i.addEventListener('change',loadReqs));
let rqt;document.getElementById('rqq').addEventListener('input',()=>{{clearTimeout(rqt);rqt=setTimeout(loadReqs,250)}});
document.getElementById('reqs').addEventListener('toggle',e=>{{if(e.target.open)loadReqs()}});
setInterval(()=>{{if(document.getElementById('reqs').open)loadReqs()}},5000);
document.querySelectorAll('input[name=uby]').forEach(i=>i.addEventListener('change',loadUsage));
document.getElementById('usage').addEventListener('toggle',e=>{{if(e.target.open)loadUsage()}});  // folded: loaded when opened
setInterval(()=>{{if(document.getElementById('usage').open&&!document.querySelector('#ures [data-armed]'))loadUsage()}},30000);
document.getElementById('q').addEventListener('input',()=>{{clearTimeout(st);st=setTimeout(()=>runSearch(false),250)}});
['qsort','qstatus'].forEach(id=>document.getElementById(id).addEventListener('change',()=>runSearch(false)));
setInterval(()=>{{if([...document.querySelectorAll('#srt .pill.busy')].some(p=>p.textContent!='waiting'))runSearch(false,true)}},5000);
document.addEventListener('click',e=>{{
  const t=e.target;
  if(t.dataset.copy!==undefined){{e.preventDefault();navigator.clipboard?.writeText(t.dataset.copy).then(()=>{{t.textContent='copied'}},()=>{{t.textContent=t.dataset.copy;t.style.userSelect='all'}})}}
  else if(t.classList.contains('seg')||t.classList.contains('browse')){{e.preventDefault();const q=document.getElementById('q');q.value='folder:'+t.dataset.f;q.scrollIntoView({{block:'center'}});runSearch(false)}}
  else if(t.classList.contains('cache'))cache({{path:t.dataset.p}});
  else if(t.dataset.rq){{t.disabled=true;post('/api/requests/'+t.dataset.rq,{{path:t.dataset.p}}).then(r=>{{if(r.error)t.textContent=r.error;loadReqs()}})}}
  else if(t.id=='rqclear'){{  // two steps in the page (a confirm() pop-up can be blocked silently)
    if(!t.dataset.armed){{t.dataset.armed=1;t.textContent='Confirm: forget done, cancelled and failed requests';
      setTimeout(()=>{{if(t.isConnected&&t.dataset.armed){{delete t.dataset.armed;t.textContent='Clear finished'}}}},6000)}}
    else{{delete t.dataset.armed;t.disabled=true;post('/api/requests/clear',{{}}).then(r=>{{t.textContent=r.error?r.error:'Clear finished';loadReqs()}})}}
  }}
  else if(t.id=='rqretryall'){{t.disabled=true;post('/api/requests/retry',{{all:'true'}}).then(r=>{{t.textContent=r.error?r.error:'Retry all failed';loadReqs()}})}}
  else if(t.classList.contains('reqlink')){{e.preventDefault();const d=document.getElementById('reqs');d.open=true;document.querySelector('input[name=rqs][value='+(t.dataset.f||'all')+']').checked=true;loadReqs();d.scrollIntoView({{block:'start'}})}}
  else if(t.classList.contains('askopen')){{  // Rename / Move: a name field in place
    const f=t.closest('form'); f.dataset.armed=1; t.remove();
    const fld=f.dataset.field||'new';
    f.insertAdjacentHTML('beforeend','<input type=text name="'+fld+'" required style="width:200px" value="'+esc(f.dataset.current)+'"> <button>OK</button> <button type=button class=askcancel>Cancel</button><div class="mut small">'+esc(f.title)+'</div>');
    const i=f.querySelector('input[name="'+fld+'"]'); i.focus(); i.select();
  }}
  else if(t.classList.contains('askcancel')){{const f=t.closest('form');delete f.dataset.armed;location.reload()}}
  else if(t.dataset.uerase!==undefined){{
    if(!t.dataset.armed){{t.dataset.armed=1;t.textContent='Confirm erase (restarts the service)';setTimeout(()=>{{if(t.isConnected){{delete t.dataset.armed;t.textContent='Erase'}}}},6000)}}
    else{{t.disabled=true;t.textContent='Erasing…';fetch('/api/erase',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:t.dataset.uerase}})
      .then(r=>r.json()).then(r=>{{t.textContent=r.error?r.error:'Erasing: see the message above';setTimeout(loadUsage,15000)}})}}
  }}
  else if(t.dataset.confirm!==undefined){{  // in-page confirmation (a confirm() pop-up can be blocked silently)
    if(!t.dataset.armed){{e.preventDefault();t.dataset.armed=1;t.dataset.label=t.textContent;t.textContent='Confirm: '+t.dataset.confirm;
      setTimeout(()=>{{if(t.isConnected){{delete t.dataset.armed;t.textContent=t.dataset.label}}}},6000)}}
  }}
  else if(t.id=='srall'){{  // two steps in the page: a confirm() pop-up can be blocked silently (embedded browsers)
    if(t.dataset.armed){{t.disabled=true;t.textContent='Queuing…';cache({{q:document.getElementById('q').value.trim(),...sopts()}})}}
    else{{t.dataset.armed=1;t.classList.add('danger');t.textContent='Confirm: read '+t.dataset.n+' file(s), '+sz(+t.dataset.s)+' into the cache';
      setTimeout(()=>{{if(t.isConnected&&!t.disabled){{delete t.dataset.armed;t.classList.remove('danger');t.textContent='Cache all'}}}},6000)}}}}
  else if(t.id=='srm'){{so+=100;runSearch(true)}}
  else if(t.id=='pfcancel')post('/api/prefetch/cancel',{{}});
}});
setInterval(async()=>{{try{{
  if(document.querySelector('#m [data-armed]'))return;  // a button waits for its confirmation
  const o=new Set([...document.querySelectorAll('#m details[open]')].map(d=>d.dataset.k));
  const t=await (await fetch('/',{{cache:'no-store'}})).text();
  const d=new DOMParser().parseFromString(t,'text/html'), n=d.getElementById('m'); if(!n)return;
  n.querySelectorAll('details').forEach(x=>{{if(o.has(x.dataset.k))x.open=true}});
  document.getElementById('m').replaceWith(n);
  const p=d.getElementById('pill'); if(p)document.getElementById('pill').replaceWith(p);
  const wp=d.getElementById('wpill'); if(wp)document.getElementById('wpill').replaceWith(wp);
  const cp=d.getElementById('cpill'); if(cp&&!document.querySelector('#cpill [data-armed]'))document.getElementById('cpill').replaceWith(cp);
}}catch(e){{}}}},5000)
</script>"""


def node(ih, n, dis):
    """HTML of one folder or file of the tree, with its Erase button."""
    button = (f"<form method=post action=/erase><input type=hidden name=h value={ih}><input type=hidden name=p value=\"{html.escape(n['path'], quote=True)}\">"
              f"<button class=danger {dis} data-confirm=\"Erase the local data of {html.escape(n['name'], quote=True)}?\">Erase</button></form>")
    if not n["children"]:
        return f"<div class=row><span>{html.escape(n['name'])}</span><span class=sz>≈ {mb(n['size'])} {button}</span></div>"
    kids = sorted(n["children"].values(), key=lambda x: -x["size"])
    body = "".join(node(ih, e, dis) for e in kids[:300])
    more = f"<div class=mut>… {len(kids) - 300} more</div>" if len(kids) > 300 else ""
    return (f"<details data-k=\"{ih}/{html.escape(n['path'], quote=True)}\"><summary>{html.escape(n['name'])}/ "
            f"<span class=sz>≈ {mb(n['size'])} {button}</span></summary><div class=tree>{body}{more}</div></details>")


def donut(parts, title):
    """Small pie (donut) from [(value, css colour)], drawn with a conic gradient; title = tooltip."""
    total = sum(v for v, _ in parts) or 1
    stops, acc = [], 0.0
    for v, c in parts:
        a = acc; acc += v / total * 100
        stops.append(f"{c} {a:.2f}% {acc:.2f}%")
    return f"<div class=donut title=\"{html.escape(title, quote=True)}\" style=\"background:conic-gradient({','.join(stops)})\"></div>"


def nice_scale(peak):
    """Top of the histogram's scale: the smallest round value at or above the peak of the last 10 minutes. Recomputed at
    every refresh, so the scale follows the data up and down; the fine steps keep the empty space above the peak small."""
    if peak <= 0:
        return 1000
    k = 10 ** math.floor(math.log10(peak))
    return next(m * k for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10) if m * k >= peak)


def gentleman_view():
    """Torrents helped by gentleman mode (from Distribyted's stats) with what the trackers say about their swarm."""
    sw = swarm_info(); st = load_settings(); gs = load_gentle_state()
    names = {}
    for r in activity.get("routes") or []:
        for t in r.get("torrentStats") or []:
            names[t["hash"]] = (t, r["name"])
    rows = []
    for h, x in gs.get("slots", {}).items():
        t, route = names.get(h, ({}, "?"))
        g = t.get("gentleman") or {}
        rows.append({"hash": h, "folder": route, "since": x["since"], "swarm": sw.get(h), "pieces_wanted": g.get("pieces_wanted", 0),
                     "pieces_held": g.get("pieces_held", 0), "pieces_pending": g.get("pieces_pending", 0), "budget": g.get("budget", 0),
                     "piece_size": g.get("piece_size", 0), "peers": g.get("peers", 0),
                     "uploaded": g.get("uploaded", 0), "active": bool(g)})
    known = [x for x in sw.values()]
    tp = torrent_pieces()
    for r_ in rows:
        r_["memory_estimate"] = tp[r_["hash"]]["npieces"] * BYTES_PER_AWAKE_PIECE if r_["hash"] in tp else 0
    return {"on": st["gentle_on"], "slots": int(st["gentle_slots"]), "space": gentle_space_bytes(st),
            "memory_budget": gentle_memory(st),
            "upload_limit": int(st["upload_kbps"]) * 1000, "helped": rows,
            "swarm": {"scraped": len(known), "dead": sum(1 for x in known if x["seeders"] == 0),
                      "in_difficulty": sum(1 for x in known if 1 <= x["seeders"] <= 2 and x["leechers"] >= 1),
                      "last_scrape": max((x["checked"] for x in known), default=0)}}


def gentleman_summary_html():
    v = gentleman_view()
    if not v["on"]:
        return "<div class=\"mut small\" style=\"margin-top:8px\">Gentleman mode: off (Settings).</div>"
    sw = v["swarm"]
    head = (f"<div class=small style=\"margin-top:10px\"><b>Gentleman mode</b> <span class=mut>· {len(v['helped'])}/{v['slots']} slot(s) "
            f"used · {mb(v['space'])} reserved · upload cap {rate(v['upload_limit']) if v['upload_limit'] else 'none'} · trackers: "
            f"{sw['in_difficulty']} torrent(s) in difficulty, {sw['dead']} with no seeder, of {sw['scraped']} answered</span></div>")
    rows = ""
    for x in v["helped"]:
        s_ = x["swarm"] or {}
        held = x["pieces_held"] * x["piece_size"]
        pend = f" · {x['pieces_pending']} on the way" if x["pieces_pending"] else ""
        rows += (f"<tr><td class=small>{html.escape(x['folder'])} <span class=mut>{x['hash'][:8]}</span></td>"
                 f"<td class=\"small mut\">{s_.get('seeders', '?')} seeder(s), {s_.get('leechers', '?')} leecher(s)</td>"
                 + (f"<td class=\"n small\">{x['pieces_held']} rare pieces held ({mb(held)} of {mb(x['budget'])}){pend}</td>"
                    if x["active"] else "<td class=\"n small mut\">waking up</td>")
                 + f"<td class=\"n small\">{mb(x['uploaded'])} sent</td></tr>")
    return head + (f"<div class=wrap><table>{rows}</table></div>" if rows else "")


def histogram_svg():
    """Download and upload rates over the last 10 minutes, one bar per 2 s sample, with a rate scale and a time axis.
    The labels are HTML around the SVG, so that stretching the bars does not stretch the text."""
    h = list(rate_history)
    if not h:
        return ""
    peak = max(max(d, u) for _, d, u in h)
    top = nice_scale(peak)
    n, w, ht = 300, 600, 60
    bw = w / n
    off = n - len(h)
    y = lambda v: ht - min(v, top) / top * ht
    grid = "".join(f"<line x1=0 y1=\"{y(top * f):.1f}\" x2={w} y2=\"{y(top * f):.1f}\" stroke=\"var(--line)\" stroke-dasharray=\"3 3\"/>"
                   for f in (0.5, 1))
    bars = "".join(f"<rect x=\"{(off + i) * bw:.1f}\" y=\"{y(d):.1f}\" width=\"{bw:.2f}\" height=\"{ht - y(d):.1f}\" fill=\"var(--acc)\"/>"
                   for i, (_, d, u) in enumerate(h) if d > 0)
    bars += "".join(f"<rect x=\"{(off + i) * bw:.1f}\" y=\"{y(u):.1f}\" width=\"{bw:.2f}\" height=\"{ht - y(u):.1f}\" fill=\"var(--warn)\" opacity=\".6\"/>"
                    for i, (_, d, u) in enumerate(h) if u > 0)
    ylab = "".join(f"<span style=\"top:{(1 - f) * 100:.0f}%\">{rate(top * f) if f else '0'}</span>" for f in (1, 0.5, 0))
    return (f"<div class=histw><div class=yax>{ylab}</div><div class=hplot>"
            f"<svg class=hist viewBox=\"0 0 {w} {ht}\" preserveAspectRatio=\"none\" role=img aria-label=\"rates over the last 10 minutes\">"
            f"{grid}<line x1=0 y1={ht} x2={w} y2={ht} stroke=\"var(--mut)\"/>{bars}</svg>"
            f"<div class=xax><span>-10 min</span><span>-5 min</span><span>now</span></div></div></div>"
            f"<div class=legend>peak {rate(peak) if peak else '0'} · now {rate(h[-1][1])} down, {rate(h[-1][2])} up · "
            f"<i style=\"background:var(--acc)\"></i>download<i style=\"background:var(--warn)\"></i>upload</div>")


def folder_block(route, xs, dis, st, lists, inner, more):
    """One folder: a summary line (name, badges only when there is something to say, torrents, size) that opens a
    Manage panel in three blocks: in the share, the folder itself, and a danger zone."""
    r = html.escape(route, quote=True)
    field = f"<input type=hidden name=folder value=\"{r}\">"
    v = st.get(route, {})
    n = len(xs)
    counts = {}
    for x in xs:
        counts[x["status"]] = counts.get(x["status"], 0) + 1
    awake = sum(1 for x in xs if x.get("awake"))
    dead = sum(1 for x in xs if looks_dead(x["hash"]))
    checked = sum(1 for x in xs if x["hash"] in health)
    files = lists.get(route, [])
    badges = []
    if v.get("hidden"):
        badges.append(("warn", "hidden from the share"))
    if v.get("view"):
        badges.append(("ok", f"ready view · {len(files)} file(s)"))
    for st_, c in counts.items():
        if not st_.startswith("mounted"):
            badges.append(("busy", f"{c} {st_}"))
    if awake:
        badges.append(("", f"{awake} awake"))
    if dead:
        badges.append(("warn", f"{dead} with no peer found"))
    bh = "".join(f"<span class=\"pill {c}\">{html.escape(t)}</span>" for c, t in badges)
    summary = (f"<summary class=frow><span class=fname>{html.escape(route)}</span><span class=badges>{bh}</span>"
               f"<span class=\"fnum mut\">{n} torrent{'s' if n != 1 else ''}</span><span class=fnum>{mb(sum(x['size'] for x in xs))}</span>"
               f"<span class=manage>Manage</span></summary>")

    # In the share
    if v.get("hidden"):
        vis = (f"<div class=line><span>Normal view: <b>hidden</b></span> <form method=post action=/folder-visibility>{field}"
               f"<input type=hidden name=hidden value=0><button {dis}>Show in the share</button></form></div>")
    else:
        vis = (f"<div class=line><span>Normal view: shown</span> <form method=post action=/folder-visibility>{field}"
               f"<input type=hidden name=hidden value=1><button {dis}>Hide from the share</button></form></div>")
    if not v.get("view"):
        ready = (f"<div class=line><span>Ready view: off</span> <form method=post action=/ready-view>{field}<input type=hidden name=on value=1>"
                 f"<button {dis}>Turn on</button></form></div>")
    else:
        name = v["view"]; loc = locations(name, True)
        arch = sum(1 for f in files if f.lower().endswith(ARCHIVE_EXT))
        ready = (f"<div class=line><span>Ready view: <b>{html.escape(name)}</b> <span class=mut>· {len(files)} complete file(s), "
                 f"{arch} archive(s) · <a href=\"{html.escape(loc['smb_url'] or '')}\">open</a> · "
                 f"<a href=# data-copy=\"{html.escape(loc['unc_path'] or '', quote=True)}\">copy \\\\ path</a></span></span> "
                 f"<form method=post action=/ready-view class=ask data-field=name data-current=\"{html.escape(name, quote=True)}\" "
                 f"title=\"New name for the ready view (not the name of a folder or of another view):\">{field}"
                 f"<input type=hidden name=on value=1><button type=button class=askopen {dis}>Rename view</button></form> "
                 f"<form method=post action=/ready-view>{field}<input type=hidden name=on value=0>"
                 f"<button {dis} data-confirm=\"Turn off the ready view {html.escape(name, quote=True)}? (restarts the service; the cache is kept)\">Turn off</button></form></div>")
    share = (f"<div class=blk><h3>In the share</h3>{vis}{ready}"
             f"<p class=\"mut small\">The <b>ready view</b> is a sibling folder showing the files of this folder that are completely in "
             f"the cache, archives (zip, 7z, rar) opened as folders: browsing it never downloads anything (turning it on or off "
             f"restarts the service). <b>Hiding</b> the folder removes it from the share within seconds, without a restart; it "
             f"stays in the catalogue, so search and Cache keep working. Both together: the share shows only what is ready.</p></div>")

    # The folder itself
    folder = (f"<div class=blk><h3>Folder</h3><div class=line>"
              f"{ask_form('route', route, dis, 'Rename', 'New name for this folder (an existing folder name merges the two; later torrents of the old name follow):', route)} "
              f"<a href=# class=browse data-f=\"{r}\">Browse in the search</a> "
              f"<span class=\"mut small\">health: {checked}/{n} checked</span></div>"
              f"<details data-k=\"src/{r}\"><summary class=\"mut small\">{n} torrent{'s' if n != 1 else ''}</summary>"
              f"<div class=wrap><table>{inner}</table></div>{more}</details></div>")

    # Danger zone
    danger = (f"<div class=\"blk danger-zone\"><h3>Danger zone</h3><div class=line>"
              f"<form method=post action=/folder-erase>{field}<button class=danger {dis} data-confirm=\"Erase the cached data of all "
              f"{n} torrent(s) of {r}? They stay in the share. (restarts the service)\">Erase its cache</button></form> "
              f"<form method=post action=/folder-remove>{field}<input type=hidden name=erase value=1><button class=danger {dis} "
              f"data-confirm=\"Remove the folder {r}: its {n} torrent(s) and their cached data? (restarts the service)\">Remove folder</button></form> "
              f"<form method=post action=/folder-remove>{field}<input type=hidden name=erase value=0><button {dis} "
              f"data-confirm=\"Remove the folder {r} ({n} torrent(s)) and keep their cached data? (restarts the service)\">Remove, keep data</button></form>"
              f"</div><p class=\"mut small\">Each one restarts the service once for the whole folder.</p></div>")
    return f"<details class=folder data-k=\"folder/{r}\">{summary}<div class=panel>{share}{folder}{danger}</div></details>"


def ask_form(field, value, dis, label, question, current):
    """A button that opens a name field in place (no prompt() pop-up: blocked silently in embedded browsers), then
    posts the name to /rename."""
    return (f"<form method=post action=/rename class=ask data-current=\"{html.escape(current, quote=True)}\" "
            f"title=\"{html.escape(question, quote=True)}\"><input type=hidden name={field} value=\"{html.escape(value, quote=True)}\">"
            f"<button type=button class=askopen {dis}>{label}</button></form>")


def render(s=None):
    s = s or snapshot(); dis = "" if s["state"] == "running" else "disabled"
    rows = ""
    for i in s["items"]:
        if not i["cache"]:
            continue   # mounted, nothing cached: the Folders section lists it
        rows += (f"<tr><td><b>{html.escape(i['name'])}</b><br><span class=\"mut small\">{html.escape(i['route'])} · {i['pieces']} pieces · {i['peers']} peers</span></td>"
                 f"<td class=n>{mb(i['total'])}</td><td class=n>{mb(i['cache'])}</td>"
                 f"<td class=n><form method=post action=/erase><input type=hidden name=h value={i['hash']}>"
                 f"<button class=danger {dis} data-confirm=\"Erase the local data of this whole torrent?\">Erase</button></form></td></tr>")
        if i["tree"]:
            kids = sorted(i["tree"]["children"].values(), key=lambda x: -x["size"])
            body = "".join(node(i["hash"], e, dis) for e in kids[:300])
            more = f"<div class=mut>… {len(kids) - 300} more at the top level</div>" if len(kids) > 300 else ""
            rows += (f"<tr><td colspan=4><details data-k=\"{i['hash']}\"><summary class=small>Files with cached data</summary>"
                     f"<div class=tree>{body}{more}</div></details></td></tr>")
    if not rows:
        rows = "<tr><td colspan=4 class=mut>Nothing cached.</td></tr>"
    av_settings = load_folder_settings(); av_lists = _written.get("lists") or {}
    folders = {}
    for x in s["sources"]:
        folders.setdefault(x["route"], []).append(x)
    srows = ""
    for route, xs in folders.items():
        counts = {}
        for x in xs:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        status = ", ".join(f"{n} {st}" if len(xs) > 1 else st for st, n in counts.items())
        nawk = sum(1 for x in xs if x.get("awake"))
        dead = sum(1 for x in xs if looks_dead(x["hash"]))
        checked = sum(1 for x in xs if x["hash"] in health)
        status += f" · {nawk} awake" if len(xs) > 1 else (" · awake" if nawk else " · dormant")
        status += (f" · health: {checked}/{len(xs)} checked, {dead} with no peer found" if len(xs) > 1
                   else f" · {health_text(xs[0]['hash'])}")
        inner = ""
        for x in xs[:200]:
            inner += (f"<tr class=sub><td>{html.escape(x['name'] or (x['hash'] or '')[:12])} <span class=\"mut small\">{x['kind']} · {(x['hash'] or '')[:8]} · {health_text(x['hash'])}</span></td>"
                      f"<td class=\"n small\">{mb(x['size']) if x['size'] else ''}</td><td class=small>{html.escape(x['status'])}</td>"
                      f"<td class=n>{ask_form('id', x['id'], dis, 'Move', 'Move this torrent to the folder (an existing folder name merges it there):', route)} "
                      f"<form method=post action=/remove><input type=hidden name=id value={x['id']}>"
                      f"<button class=danger {dis} name=erase value=1 data-confirm=\"Remove this torrent and erase its local data?\">Remove</button> "
                      f"<button {dis} name=erase value=0 data-confirm=\"Remove this torrent and keep its local data?\">Remove, keep data</button></form></td></tr>")
        more = (f"<div class=\"mut small\">… and {len(xs) - 200} more (the full list is in <a href=/api/torrents>/api/torrents</a>)</div>"
                if len(xs) > 200 else "")
        srows += folder_block(route, xs, dis, av_settings, av_lists, inner + "", more)
    if not srows:
        srows = "<div class=mut>No torrent yet: add one above.</div>"
    msg = f"<div class=msg>{html.escape(s['message'])}</div>" if s["message"] else ""
    secwarn = ""
    if WARNINGS:   # a badge in the header that unfolds, not a banner on every page
        items = "".join(f"<li>{html.escape(w)}</li>" for w in WARNINGS)
        secwarn = (f"<details class=sec><summary class=\"pill busy\">&#9888; {len(WARNINGS)} security warning"
                   f"{'s' if len(WARNINGS) > 1 else ''}</summary><div class=secbox><ul>{items}</ul>"
                   f"<div class=\"mut small\">Set in <code>.env</code>, then <code>sh scripts/up.sh</code>. See the Security "
                   f"section of the README.</div></div></details>")
    capmb = s["capacity"] or 0
    cap = f"of a {mb(capmb * 1e6)} limit" if capmb else ""
    pct = min(100, round(s["total_cache"] / (capmb * 1e6) * 100, 1)) if capmb else 0
    unk = (f"<p class=\"mut small\">{mb(s['unknown'])} of cache belongs to no known torrent: "
           f"only “Erase everything” removes it.</p>") if s["unknown"] > 1e6 else ""
    st = s["settings"]
    capform = ("<form class=add method=post action=/settings><span class=mut>Cache cap:</span>"
               f"<label><input type=radio name=mode value=manual {'checked' if st['mode'] == 'manual' else ''}> fixed</label>"
               f"<input type=number name=cache_gb step=0.5 min=0.5 value={st['cache_mb'] / 1000:g} style=\"width:90px\"> GB"
               f"<label><input type=radio name=mode value=auto {'checked' if st['mode'] == 'auto' else ''}> automatic: keep</label>"
               f"<input type=number name=reserve_pct step=1 min=1 max=90 value={st['reserve_pct']:g} style=\"width:70px\"> % of the disk free"
               "<button type=submit>Apply (restarts)</button></form>")
    capform += ("<form class=add method=post action=/gentleman><span class=mut>Gentleman mode:</span>"
                f"<label><input type=checkbox name=on value=1 {'checked' if st['gentle_on'] else ''}> help torrents in difficulty</label>"
                f"<input type=number name=slots min=0 max=20 value={int(st['gentle_slots'])} style=\"width:60px\"> slots"
                f"<input type=number name=pct min=0 max=50 step=0.5 value={st['gentle_pct']:g} style=\"width:70px\"> % of the cache cap"
                f"<span class=mut>· upload cap</span><input type=number name=upload min=0 step=100 value={int(st['upload_kbps'])} "
                f"style=\"width:90px\"> kB/s (0 = none)<button type=submit>Apply (restarts)</button></form>"
                "<div class=\"mut small\">Torrents with 1 or 2 seeders and leechers waiting (asked to the trackers every 6 h) get a "
                "slot: their rarest pieces are downloaded into a separate part of the cache and seeded under the upload cap, "
                "until they have 3 seeders again or for 24 h, within the memory left by waiting cache requests (below). "
                "Seeding shows this machine's IP address to more peers: see the Security section of the README.</div>")
    capform += ("<form class=add method=post action=/awake style=\"margin-top:12px\"><span class=mut>Torrents kept awake:</span>"
                f"<input type=number name=mem min=0 step=100 value={int(st['awake_mem_mb'])} style=\"width:90px\"> MB estimated at most"
                f"<span class=mut>· put them back to sleep when Distribyted uses more than</span><input type=number name=ceiling "
                f"min=1000 step=500 value={int(st['memory_ceiling_mb'])} style=\"width:90px\"> MB<button type=submit>Apply</button></form>"
                "<div class=\"mut small\">A cache request that gets no data waits for a seeder: its torrent is kept awake with the "
                "pieces it needs, cheapest torrents first, within this budget (about 3.5 kB per piece: a few MB for a small torrent, "
                "more than 1 GB for one of tens of TB); gentleman mode gets what is left. Torrents left out are woken 10 min now and "
                "then (after 15 min, 30, 1 h, then every 2 h) and at once when a health check or the trackers report a seeder. "
                f"Above the ceiling, nothing new is woken and gentleman slots, then the largest waiting torrents, are put back to "
                f"sleep one at a time. Distribyted uses {mb(awake['rss'])} now.</div>")
    nwait, narrived = request_badges()
    waitpill = ((f" <a href=#reqs class=\"pill busy reqlink\" data-f=waiting title=\"cache requests waiting for a seeder\">"
                 f"{nwait} waiting</a>" if nwait else "")
                + (f" <a href=#reqs class=\"pill ok reqlink\" data-f=done title=\"waiting cache requests that arrived in the last 24 h\">"
                   f"{narrived} arrived</a>" if narrived else ""))
    a = activity_view()
    act = histogram_svg() + gentleman_summary_html()
    if awake["torrents"]:
        ws = awake["torrents"].values()
        act += (f"<div class=small style=\"margin-top:8px\"><b>Waiting for a seeder</b> <span class=mut>· {nwait} cache request(s) in "
                f"{len(ws)} torrent(s): {sum(x['mode'] == 'listening' for x in ws)} kept awake, {sum(x['mode'] == 'probing' for x in ws)} "
                f"being probed, {sum(x['mode'] == 'sleeping' for x in ws)} asleep until their next probe · Distribyted uses "
                f"{mb(awake['rss'])} of {mb(awake.get('ceiling', 0))}{' (over the ceiling: nothing new is woken)' if awake['over'] else ''}"
                f"</span> <a href=#reqs class=reqlink data-f=waiting>details</a></div>")
        recv = [x for x in ws if x.get("last_received") and time.time() - x["last_received"] < 180]
        if recv:
            act += "<div class=wrap><table><tr><th>Waiting request receiving data</th><th class=n>Down</th><th class=n>Pieces here</th><th class=n>Peers</th></tr>"
            for x in sorted(recv, key=lambda x: -x["down_rate"]):
                act += (f"<tr><td>{html.escape(x['name'])} <span class=\"mut small\">{x['hash'][:8]} · {x['requests']} request(s)</span></td>"
                        f"<td class=n>{rate(x['down_rate'])}</td><td class=n>{x['pieces_needed'] - x['missing_pieces']} of {x['pieces_needed']}</td>"
                        f"<td class=n>{x['peers']} ({x['seeders']} seeder{'s' if x['seeders'] != 1 else ''})</td></tr>")
            act += "</table></div>"
    if a["torrents"]:
        act += "<div class=wrap><table><tr><th>Torrent</th><th>Folder</th><th class=n>Down</th><th class=n>Up</th><th class=n>Peers</th></tr>"
        for x in a["torrents"][:15]:
            act += (f"<tr><td>{html.escape(x['name'])} <span class=\"mut small\">{x['hash'][:8]}</span></td><td>{html.escape(x['route'])}</td>"
                    f"<td class=n>{rate(x['down'])}</td><td class=n>{rate(x['up'])}</td><td class=n>{x['peers']}</td></tr>")
        act += "</table></div>"
    else:
        act += "<div class=\"mut small\">No transfer right now.</div>"
    pf = a["prefetch"]
    if pf["active"] or pf["queued"]:
        act += (f"<div style=\"margin-top:10px\"><b class=small>Adding to the cache</b> <span class=\"mut small\">{pf['queued']} queued "
                f"({mb(pf['queued_size'])}) · {pf['done']} done</span> <button id=pfcancel class=danger>Cancel all</button></div><div class=wrap><table>")
        peers = {x["hash"]: x["peers"] for x in activity["torrents"]}
        for j in pf["active"]:
            pct = min(100, round(j["read"] * 100 / j["size"])) if j["size"] else 0
            n = peers.get(j.get("hash"), 0)
            how = (f"waiting for peers ({n} connected)" if not j["read"] else f"{n} peer{'s' if n != 1 else ''}")
            act += (f"<tr><td class=small>{html.escape(j['path'])}<div class=bar><i style=\"width:{pct}%\"></i></div></td>"
                    f"<td class=\"n small\">{mb(j['read'])} / {mb(j['size'])}<br><span class=mut>{how} · {round(time.time() - j['since'])} s</span></td></tr>")
        act += "</table></div>"
    if pf["failed"]:
        act += "".join(f"<div class=\"mut small\">could not cache {html.escape(f['path'])} "
                       f"({time.strftime('%H:%M', time.localtime(f.get('at', 0)))}): {html.escape(f['error'])}</div>" for f in pf["failed_recent"])
        act += (f"<div class=small>{pf['failed']} failed request{'s' if pf['failed'] > 1 else ''}: see "
                "<a href=#reqs class=reqlink data-f=failed>Cache requests</a></div>")
    if a["files"]:
        act += (f"<div class=\"mut small\" style=\"margin-top:10px\">Files that received data in the last {WINDOW} s "
                "(counted per completed piece):</div><div class=wrap><table>")
        for f in a["files"][:15]:
            act += (f"<tr><td class=small>{html.escape(f['path'])}</td><td class=\"n small\">{mb(f['bytes'])}</td>"
                    f"<td class=\"n small mut\">{f['ago']} s ago</td></tr>")
        act += "</table></div>"
    capb = capmb * 1e6
    cachepie = donut([(s["total_cache"], "var(--acc)"), (max(capb - s["total_cache"], 0), "var(--chip)")],
                     f"cache {mb(s['total_cache'])} of {mb(capb)}") if capb else ""
    other = max(s["disk_total"] - s["disk_free"] - s["total_cache"], 0)
    diskpie = (donut([(other, "var(--mut)"), (s["total_cache"], "var(--acc)"), (s["disk_free"], "var(--chip)")],
                     f"other data {mb(other)} · cache {mb(s['total_cache'])} · free {mb(s['disk_free'])}")
               + "") if s["disk_total"] else ""
    return PAGE.format(capform=capform, act=act, cachepie=cachepie, diskpie=diskpie, secwarn=secwarn, waitpill=waitpill,
                       cachepill=caching_pill(),
                       ncached=sum(1 for i in s["items"] if i["cache"]), down=rate(a["down"]), up=rate(a["up"]), dfree=mb(s["disk_free"]), dtotal=mb(s["disk_total"]), dmax=mb(s["max_cap_mb"] * 1e6),
                       state=html.escape(s["state"]), pillcls="ok" if s["state"] == "running" else "busy",
                       cache=mb(s["total_cache"]), cap=cap, cachepct=pct, msg=msg, rows=rows, sources=srows, unknown=unk,
                       dis=dis, memtile=memory_tile(), rx=mb(s["network_rx"]), tx=mb(s["network_tx"]), ntor=len(s["sources"]), nfold=len(folders),
                       nawake=sum(1 for x in s["sources"] if x.get("awake")))



# ---------------------------------------------------------------- API (JSON): status, torrents, files, usage, erase

SMB_SHARE = os.environ.get("ATHENAFS_SMB_SHARE", "torrents")
# risky exposures found by scripts/up.sh (it reads .env; the share's password never reaches this container), one per line
WARNINGS = [w for w in os.environ.get("ATHENAFS_WARNINGS", "").split("|") if w.strip()]


def smb_host():
    """Name clients use for the SMB server: ATHENAFS_SMB_HOST, else the host of the page's address."""
    return os.environ.get("ATHENAFS_SMB_HOST") or urllib.parse.urlparse(PAGE_URL).hostname or "localhost"


def locations(path, is_dir=False):
    """Where a share path is found from a client: smb:// URL (macOS Finder, Linux) and UNC path (Windows)."""
    h = smb_host()
    p = path.strip("/")
    if p.split("/", 1)[0] in hidden_folders:   # hidden from the share: no address a client could open
        return {"share_path": p + ("/" if is_dir and p else ""), "smb_url": None, "unc_path": None, "hidden": True}
    return {"share_path": p + ("/" if is_dir and p else ""),
            "smb_url": f"smb://{h}/{SMB_SHARE}/" + urllib.parse.quote(p) + ("/" if is_dir and p else ""),
            "unc_path": f"\\\\{h}\\{SMB_SHARE}\\" + p.replace("/", "\\")}


def torrent_root(route, t, flat):
    """Share path of the top folder of torrent t (or of its single file)."""
    return share_path(route, t, "", flat).rstrip("/") if t["multi"] else f"{route}/{t['name']}"


def api_status():
    s = served.get("data") or snapshot()
    a = activity_view()
    srcs = s["sources"]
    checked = [health[x["hash"]] for x in srcs if x["hash"] in health]
    return {"service": s["state"], "message": s["message"], "commit": os.environ.get("ATHENAFS_COMMIT", ""),
            "cache": {"used": s["total_cache"], "cap": (s["capacity"] or 0) * 1_000_000, "unattributed": s["unknown"]},
            "disk": {"total": s["disk_total"], "free": s["disk_free"]},
            "torrents": {"total": len(srcs), "mounted": sum(x["status"].startswith("mounted") for x in srcs),
                         "awake": sum(1 for x in srcs if x.get("awake")),
                         "waiting_for_metadata": sum(x["status"] == "waiting for metadata" for x in srcs),
                         "folders": len({x["route"] for x in srcs})},
            "health": {"checked": len(checked), "with_seeders": sum(1 for x in checked if x["seeders"]),
                       "no_peer_found": sum(1 for x in checked if not x["known"] and not x["connected"])},
            "transfer": {"down": a["down"], "up": a["up"]},
            "prefetch": {"queued": a["prefetch"]["queued"], "active": len(a["prefetch"]["active"]),
                         "done": a["prefetch"]["done"], "failed": a["prefetch"]["failed"]},
            "caching_stopped": bool(load_settings().get("caching_stopped")),
            "smb": locations("", True), "warnings": WARNINGS}


def torrent_rows(folder="", hash_=""):
    s = served.get("data") or snapshot()
    tp = torrent_pieces(); flat = flat_folders(tp)
    cached = {i["hash"]: i for i in s["items"]}
    out = []
    for x in s["sources"]:
        if (folder and x["route"] != folder) or (hash_ and x["hash"] != hash_):
            continue
        t = tp.get(x["hash"]); c = cached.get(x["hash"], {})
        row = {"id": x["id"], "hash": x["hash"], "name": x["name"], "folder": x["route"], "kind": x["kind"],
               "status": x["status"], "awake": bool(x.get("awake")), "size": x["size"], "cached": c.get("cache", 0),
               "pieces_cached": c.get("pieces", f"0/{t['npieces']}" if t else ""), "files": len(t["files"]) if t else None,
               "health": health.get(x["hash"]), "health_text": health_text(x["hash"])}
        if t:
            row["location"] = locations(torrent_root(x["route"], t, flat.get(x["route"])), t["multi"])
        out.append(row)
    return out


def api_file(path):
    c = db()
    try:
        r = c.execute("SELECT hash, path, tpath, size, off FROM files WHERE path = ?", (path.strip("/"),)).fetchone()
    finally:
        c.close()
    if not r:
        return {"error": "no such file in the share (folders: use /api/search?q=folder:path)"}
    h, p, tpath, size, off = r
    tp = torrent_pieces()
    st, have = file_state(h, p, off, size, tp, set(cache_files()))
    t = tp.get(h)
    return {"path": p, "size": size, "status": st, "cached": have, "request": requests_of([p]).get(p),
            "torrent": {"hash": h, "name": t["name"] if t else "", "path_in_torrent": tpath}, "health": health.get(h), "health_text": health_text(h), **locations(p)}


def usage(by="file", limit=50, offset=0, depth=3, folder=""):
    """What takes the most room in the cache, by file, folder (share path cut at `depth` levels) or torrent."""
    limit, offset, depth = max(1, min(int(limit), 1000)), max(0, int(offset)), max(1, int(depth))
    files = cache_files(); tp = torrent_pieces(); sources = load_sources(); flat = flat_folders(tp, sources)
    route = {source_hash(x): x["route"] for x in sources}
    agg = {}
    for h, owners in piece_owners(files).items():
        for ih, i in owners:
            t = tp.get(ih)
            if not t:
                continue
            if by == "torrent":
                a = agg.setdefault(ih, {"hash": ih, "name": t["name"], "folder": route.get(ih, "?"), "size": t["total"], "cached": 0})
                a["cached"] += files[h]
                continue
            for k, part in files_of_piece(t, i, files[h]):
                fp, _, fl = t["files"][k]
                if is_padding(fp):
                    continue
                sp = share_path(route.get(ih, "?"), t, fp, flat.get(route.get(ih)))
                if folder and not sp.startswith(folder.strip("/") + "/"):
                    continue
                if by == "folder":
                    parts = sp.split("/")[:-1][:depth]
                    key = "/".join(parts)
                    a = agg.setdefault(key, {"path": key, "cached": 0, "files": set()})
                    a["files"].add(sp)
                else:
                    key = sp
                    a = agg.setdefault(key, {"path": sp, "hash": ih, "size": fl, "cached": 0})
                a["cached"] += part
    rows = sorted(agg.values(), key=lambda a: -a["cached"])
    for a in rows[offset:offset + limit]:
        if by == "folder":
            a["files"] = len(a["files"]); a.update(locations(a["path"], True))
        elif by == "file":
            a.update(locations(a["path"])); a["health_text"] = health_text(a["hash"])
        else:
            t = tp[a["hash"]]
            a["location"] = locations(torrent_root(a["folder"], t, flat.get(a["folder"])), t["multi"])
    page = rows[offset:offset + limit]
    return {"by": by, "total": len(rows), "total_cached": sum(a["cached"] for a in rows), "offset": offset, "results": page}


def erase_targets(path="", hash_=""):
    """[(info-hash, path in the torrent)] for a share path (file or folder) or a whole torrent."""
    if hash_:
        return [(hash_, "")] if hash_ in torrent_pieces() else []
    p = path.strip("/")
    if not p:
        return []
    c = db()
    try:
        rows = c.execute("SELECT hash, path, tpath FROM files WHERE path = ? OR (path >= ? AND path < ?) GROUP BY hash",
                         (p, p + "/", p + "/\U0010ffff")).fetchall()
    finally:
        c.close()
    out = []
    for h, sp, tpath in rows:
        if sp == p:
            out.append((h, tpath))   # a file
            continue
        rel = sp[len(p) + 1:]        # inside the folder
        if tpath == rel or not tpath.endswith("/" + rel):
            out.append((h, ""))      # the folder holds the whole torrent (or more)
        else:
            out.append((h, tpath[:-len(rel) - 1]))
    return out


def cached_pieces_of(hashes):
    """Cache file names (piece hashes) belonging to any of these torrents, found from the cached pieces: cheap even for
    torrents of hundreds of thousands of pieces, where listing every piece of the torrent is not."""
    hs = set(hashes)
    return {h for h, owners in piece_owners(cache_files()).items() if any(ih in hs for ih, _ in owners)}


def erase_many(targets):
    """Erase several (torrent, path) targets with one restart. Whole torrents (path '') go through the cached pieces."""
    tp = torrent_pieces()

    def job():
        wanted, kept = cached_pieces_of({ih for ih, p in targets if not p}), 0
        for ih, p in targets:
            if not p:
                continue
            w, k = pieces_to_erase(tp[ih], p)
            wanted |= w; kept += k
        n, sz = delete_pieces(wanted)
        rest = f"; {kept} edge piece(s) kept, shared with neighbouring files that were not chosen" if kept else ""
        return f"erased: {n} pieces, {sz / 1e6:.0f} MB ({len(targets)} target(s)){rest}"
    exclusive(job)


API_DOC = [
    # (method, path, parameters, description)
    ("GET", "/api/status", "", "Summary: service, cache, disk, torrents (mounted, awake, waiting), health, transfer, prefetch, SMB address."),
    ("GET", "/api/torrents", "folder (optional)", "Every torrent: folder, status, awake, size, cached, health, location of its top folder in the share."),
    ("GET", "/api/torrents/{hash}", "", "One torrent, with its largest cached files."),
    ("GET", "/api/search", "q (words, all must match; or folder:path), sort (name, largest or smallest; default: torrent order), status (cached or not cached; default: all), limit (≤ 500), offset",
     "Files whose path contains every word. Each result: size, status (cached, partial, not cached, downloading, queued, caching, waiting, failed), cached bytes, its Cache request if any, health, smb_url, unc_path. status=cached draws from the list of complete files (see /api/cached): complete, never capped. status=not cached looks at the first 10,000 matches only (total_capped says so). For the whole list of cached files, use /api/cached."),
    ("GET", "/api/cached", "folder (optional: only under this share path), format (json or ndjson)",
     "Every file completely in the cache, in one answer with no page cap, sorted by path: path, size, torrent hash, folder, hidden. Kept up to date every 30 s. Fields generation (grows when the list changes; also the ETag: send If-None-Match to get 304 Not Modified when nothing changed), changed and checked (times), total, total_size. ndjson: a first line with those fields, then one line per file. 503 with Retry-After for a few seconds after a start."),
    ("GET", "/api/file", "path (as in the share)", "One file: status, cached bytes, its Cache request if any (state, error, attempts), torrent, health, smb_url, unc_path."),
    ("GET", "/api/requests", "state (all, open, waiting, failed, done or cancelled), q (words that must all appear in the path), limit (≤ 500), offset",
     "Cache requests, kept across restarts: state (queued, caching, waiting for a seeder, failed, done, cancelled), bytes read, error, waiting since, arrived, counts per state; a waiting one says how its torrent waits (kept awake, probed, or asleep until its next probe). A request that gets no data waits as long as needed; failed is only for files that cannot be read."),
    ("GET", "/api/waiting", "", "Torrents kept awake or probed for waiting cache requests (pieces missing, peers, next probe), the memory budget shared with gentleman mode, the ceiling, and Distribyted's memory now and over the last 24 h."),
    ("GET", "/api/usage", "by (file, folder or torrent), depth (folder levels, default 3), folder (only under this share path), limit, offset",
     "What takes the most room in the cache, largest first."),
    ("GET", "/api/activity", "", "Rates per torrent, files that received data in the last 30 s, prefetch queue."),
    ("GET", "/api/gentleman", "", "Gentleman mode: settings, the torrents helped (swarm from the trackers, rare pieces held and asked for, upload sent), and how many torrents the trackers report in difficulty or with no seeder."),
    ("GET", "/api/folders", "", "Per folder: hidden from the share or not, ready view on or off, its name and location, how many complete files and archives it shows."),
    ("POST", "/api/ready-view", "folder, on (true or false), name (optional)",
     "Turn a folder's ready view on or off: a sibling folder showing the files that are completely in the cache, archives (zip, 7z, rar) opened as folders. Browsing it never downloads. Restarts the service."),
    ("POST", "/api/folder-remove", "folder, erase (true by default: also erase the cached data; false keeps it)",
     "Remove every torrent of a folder, with one restart. To erase a folder's cached data and keep its torrents: POST /api/erase with path=<folder>."),
    ("POST", "/api/folder-visibility", "folder, hidden (true or false)",
     "Hide a folder from the share or show it again (Samba only, a few seconds, no restart). A hidden folder stays in the catalogue; with its ready view on, the share shows only what is ready."),
    ("GET", "/api", "", "Full status as used by the page (sources, cache tree, settings)."),
    ("POST", "/api/caching", "stopped (true or false)",
     "Stop caching (true): nothing new is downloaded until resumed, also after a restart. What is in the cache stays available over SMB; a read needing data that is not cached fails within 2 s; Cache requests are refused; queued and waiting ones pause; gentleman mode takes no slot. false resumes. No restart. The state is caching_stopped in /api/status."),
    ("POST", "/api/prefetch", "path (repeatable) or q (a search, with sort and status as in /api/search)", "Read files into the cache ahead of time, in that order. Refused when larger than 90 % of the cache cap."),
    ("POST", "/api/prefetch/cancel", "", "Empty the prefetch queue and stop the reads in progress."),
    ("POST", "/api/requests/clear", "", "Forget finished Cache requests (done, cancelled, failed); queued, running and waiting ones stay."),
    ("POST", "/api/requests/retry", "path (repeatable) or all=true (every failed request)", "Queue failed or cancelled Cache requests again."),
    ("POST", "/api/requests/cancel", "path (repeatable) or all=true (everything queued, being read or waiting)",
     "Cancel Cache requests; a waiting request cancelled lets its torrent sleep."),
    ("POST", "/api/erase", "path (file or folder of the share) or hash (whole torrent)",
     "Erase cached data. Restarts the service (the share is unavailable a few seconds). Edge pieces shared with files that were not chosen are kept."),
]


def api_docs_html():
    rows = "".join(f"<tr><td><code>{m}</code></td><td><code>{html.escape(p)}</code></td><td>{html.escape(a)}</td><td>{html.escape(d)}</td></tr>"
                   for m, p, a, d in API_DOC)
    host = html.escape(PAGE_URL.rstrip("/") or "http://localhost:8080")
    return f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>athenafs API</title>
<style>:root{{--bg:#f6f7f9;--fg:#1b1d21;--mut:#6b7079;--line:#e3e5ea;--card:#fff}}@media (prefers-color-scheme:dark){{:root{{--bg:#111315;--fg:#e6e7e9;--mut:#9aa0a8;--line:#2c3035;--card:#1a1d20}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:16px}}.page{{max-width:1040px;margin:auto}}
table{{width:100%;border-collapse:collapse;background:var(--card)}}td,th{{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}}
code,pre{{font:13px ui-monospace,monospace}}pre{{background:var(--card);border:1px solid var(--line);padding:10px;overflow-x:auto}}.mut{{color:var(--mut)}}a{{color:inherit}}</style>
<div class=page><h1>athenafs API</h1><p class=mut>JSON over HTTP. Sizes in bytes, times in unix seconds. <a href=/>Back to the page</a> · <a href=/api/openapi.json>openapi.json</a></p>
<table><tr><th>Method</th><th>Path</th><th>Parameters</th><th>What</th></tr>{rows}</table>
<h2>POST requests</h2><p>Form-encoded or JSON body. They must carry either an <code>Origin</code> header naming this host (the page does)
or <code>X-Athenafs: 1</code> (scripts): a header a third-party site cannot add, so other sites cannot act on your behalf.
When a password is set (<code>ATHENAFS_PASSWORD</code>), use basic authentication with the user <code>admin</code>.</p>
<h2>Examples</h2><pre>curl -s '{host}/api/search?q=readme+pdf&amp;limit=5'
curl -s '{host}/api/usage?by=folder&amp;depth=3&amp;limit=10'
curl -s -H 'X-Athenafs: 1' --data-urlencode 'path=Some/folder/file.zip' {host}/api/prefetch
curl -s -H 'X-Athenafs: 1' -H 'Content-Type: application/json' -d '{{"path": "Some/folder"}}' {host}/api/erase</pre></div>"""


def api_openapi():
    paths = {}
    for m, p, a, d in API_DOC:
        params = [{"name": n.strip().split(" ")[0], "in": "path" if "{" + n.strip().split(" ")[0] + "}" in p else "query",
                   "description": n.strip()} for n in a.split(",") if n.strip()] if m == "GET" else []
        op = {"summary": d, "responses": {"200": {"description": "JSON"}}}
        if params:
            op["parameters"] = params
        if m == "POST" and a:
            op["requestBody"] = {"description": a, "content": {"application/x-www-form-urlencoded": {}, "application/json": {}}}
        paths.setdefault(p, {})[m.lower()] = op
    return {"openapi": "3.0.3", "info": {"title": "athenafs", "version": os.environ.get("ATHENAFS_COMMIT", "")}, "paths": paths}


def body_params(raw, ctype):
    """POST body as {name: [values]}, form-encoded or JSON."""
    if "json" in (ctype or ""):
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return {k: (v if isinstance(v, list) else [v]) for k, v in d.items()} if isinstance(d, dict) else {}
    return urllib.parse.parse_qs(raw.decode("utf-8", "replace"))


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
        if ok and self.command == "POST" and self.headers.get("X-Athenafs") != "1":   # scripts: see /api/docs
            o = self.headers.get("Origin") or self.headers.get("Referer") or ""
            ok = bool(o) and o.split("//", 1)[-1].split("/", 1)[0] == self.headers.get("Host", "")
        if not ok:
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Mounted torrents"')
            self.send_header("Content-Length", "0"); self.end_headers()
        return ok

    def do_GET(self):
        if not self.authorized():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        one = lambda k, d="": (q.get(k) or [d])[0]
        res, body, ct = None, None, "application/json"
        if u.path == "/api/cached":
            return self.send_cached(one("folder"), one("format", "json"))
        try:
            if u.path == "/api":
                body = served["json"] or json.dumps(snapshot()).encode()
            elif u.path == "/api/search":
                res = search(one("q"), one("limit", "50"), one("offset", "0"), one("sort"), one("status"))
            elif u.path == "/api/activity":
                res = activity_view()
            elif u.path == "/api/status":
                res = api_status()
            elif u.path == "/api/torrents":
                res = {"torrents": torrent_rows(folder=one("folder"))}
            elif u.path.startswith("/api/torrents/"):
                h = u.path.rsplit("/", 1)[-1].lower()
                rows = torrent_rows(hash_=h)
                res = dict(rows[0], largest_cached_files=[f for f in usage("file", 1000)["results"] if f["hash"] == h][:20]) \
                    if rows else {"error": "unknown torrent"}
            elif u.path == "/api/file":
                res = api_file(one("path"))
            elif u.path == "/api/waiting":
                res = awake_view()
            elif u.path == "/api/requests":
                res = requests_view(one("state", "all"), one("limit", "50"), one("offset", "0"), one("q"))
            elif u.path == "/api/usage":
                res = usage(one("by", "file"), one("limit", "50"), one("offset", "0"), one("depth", "3"), one("folder"))
            elif u.path == "/api/gentleman":
                res = gentleman_view()
            elif u.path == "/api/folders":
                res = folder_settings_status()
            elif u.path == "/api/openapi.json":
                res = api_openapi()
            elif u.path == "/api/docs":
                body = api_docs_html().encode(); ct = "text/html; charset=utf-8"
        except ValueError:
            res = {"error": "limit, offset and depth must be integers"}
        if res is not None:
            body = json.dumps(res).encode()
        if body is None:   # the page
            body = served["html"] or render().encode(); ct = "text/html; charset=utf-8"
        self.send_response(200); self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def send_cached(self, folder, fmt):
        """/api/cached: the whole list in one answer, with an ETag (the generation) so that a client can poll cheaply."""
        if not complete["ready"]:
            body = json.dumps({"error": "the list is being built after a start, retry in a few seconds"}).encode()
            self.send_response(503); self.send_header("Retry-After", "10")
        else:
            etag = f'"gen-{complete["generation"]}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304); self.send_header("ETag", etag); self.send_header("Content-Length", "0"); self.end_headers()
                return
            head, items = cached_view(folder, fmt)
            if fmt == "ndjson":   # one line for the totals, then one line per file
                body = "\n".join(json.dumps(x) for x in [head] + items).encode() + b"\n"
            else:
                body = json.dumps(dict(head, files=items)).encode()
            self.send_response(200); self.send_header("ETag", etag)
        self.send_header("Content-Type", "application/x-ndjson" if fmt == "ndjson" and complete["ready"] else "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        if not self.authorized():
            return
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_TORRENT:
            self.send_response(413); self.send_header("Content-Length", "0"); self.end_headers(); return
        raw = self.rfile.read(n)
        go = lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start()
        if self.path in ("/api/caching", "/api/prefetch", "/api/prefetch/cancel", "/api/requests/retry", "/api/requests/cancel", "/api/requests/clear", "/api/erase", "/api/ready-view", "/api/folder-visibility",
                         "/api/folder-remove"):
            q = body_params(raw, self.headers.get("Content-Type"))
            one = lambda k: str((q.get(k) or [""])[0])
            yes = lambda k: one(k).lower() in ("1", "true", "on", "yes")
            if self.path == "/api/caching":
                res = set_caching(yes("stopped")) if one("stopped") else {"error": "give stopped=true or stopped=false"}
            elif self.path == "/api/ready-view":
                res = set_ready_view(one("folder"), yes("on"), one("name"))
            elif self.path == "/api/folder-visibility":
                res = set_hidden(one("folder"), yes("hidden"))
            elif self.path == "/api/folder-remove":
                if not any(x["route"] == one("folder") for x in load_sources()):
                    res = {"error": "unknown folder"}
                else:
                    go(remove_folder, one("folder"), one("erase").lower() not in ("0", "false", "no", "off"))
                    res = {"removing": one("folder"), "note": "the service restarts; see /api/status for the result"}
            elif self.path == "/api/erase":
                h = one("hash").lower()
                targets = erase_targets(one("path"), h if re.fullmatch(r"[0-9a-f]{40}", h) else "")
                if targets and state["state"] == "running":
                    go(erase_many, targets)
                    res = {"erasing": len(targets), "note": "the service restarts; see /api/status for the result"}
                else:
                    res = {"error": "nothing to erase there" if not targets else "the service is busy, retry in a few seconds"}
            elif self.path == "/api/requests/clear":
                res = requests_clear()
            elif self.path.startswith("/api/requests/"):
                paths = [str(x) for x in q.get("path") or []]
                if not paths and not yes("all"):
                    res = {"error": "give path (repeatable) or all=true"}
                elif self.path.endswith("retry"):
                    res = prefetch_retry(paths or None)
                else:
                    res = prefetch_cancel(paths or None)
            elif self.path.endswith("cancel"):
                res = prefetch_cancel()
            else:
                res = prefetch_add(one("q"), [str(x) for x in q.get("path") or []], one("sort"), one("status"))
            body = json.dumps(res).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.path == "/upload":
            name = urllib.parse.unquote(self.headers.get("X-Filename", "")).rsplit(".torrent", 1)[0]
            go(lambda: exclusive(lambda: add_torrent_bytes(raw, name)))
        else:
            q = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
            one = lambda k: (q.get(k) or [""])[0]
            if self.path == "/erase" and (one("h") == "all" or re.fullmatch(r"[0-9a-f]{40}", one("h"))):
                go(erase, one("h"), one("p"))
            elif self.path == "/gentleman":
                go(apply_gentleman, one("on") == "1", one("slots") or "0", one("pct") or "0", one("upload") or "0")
            elif self.path == "/caching":
                set_caching(one("stopped") == "1")
            elif self.path == "/awake":
                apply_awake(one("mem"), one("ceiling"))
            elif self.path == "/settings":
                go(apply_settings, one("mode"), one("cache_gb") or "0", one("reserve_pct") or "20")
            elif self.path == "/add" and one("link"):
                go(add_links, one("link"), one("route"))
            elif self.path == "/rename" and one("new") and (one("route") or re.fullmatch(r"[0-9a-f]{8}", one("id"))):
                go(rename, one("new"), one("route"), one("id") if not one("route") else "")
            elif self.path == "/ready-view" and one("folder"):
                res = set_ready_view(one("folder"), one("on") == "1", one("name"))
                if res.get("error"):
                    state["message"] = res["error"]
            elif self.path == "/folder-erase" and one("folder"):
                targets = erase_targets(one("folder"))
                if targets:
                    go(erase_many, targets)
                else:
                    state["message"] = "nothing cached to erase there"
            elif self.path == "/folder-remove" and one("folder"):
                go(remove_folder, one("folder"), one("erase") != "0")
            elif self.path == "/folder-visibility" and one("folder"):
                res = set_hidden(one("folder"), one("hidden") == "1")
                if res.get("error"):
                    state["message"] = res["error"]
            elif self.path == "/remove" and re.fullmatch(r"[0-9a-f]{8}", one("id")):
                go(remove_source, one("id"), one("erase") != "0")
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()


# ---------------------------------------------------------------- activity (rates, files receiving pieces)

WINDOW = 30   # seconds of history for "files receiving data"
rate_history = collections.deque(maxlen=300)   # (time, down, up) every 2 s: the last 10 minutes, for the histogram
activity = {"at": 0, "routes": [], "down": 0, "up": 0, "net_rx": 0, "net_tx": 0, "torrents": [], "files": []}


def sampler():
    """Every 2 s: rates per torrent (from Distribyted's cumulative counters) and the files whose pieces just reached the cache.
    The only regular caller of Distribyted's /routes, so its numbers are not disturbed by page refreshes."""
    prev, prev_net, prev_pieces, recent = {}, None, None, []
    while True:
        time.sleep(2)
        now = time.time()
        routes = api("/routes", 20)   # slow while hundreds of torrents are being added (the client lock is busy)
        p = state.get("proc")
        if routes is None and p and p.poll() is None and now - activity["at"] < 120:
            continue   # running but busy: keep the last known state for a while
        if routes is None:   # stopped or restarting
            prev, prev_pieces = {}, None
            activity.update(at=now, routes=[], down=0, up=0, torrents=[])
            continue
        net = network()
        if prev_net:
            dt = now - prev_net[0]
            activity["net_rx"], activity["net_tx"] = (net[0] - prev_net[1]) / dt, (net[1] - prev_net[2]) / dt
        prev_net = (now, *net)
        rows, cur, fresh = [], {}, {}
        for r in routes:
            for t in r.get("torrentStats") or []:
                d, u = t.get("totalDownloadedBytes", 0), t.get("totalUploadedBytes", 0)
                cur[t["hash"]] = (now, d, u)
                p = prev.get(t["hash"])
                rd = max(0, (d - p[1]) / (now - p[0])) if p else 0
                ru = max(0, (u - p[2]) / (now - p[0])) if p else 0
                hl = t.get("health")
                if hl and hl.get("checked", 0) > health.get(t["hash"], {}).get("checked", 0):
                    fresh[t["hash"]] = {k: hl.get(k, 0) for k in ("known", "connected", "seeders", "checked")}
                rows.append({"hash": t["hash"], "name": t["name"], "route": r["name"], "down": rd, "up": ru,
                             "peers": t.get("peers", 0), "seeders": t.get("seeders", 0),
                             "downloaded": d, "uploaded": u})
        prev = cur
        if fresh:
            health.update(fresh)
            try:
                save_health(fresh)
            except sqlite3.Error as e:
                print("health save:", e)
        # pieces that appeared in the cache since the last sample -> torrent, file
        try:
            pieces = {e.name: e.stat().st_size for e in os.scandir(CACHE)}
        except OSError:
            pieces = {}
        if prev_pieces is not None:
            new = pieces.keys() - prev_pieces.keys()
            if new:
                tp = torrent_pieces()
                piece_owner = piece_owners(new)
                route_of = {x["hash"]: x["route"] for x in rows}
                flat = flat_folders(tp)
                for h in new:
                    for ih, i in piece_owner.get(h, ()):
                        t = tp.get(ih)
                        # a piece can span several files: credit each file the part of the piece it holds
                        for k, part in (files_of_piece(t, i, pieces[h]) if t else ()):
                            path = t["files"][k][0]
                            if not is_padding(path):
                                recent.append((now, ih, share_path(route_of.get(ih, "?"), t, path, flat.get(route_of.get(ih))), part))
        prev_pieces = pieces
        recent = [x for x in recent if x[0] > now - WINDOW]
        files = {}
        for at, ih, path, n in recent:
            f = files.setdefault(path, {"path": path, "hash": ih, "bytes": 0, "last": 0})
            f["bytes"] += n; f["last"] = max(f["last"], at)
        rate_history.append((now, sum(x["down"] for x in rows), sum(x["up"] for x in rows)))
        activity.update(at=now, routes=routes, torrents=sorted(rows, key=lambda x: -(x["down"] + x["up"])),
                        down=sum(x["down"] for x in rows), up=sum(x["up"] for x in rows),
                        files=sorted(files.values(), key=lambda f: -f["last"])[:50])


# ---------------------------------------------------------------- file search

def db():
    c = sqlite3.connect(INDEX, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def index_wanted():
    """info-hash -> (folder, layout), for every source whose metadata is known. The layout changes when the folder's
    top-level directory starts or stops being skipped, which moves every path of the torrent in the share."""
    tp, sources = torrent_pieces(), load_sources()
    flat = flat_folders(tp, sources)
    return {h: (s["route"], share_path(s["route"], tp[h], "", flat[s["route"]]))
            for s in sources if (h := source_hash(s)) in tp}, tp, flat


def sync_index():
    """Bring the index in line with the sources: only torrents added, removed or moved to another folder are (re)indexed.
    Reads the .torrent files only: nothing is downloaded and archives are not opened."""
    wanted, tp, flat = index_wanted()
    c = db()
    with c:
        c.execute("CREATE TABLE IF NOT EXISTS torrents(hash TEXT PRIMARY KEY, route TEXT, layout TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, hash TEXT, path TEXT, tpath TEXT, size INTEGER, off INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS files_hash ON files(hash)")
        c.execute("CREATE INDEX IF NOT EXISTS files_path ON files(path)")
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(path, content='files', content_rowid='id', tokenize='trigram')")
    have = dict(((h, (r, l)) for h, r, l in c.execute("SELECT hash, route, layout FROM torrents")))
    changed = 0
    for h in set(have) | set(wanted):
        t = tp.get(h)
        want = wanted.get(h)
        if have.get(h) == want:
            continue
        with c:
            if h in have:
                c.execute("INSERT INTO files_fts(files_fts, rowid, path) SELECT 'delete', id, path FROM files WHERE hash=?", (h,))
                c.execute("DELETE FROM files WHERE hash=?", (h,))
                c.execute("DELETE FROM torrents WHERE hash=?", (h,))
            if want:
                rows = [(h, share_path(want[0], t, p, flat[want[0]]), p, n, o) for p, o, n in t["files"] if n > 0 and not is_padding(p)]
                c.executemany("INSERT INTO files(hash, path, tpath, size, off) VALUES (?,?,?,?,?)", rows)
                c.execute("INSERT INTO files_fts(rowid, path) SELECT id, path FROM files WHERE hash=?", (h,))
                c.execute("INSERT INTO torrents VALUES (?,?,?)", (h, *want))
        changed += 1
    c.close()
    return changed


pieces_ready = {"done": False}   # False until every known torrent's pieces are in the table


def sync_pieces(tp):
    """Table piece hash -> (torrent, index) for every known torrent (on disk: 12 million pieces would cost GBs in memory).
    Only torrents added or gone since the last run are written."""
    c = db()
    with c:
        c.execute("CREATE TABLE IF NOT EXISTS ptorrents(id INTEGER PRIMARY KEY, ih TEXT UNIQUE)")
        c.execute("CREATE TABLE IF NOT EXISTS pieces(h BLOB, t INTEGER, idx INTEGER)")
        c.execute("CREATE INDEX IF NOT EXISTS pieces_h ON pieces(h)")
    have = dict(c.execute("SELECT ih, id FROM ptorrents").fetchall())
    changed = 0
    for ih in set(have) - set(tp):
        with c:
            c.execute("DELETE FROM pieces WHERE t=?", (have[ih],)); c.execute("DELETE FROM ptorrents WHERE id=?", (have[ih],))
        changed += 1
    for ih in set(tp) - set(have):
        t = tp[ih]; b = t["pieces"]
        with c:
            tid = c.execute("INSERT INTO ptorrents(ih) VALUES (?)", (ih,)).lastrowid
            c.executemany("INSERT INTO pieces VALUES (?,?,?)", ((b[20 * i:20 * i + 20], tid, i) for i in range(t["npieces"])))
        changed += 1
    c.close()
    pieces_ready["done"] = True
    return changed


def piece_owners(hashes):
    """{hash (hex): [(info-hash, piece index)]} for the given cache file names; unknown hashes are left out."""
    out, hs = {}, [h for h in hashes if len(h) == 40]
    c = db()
    try:
        for k in range(0, len(hs), 500):
            chunk = [bytes.fromhex(h) for h in hs[k:k + 500]]
            q = ("SELECT pieces.h, ptorrents.ih, pieces.idx FROM pieces JOIN ptorrents ON ptorrents.id = pieces.t "
                 f"WHERE pieces.h IN ({','.join('?' * len(chunk))})")
            for h, ih, i in c.execute(q, chunk):
                out.setdefault(h.hex(), []).append((ih, i))
    except sqlite3.OperationalError:   # table not built yet
        pass
    finally:
        c.close()
    return out


def indexer():
    """Keeps the index in sync: at start, and whenever the sources or the known metadata change."""
    last = None
    while True:
        try:
            wanted, tp, _ = index_wanted()
            key = (sorted(wanted.items()), sorted(tp))
            if key != last:
                t0 = time.time(); n = sync_pieces(tp)
                if n:
                    print(f"piece table: {n} torrents (re)indexed in {time.time() - t0:.0f} s")
                n = sync_index()
                if n:
                    print(f"search index: {n} torrents (re)indexed")
                last = key
        except Exception as e:
            print("search index error:", e)
        time.sleep(10)


def query_sql(q):
    """SQL (FROM/WHERE part, args) for a search: words that must all appear in the path, or folder:<path> for everything
    under a folder of the share."""
    q = q.strip()
    if q.startswith("folder:"):
        prefix = q[7:].strip().strip("/") + "/"
        return "FROM files WHERE path >= ? AND path < ?", [prefix, prefix + "\U0010ffff"]
    words = [w for w in q.split() if w]
    if not words:
        return None, None
    if all(len(w) >= 3 for w in words):   # trigram index: substring match
        m = " AND ".join('"' + w.replace('"', '""') + '"' for w in words)
        return "FROM files_fts JOIN files ON files.id = files_fts.rowid WHERE files_fts MATCH ?", [m]
    # a word shorter than 3 characters: plain scan
    return ("FROM files WHERE " + " AND ".join("path LIKE ? ESCAPE '\\'" for _ in words),
            ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words])


SEARCH_CAP = 10000   # results counted (and cacheable at once) at most


def file_state(h, path, off, size, tp, cached):
    """(status, cached bytes) of one file. Status: cached, partial, not cached, queued / caching / failed (a Cache
    request) and downloading (pieces of it reached the cache in the last seconds)."""
    t, have = tp.get(h), 0
    present = total = 0
    if t and size > 0:
        pl = t["pl"]
        for i in range(off // pl, (off + size - 1) // pl + 1):
            total += 1
            if piece(t, i) in cached:
                present += 1
        have = min(size, present * pl)
    with plock:
        if path in prefetch["active"] and not prefetch["active"][path]["cancel"]:
            return "caching", have
        if path in prefetch["queued"]:
            return "queued", have
    if total and present == total:
        return "cached", size
    if path in prefetch["failed"]:
        return "failed", have
    if path in awake["paths"]:
        return "waiting", have
    if any(f["path"] == path for f in activity["files"]):
        return "downloading", have
    return ("partial" if present else "not cached"), have


SEARCH_SORTS = {"": "", "name": " ORDER BY files.path", "largest": " ORDER BY files.size DESC", "smallest": " ORDER BY files.size"}
SEARCH_STATUS = ("", "cached", "not cached")   # not cached: anything not completely in the cache


def search_rows(c, q, sort="", status="", limit=None, offset=0):
    """(rows, total, total_size, capped) of a search, sorted and filtered by status. Rows: (hash, path, tpath, size,
    off, status, cached bytes) when a status filter is given, else (hash, path, tpath, size, off) and the status is
    computed by the caller for the page shown only. limit None: every row (at most SEARCH_CAP)."""
    base, args = query_sql(q)
    if not base:
        return [], 0, 0, False
    order = SEARCH_SORTS[sort]   # "" = the order of the index (torrent by torrent)
    if not status:
        total, total_size = c.execute(f"SELECT count(*), coalesce(sum(size), 0) FROM (SELECT files.size {base} LIMIT {SEARCH_CAP + 1})",
                                      args).fetchone()
        n = SEARCH_CAP if limit is None else limit
        rows = c.execute(f"SELECT files.hash, files.path, files.tpath, files.size, files.off {base}{order} LIMIT ? OFFSET ?",
                         args + [n, offset]).fetchall()
        return rows, min(total, SEARCH_CAP), total_size, total > SEARCH_CAP
    if status == "cached" and complete["ready"]:   # complete and consistent: from the list of complete files
        return cached_search(q, sort, limit, offset)
    # a status filter: every match is looked at (at most SEARCH_CAP), then the page is cut from what is left
    rows = c.execute(f"SELECT files.hash, files.path, files.tpath, files.size, files.off {base}{order} LIMIT {SEARCH_CAP + 1}",
                     args).fetchall()
    capped = len(rows) > SEARCH_CAP
    tp, cached = torrent_pieces(), set(cache_files())
    kept = []
    for h, path, tpath, size, off in rows[:SEARCH_CAP]:
        st, have = file_state(h, path, off, size, tp, cached)
        if (st == "cached") == (status == "cached"):
            kept.append((h, path, tpath, size, off, st, have))
    page = kept[offset:] if limit is None else kept[offset:offset + limit]
    return page, len(kept), sum(r[3] for r in kept), capped


def cached_search(q, sort, limit, offset):
    """search_rows() for status=cached: the files of the complete list that match q (same rules as the index: every word
    as a case-insensitive substring of the path, or folder:<path>). Never capped."""
    q = q.strip()
    with clock:
        files = list(complete["files"].values())
    if q.startswith("folder:"):
        prefix = q[7:].strip().strip("/") + "/"
        rows = [f for f in files if f["path"].startswith(prefix)]
    else:
        words = [w.lower() for w in q.split()]
        rows = [f for f in files if all(w in f["path"].lower() for w in words)]
    key = {"name": lambda f: f["path"], "largest": lambda f: -f["size"], "smallest": lambda f: f["size"]}.get(sort)
    rows.sort(key=key or (lambda f: (f["hash"], f["off"])))   # "": torrent by torrent, like the index
    page = rows[offset:] if limit is None else rows[offset:offset + limit]
    return ([(f["hash"], f["path"], f["torrent_path"], f["size"], f["off"], "cached", f["size"]) for f in page],
            len(rows), sum(f["size"] for f in rows), False)


def search(q, limit=50, offset=0, sort="", status=""):
    """Files whose path (as seen in the share) contains every word of q, case-insensitive; or folder:<path>.
    sort: "" (torrent order), name, largest, smallest; status: "" (all), cached, not cached."""
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    if sort not in SEARCH_SORTS or status not in SEARCH_STATUS:
        return {"error": "sort: name, largest or smallest; status: cached or not cached"}
    if not query_sql(q)[0]:
        return {"query": q, "total": 0, "results": []}
    c = db()
    try:
        rows, total, total_size, capped = search_rows(c, q, sort, status, limit, offset)
    except sqlite3.OperationalError as e:
        return {"query": q, "total": 0, "results": [], "error": str(e)}
    finally:
        c.close()
    tp, cached = torrent_pieces(), set(cache_files())
    reqs = requests_of(r[1] for r in rows)
    out = []
    for r in rows:
        h, path, tpath, size, off = r[:5]
        st, have = r[5:] if status else file_state(h, path, off, size, tp, cached)
        out.append({"path": path, "size": size, "hash": h, "torrent_path": tpath, "cached": have, "status": st,
                    "request": reqs.get(path),
                    "health": health.get(h), "health_text": health_text(h), "dead": looks_dead(h), **locations(path)})
    return {"query": q, "sort": sort, "status": status, "total": total, "total_capped": capped, "total_size": total_size,
            "offset": offset, "results": out}


# ---------------------------------------------------------------- cache requests ("Cache"): read files through the mount

PREFETCH_WORKERS = 2
REQUESTS = "/data/requests.db"   # every Cache request and its state (kept apart from the index, which can be rebuilt)
plock = threading.Lock()
# A request is read through the mount once. If no data comes (no reachable seeder), it waits: its torrent is kept awake
# with the pieces it needs (see waiter()) until they arrive or the request is cancelled; it never fails for that reason.
# In memory: the queue (paths, in order), the reads in progress, and the failed requests (for the search's status);
# the requests table holds the rest and lets the queue survive a restart
prefetch = {"queue": collections.deque(), "queued": {}, "hash": {}, "active": {}, "done": 0, "failed": {}, "bytes": 0}


def rdb():
    c = sqlite3.connect(REQUESTS, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS requests(path TEXT PRIMARY KEY, hash TEXT, size INTEGER, state TEXT, created REAL, "
              "updated REAL, seq INTEGER, attempts INTEGER DEFAULT 0, auto_retries INTEGER DEFAULT 0, error TEXT DEFAULT '', "
              "retriable INTEGER DEFAULT 0, read INTEGER DEFAULT 0)")
    c.execute("CREATE INDEX IF NOT EXISTS requests_state ON requests(state, updated)")
    cols = {r[1] for r in c.execute("PRAGMA table_info(requests)")}
    for col in ("waiting_since REAL", "arrived REAL"):   # added with the waiting state
        if col.split()[0] not in cols:
            c.execute(f"ALTER TABLE requests ADD COLUMN {col}")
    # torrents of waiting requests: probes done and the next one (backoff), kept across restarts
    c.execute("CREATE TABLE IF NOT EXISTS wait_torrents(hash TEXT PRIMARY KEY, probes INTEGER, next_probe REAL, last_probe REAL)")
    # what the waiting torrents saw (how often a seeder shows up) and Distribyted's memory (to set the budget from data)
    c.execute("CREATE TABLE IF NOT EXISTS wait_log(at REAL, hash TEXT, mode TEXT, peers INTEGER, seeders INTEGER, missing INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS mem_log(at REAL, rss INTEGER, awake INTEGER, listening INTEGER, listening_est INTEGER, "
              "probing INTEGER, gentleman INTEGER)")
    return c


def req_set(path, **f):
    f["updated"] = time.time()
    c = rdb()
    with c:
        c.execute(f"UPDATE requests SET {', '.join(k + '=?' for k in f)} WHERE path=?", (*f.values(), path))
    c.close()


def load_requests():
    """At start: queue again what was queued or being read when the service stopped, in the same order; forget finished
    requests older than 30 days."""
    c = rdb()
    with c:
        c.execute("DELETE FROM requests WHERE state IN ('done', 'cancelled') AND updated < ?", (time.time() - 30 * 86400,))
        c.execute("DELETE FROM wait_log WHERE at < ?", (time.time() - 30 * 86400,))
        c.execute("DELETE FROM mem_log WHERE at < ?", (time.time() - 30 * 86400,))
        c.execute("UPDATE requests SET state='queued' WHERE state='caching'")
        # before the waiting state, a request with no reachable seeder failed: it waits now
        c.execute("UPDATE requests SET state='waiting', waiting_since=updated WHERE state='failed' AND retriable=1")
    rows = c.execute("SELECT path, hash, size, state, error, updated, attempts, auto_retries FROM requests "
                     "WHERE state IN ('queued', 'failed') ORDER BY seq").fetchall()
    c.close()
    with plock:
        for p, h, n, st, err, at, att, auto in rows:
            if st == "queued":
                prefetch["queue"].append(p); prefetch["queued"][p] = n; prefetch["hash"][p] = h
            else:
                prefetch["failed"][p] = {"path": p, "error": err, "at": at, "attempts": att, "auto_retries": auto}
    print(f"cache requests: {sum(1 for r in rows if r[3] == 'queued')} queued again, {sum(1 for r in rows if r[3] == 'failed')} failed")


def enqueue(rows, manual=True):
    """Queue (path, size, hash) rows, under plock. A manual request starts a fresh count of automatic retries."""
    now = time.time()
    c = rdb()
    seq = (c.execute("SELECT max(seq) FROM requests").fetchone()[0] or 0) + 1
    with c:
        c.executemany("INSERT INTO requests(path, hash, size, state, created, updated, seq) VALUES (?,?,?,'queued',?,?,?) "
                      "ON CONFLICT(path) DO UPDATE SET hash=excluded.hash, size=excluded.size, state='queued', "
                      "updated=excluded.updated, seq=excluded.seq, read=0, arrived=NULL" + (", created=excluded.created, "
                      "attempts=0, auto_retries=0, error='', waiting_since=NULL" if manual else ""),
                      [(p, h, n, now, now, seq + k) for k, (p, n, h) in enumerate(rows)])
    c.close()
    for p, n, h in rows:
        prefetch["queue"].append(p); prefetch["queued"][p] = n; prefetch["hash"][p] = h
        prefetch["failed"].pop(p, None)


def prefetch_add(q="", paths=(), sort="", status=""):
    """Queue the files of a search (q, sort and status as in search(), at most SEARCH_CAP, in that order) or the given
    share paths for reading. Refused when the total is larger than the cache cap: the first files would be evicted
    before the last are read."""
    if sort not in SEARCH_SORTS or status not in SEARCH_STATUS:
        return {"error": "sort: name, largest or smallest; status: cached or not cached"}
    if caching_stopped():
        return {"error": "caching is stopped (Resume caching on the page, or POST /api/caching stopped=false): nothing queued",
                "caching_stopped": True}
    c = db()
    try:
        if q:
            if not query_sql(q)[0]:
                return {"error": "empty query"}
            rows = [(r[1], r[3], r[0]) for r in search_rows(c, q, sort, status)[0]]
        else:
            rows = [r for p in paths for r in c.execute("SELECT path, size, hash FROM files WHERE path = ?", (p,)).fetchall()]
    finally:
        c.close()
    capmb = effective_cap_mb(load_settings())
    with plock:
        waiting = waiting_paths()
        new = [(p, n, h) for p, n, h in rows if p not in prefetch["queued"] and p not in prefetch["active"] and p not in waiting]
        size = sum(n for _, n, _ in new)
        if size + sum(prefetch["queued"].values()) > capmb * 1e6 * 0.9:
            return {"error": f"{mb(size)} is more than the cache can hold ({mb(capmb * 1e6)}): nothing queued", "size": size}
        enqueue(new)
    dead = sum(1 for _, _, h in new if looks_dead(h))
    return {"queued": len(new), "size": size, "already": len(rows) - len(new), "no_peer": dead}


def waiting_paths():
    c = rdb()
    try:
        return {p for p, in c.execute("SELECT path FROM requests WHERE state='waiting'")}
    finally:
        c.close()


def prefetch_cancel(paths=None):
    """Cancel the given requests (queued, being read, waiting or failed), or every queued, running and waiting one
    when paths is None. A waiting request cancelled lets its torrent sleep."""
    with plock:
        waiting = waiting_paths()
        if paths is None:
            paths = list(prefetch["queue"]) + list(prefetch["active"]) + list(waiting)
        n = 0
        for p in paths:
            if p in waiting:
                n += 1
                continue
            if p in prefetch["queued"]:
                prefetch["queue"].remove(p); prefetch["queued"].pop(p); prefetch["hash"].pop(p, None)
            elif p in prefetch["active"]:
                prefetch["active"][p]["cancel"] = True
            elif not prefetch["failed"].pop(p, None):
                continue
            n += 1
        c = rdb()
        with c:
            c.executemany("UPDATE requests SET state='cancelled', updated=? WHERE path=? AND state IN ('queued', 'caching', 'waiting', 'failed')",
                          [(time.time(), p) for p in paths])
        c.close()
    waiter_wake.set()
    return {"cancelled": n}


def requests_clear():
    """Forget finished requests (done, cancelled, failed); queued, running and waiting ones stay."""
    c = rdb()
    with c:
        n = c.execute("DELETE FROM requests WHERE state IN ('done', 'cancelled', 'failed')").rowcount
    c.close()
    with plock:
        prefetch["failed"].clear()
    return {"cleared": n}


def prefetch_retry(paths=None):
    """Queue failed or cancelled requests again (all failed ones when paths is None). A waiting request is not
    queued again: its torrent is already kept awake for it."""
    if paths is None:
        with plock:
            paths = list(prefetch["failed"])
    return prefetch_add(paths=paths)


def prefetch_worker():
    while True:
        with plock:
            path = (prefetch["queue"].popleft() if prefetch["queue"] and state["state"] == "running"
                    and not load_settings().get("caching_stopped") else None)   # stopped: the queue waits
            if path:
                size = prefetch["queued"].pop(path, 0)
                job = prefetch["active"][path] = {"path": path, "size": size, "read": 0, "cancel": False,
                                                  "hash": prefetch["hash"].pop(path, ""), "since": time.time()}
        if not path:
            time.sleep(1); continue
        req_set(path, state="caching")
        ok, err, retriable = False, "", False
        refused = path.startswith("/") or ".." in path.split("/")   # a torrent can name its files anything: stay in the mount
        if refused:
            err, job["cancel"] = "refused: path leaves the share", True
        waiting = False
        for attempt in range(0 if job["cancel"] else 3):   # a read can fail during a restart: try again
            # a restart is not a failure: wait (up to 5 minutes) until the service runs and the mount answers again
            for _ in range(150):
                if job["cancel"] or (state["state"] == "running" and os.path.isdir(os.path.join(MOUNT, path.split("/", 1)[0]))):
                    break
                time.sleep(2)
            try:
                with open(os.path.join(MOUNT, path), "rb", buffering=0) as f:
                    f.seek(job["read"])
                    while not job["cancel"]:
                        b = f.read(1 << 20)
                        if not b:
                            break
                        job["read"] += len(b)
                ok = not job["cancel"]
                break
            except OSError as e:
                if e.errno == 5 and state["state"] == "running":   # Distribyted's answer to a read that got no data in time
                    err = (f"no data from peers within the read timeout ({os.environ.get('ATHENAFS_READ_TIMEOUT') or 60} s): "
                           f"no reachable seeder when asked ({health_text(job['hash'])})")
                    waiting = True
                    break   # no point reading again now: the torrent is kept awake and waits for a peer instead
                else:
                    err, retriable = str(e), e.errno != 2   # a file gone from the share will not come back by itself
                time.sleep(5)
        failed = refused or not ok and not job["cancel"] and not waiting
        waiting = waiting and not job["cancel"]
        with plock:
            prefetch["active"].pop(path, None)
            prefetch["bytes"] += job["read"]
            if ok:
                prefetch["done"] += 1
            elif failed:
                prefetch["failed"][path] = {"path": path, "error": err, "at": time.time()}
        try:
            if waiting:
                c = rdb()
                with c:
                    c.execute("UPDATE requests SET state='waiting', error=?, read=?, attempts=attempts+1, updated=?, "
                              "waiting_since=coalesce(waiting_since, ?) WHERE path=?", (err, job["read"], time.time(), time.time(), path))
                c.close()
                waiter_wake.set()
                continue
            if ok:
                req_set(path, state="done", read=job["read"], error="")
            elif failed:
                c = rdb()
                with c:
                    c.execute("UPDATE requests SET state='failed', error=?, retriable=?, read=?, attempts=attempts+1, updated=? "
                              "WHERE path=?", (err, int(retriable), job["read"], time.time(), path))
                c.close()
            else:
                req_set(path, state="cancelled", read=job["read"])
        except sqlite3.Error as e:
            print("cache requests:", e)


def prefetch_view():
    with plock:
        failed = sorted(prefetch["failed"].values(), key=lambda f: f["at"])
        return {"queued": len(prefetch["queue"]), "queued_size": sum(prefetch["queued"].values()),
                "active": [{k: v for k, v in a.items() if k != "cancel"} for a in prefetch["active"].values()],
                "done": prefetch["done"], "failed": len(failed), "failed_recent": failed[-3:], "bytes_read": prefetch["bytes"]}


def requests_of(paths):
    """{path: request} for the given share paths that have one."""
    out, paths = {}, list(paths)
    c = rdb()
    try:
        for k in range(0, len(paths), 500):
            chunk = paths[k:k + 500]
            for r in c.execute(f"SELECT path, state, created, updated, attempts, error, read, waiting_since, arrived FROM requests "
                               f"WHERE path IN ({','.join('?' * len(chunk))})", chunk):
                out[r[0]] = dict(zip(("path", "state", "created", "updated", "attempts", "error", "read", "waiting_since", "arrived"), r))
    finally:
        c.close()
    with plock:
        for p, r in out.items():
            if p in prefetch["active"]:
                r["read"] = prefetch["active"][p]["read"]
    return out


REQUEST_FILTERS = {"all": None, "open": ("queued", "caching"), "waiting": ("waiting",), "failed": ("failed",), "done": ("done",),
                   "cancelled": ("cancelled",)}


def requests_view(which="all", limit=50, offset=0, q=""):
    """Cache requests, most recently changed first (open ones: in queue order), with counts per state. q: words that
    must all appear in the path (case-insensitive)."""
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    if which not in REQUEST_FILTERS:
        return {"error": "state must be one of " + ", ".join(REQUEST_FILTERS)}
    states = REQUEST_FILTERS[which]
    conds = [f"state IN ({','.join('?' * len(states))})"] if states else []
    args = list(states or ())
    for w in q.split():
        conds.append("path LIKE ? ESCAPE '\\'")
        args.append("%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    where = "WHERE " + " AND ".join(conds) if conds else ""
    order = {"open": "CASE state WHEN 'caching' THEN 0 ELSE 1 END, seq", "waiting": "waiting_since"}.get(which, "updated DESC")
    c = rdb()
    try:
        counts = dict(c.execute("SELECT state, count(*) FROM requests GROUP BY state").fetchall())
        total = c.execute(f"SELECT count(*) FROM requests {where}", args).fetchone()[0]
        rows = c.execute(f"SELECT path, hash, size, state, created, updated, attempts, error, read, waiting_since, arrived "
                         f"FROM requests {where} ORDER BY {order} LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    finally:
        c.close()
    keys = ("path", "hash", "size", "state", "created", "updated", "attempts", "error", "read", "waiting_since", "arrived")
    out = []
    with plock:
        active = {p: dict(a) for p, a in prefetch["active"].items()}
    for r in rows:
        x = dict(zip(keys, r))
        if x["path"] in active:
            x["read"], x["since"] = active[x["path"]]["read"], active[x["path"]]["since"]
        if x["state"] == "waiting":
            w = awake["torrents"].get(x["hash"])   # how its torrent waits: kept awake, probed, or asleep until its next probe
            x["wait"] = {k: v for k, v in w.items() if k != "ranges"} if w else None
        x.update(health_text=health_text(x["hash"]), dead=looks_dead(x["hash"]), **locations(x["path"]))
        out.append(x)
    return {"state": which, "counts": {s: counts.get(s, 0) for s in ("queued", "caching", "waiting", "failed", "done", "cancelled")},
            "total": total, "offset": offset, "results": out}



# ---------------------------------------------------------------- waiting requests: torrents kept awake for them

WANTED_FILE = "/data/wanted.json"     # read by Distribyted every 30 s: torrents to hold awake and the pieces they need
PROBE_STEPS = (900, 1800, 3600, 7200)  # s between probes of a torrent left out of the budget: 15 min, 30, 1 h, then 2 h
PROBE_WINDOW = 600     # s a probe keeps its torrent awake (finding peers on the DHT and the trackers takes minutes)
PROBES_AT_ONCE = 2
SHED_PAUSE = 1500      # s between two torrents put to sleep above the ceiling: one sleeps after the idle delay (600 s),
                       # then Go returns its memory over about 10 min (measured 2026-10-23)
# what the waiter decided last, for the page, the API and plan_gentleman
awake = {"torrents": {}, "paths": set(), "listening_cost": 0, "rss": 0, "over": False, "calm": True, "last_shed": 0,
         "drop_gentle": None, "at": 0}
waiter_wake = threading.Event()


def distribyted_rss():
    p = state.get("proc")
    try:
        for line in open(f"/proc/{p.pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, AttributeError, ValueError):
        pass
    return 0


def awake_limits(st):
    """(budget for the torrents kept awake by choice, ceiling of Distribyted's real memory), in bytes."""
    return int(st["awake_mem_mb"]) * 1_000_000, int(st["memory_ceiling_mb"]) * 1_000_000


def merge_ranges(rs):
    out = []
    for b, e in sorted(rs):
        if out and b <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([b, e])
    return out


def plan_waiting(log=False):
    """Waiting requests whose pieces are all in the cache are done. The torrents of the others are kept awake with the
    pieces they need (listening) while their estimated memory fits the budget shared with gentleman mode, cheapest
    first (the most requests for the memory); the rest are probed: woken PROBE_WINDOW s with backoff, or at once when a
    health check or a scrape finds a seeder. Above the ceiling of Distribyted's real memory, nothing new is woken and,
    one at a time, a gentleman slot then the largest listening torrent is put back to sleep."""
    now = time.time(); st = load_settings(); budget, ceiling = awake_limits(st)
    c = rdb()
    reqs = c.execute("SELECT path, hash, waiting_since FROM requests WHERE state='waiting'").fetchall()
    probes = {h: (n, nx, lp) for h, n, nx, lp in c.execute("SELECT hash, probes, next_probe, last_probe FROM wait_torrents")}
    c.close()
    tp, cached = torrent_pieces(), set(cache_files())
    loc = {}
    d = db()
    try:
        for k in range(0, len(reqs), 500):
            chunk = [r[0] for r in reqs[k:k + 500]]
            for p, h, off, size in d.execute(f"SELECT path, hash, off, size FROM files WHERE path IN ({','.join('?' * len(chunk))})", chunk):
                loc[p] = (h, off, size)
    finally:
        d.close()
    need, done, gone = {}, [], []
    for p, h, since in reqs:
        t, l = tp.get(h), loc.get(p)
        if not t or not l:
            gone.append(p); continue
        pl = t["pl"]
        b, e = l[1] // pl, (l[1] + max(l[2], 1) - 1) // pl + 1
        missing = sum(1 for i in range(b, e) if piece(t, i) not in cached)
        if not missing:
            done.append(p); continue
        x = need.setdefault(h, {"ranges": [], "missing": 0, "since": since or now, "requests": 0})
        x["ranges"].append((b, e)); x["missing"] += missing; x["requests"] += 1; x["since"] = min(x["since"], since or now)
    if done or gone:
        c = rdb()
        with c:
            c.executemany("UPDATE requests SET state='done', arrived=?, updated=?, error='' WHERE path=? AND state='waiting'",
                          [(now, now, p) for p in done])
            c.executemany("UPDATE requests SET state='failed', error='no longer in the share', updated=? WHERE path=? AND state='waiting'",
                          [(now, p) for p in gone])
        c.close()
        with plock:
            for p in gone:
                prefetch["failed"][p] = {"path": p, "error": "no longer in the share", "at": now}
        if done:
            print(f"cache requests: {len(done)} waiting request(s) arrived")

    cost = {h: tp[h]["npieces"] * BYTES_PER_AWAKE_PIECE for h in need}
    rss = distribyted_rss()
    over, calm = rss > ceiling, rss < 0.9 * ceiling   # between the two: keep what is awake, wake nothing new
    prev = awake["torrents"]
    was_listening = {h for h, x in prev.items() if x["mode"] == "listening"}
    if over and now - awake["last_shed"] > SHED_PAUSE:
        gs = load_gentle_state().get("slots", {})
        if gs:   # gentleman slots go first
            awake["drop_gentle"] = max(gs, key=lambda k: gs[k]["since"])
            print(f"memory: Distribyted uses {mb(rss)} (ceiling {mb(ceiling)}): gentleman slot {awake['drop_gentle'][:8]} released")
        elif was_listening:
            h = max(was_listening, key=lambda k: cost.get(k, 0))
            was_listening.discard(h)
            print(f"memory: Distribyted uses {mb(rss)} (ceiling {mb(ceiling)}): {h[:8]} stops listening, probed instead")
        awake["last_shed"] = now
    stopped = bool(st.get("caching_stopped"))   # caching stopped: no torrent is held or probed, requests keep waiting
    listening, left, room = [], budget, 0.9 * ceiling - rss
    for h in sorted(need, key=lambda h: (h not in was_listening, cost[h], need[h]["since"])):
        if cost[h] > left or stopped:
            continue
        if h in was_listening:
            listening.append(h); left -= cost[h]
        elif calm and cost[h] <= room:
            listening.append(h); left -= cost[h]; room -= cost[h]
    # a probe runs until its last_probe time (kept in the table, so a restart does not cut it short)
    probing = [] if stopped else [h for h in need if h not in listening and probes.get(h, (0, 0, 0))[2] > now][:PROBES_AT_ONCE]
    sw = swarm_info()
    newp = {}
    for h in sorted(need, key=lambda h: probes.get(h, (0, 0, 0))[1]):
        if h in listening or h in probing:
            continue
        n, nx, lp = probes.get(h, (0, need[h]["since"] + PROBE_STEPS[0], 0))
        hl, s_ = health.get(h) or {}, sw.get(h) or {}
        signal = (hl.get("seeders") and hl.get("checked", 0) > lp) or (s_.get("seeders") and s_.get("checked", 0) > lp)
        if (now >= nx or signal) and len(probing) < PROBES_AT_ONCE and calm and cost[h] <= room and not stopped:
            probing.append(h); room -= cost[h]
            n += 1; lp = now + PROBE_WINDOW; nx = lp + PROBE_STEPS[min(n, len(PROBE_STEPS) - 1)]
            print(f"cache requests: probing {h[:8]} for {PROBE_WINDOW // 60} min ({'a seeder was reported' if signal else f'probe {n}'})")
        newp[h] = (n, nx, lp)
    c = rdb()
    with c:
        c.execute(f"DELETE FROM wait_torrents WHERE hash NOT IN ({','.join('?' * len(need))})", list(need))
        c.executemany("INSERT OR REPLACE INTO wait_torrents VALUES (?,?,?,?)", [(h, *v) for h, v in newp.items()])
    c.close()

    peers = {x["hash"]: x for x in activity.get("torrents") or []}
    # data received, from the torrent's cumulative counter between two runs (the instant rate is bursty); the counter
    # starts again from 0 when the torrent is woken again
    rx = awake.setdefault("rx", {})   # hash -> (time, bytes downloaded, last time bytes arrived)
    torrents = {}
    for h, x in need.items():
        n, nx, lp = newp.get(h, probes.get(h, (0, 0, 0)))
        mode = "listening" if h in listening else "probing" if h in probing else "sleeping"
        got = peers.get(h, {}).get("downloaded")
        t0, d0, last = rx.get(h, (now, got, 0))
        rx_rate = (got - d0) / (now - t0) if got is not None and d0 is not None and got >= d0 and now > t0 else 0
        if rx_rate > 0:
            last = now
        rx[h] = (now, got, last)
        torrents[h] = {"hash": h, "name": tp[h]["name"], "mode": mode, "requests": x["requests"], "missing_pieces": x["missing"],
                       "waiting_since": x["since"], "memory_estimate": cost[h], "probes": n, "next_probe": nx if mode == "sleeping" else None,
                       "until": lp if mode == "probing" else None,
                       "peers": peers.get(h, {}).get("peers", 0), "seeders": peers.get(h, {}).get("seeders", 0),
                       "pieces_needed": sum(e - b for b, e in merge_ranges(x["ranges"])), "down_rate": rx_rate,
                       "last_received": last or None, "ranges": merge_ranges(x["ranges"])}
    with open(WANTED_FILE + ".tmp", "w") as f:
        json.dump({"torrents": [{"hash": h, "pieces": torrents[h]["ranges"]} for h in listening + probing]}, f)
    os.replace(WANTED_FILE + ".tmp", WANTED_FILE)
    listening_cost = sum(cost[h] for h in listening)
    changed = listening_cost != awake["listening_cost"] or calm != awake["calm"] or awake["drop_gentle"] or \
        set(need) != set(prev)
    for h in list(rx):
        if h not in need:
            rx.pop(h)
    with plock:
        paths = {p for p, h, _ in reqs if h in need and p not in done}
    awake.update(torrents=torrents, paths=paths, listening_cost=listening_cost, rss=rss, over=over, calm=calm, at=now,
                 budget=budget, ceiling=ceiling)
    if changed:
        plan_gentleman()   # its budget is what the waiting torrents leave
        awake["drop_gentle"] = None
    if log:
        gs = load_gentle_state().get("slots", {})
        nawake = sum(1 for x in (served.get("data") or {}).get("sources", []) if x.get("awake"))
        c = rdb()
        with c:
            c.executemany("INSERT INTO wait_log VALUES (?,?,?,?,?,?)",
                          [(now, h, x["mode"], x["peers"], x["seeders"], x["missing_pieces"]) for h, x in torrents.items()])
            c.execute("INSERT INTO mem_log VALUES (?,?,?,?,?,?,?)", (now, rss, nawake, len(listening), listening_cost, len(probing), len(gs)))
        c.close()


def waiter():
    last_log = 0
    time.sleep(60)   # let the start-up settle
    while True:
        try:
            if state["state"] == "running":
                lg = time.time() - last_log >= 300
                plan_waiting(log=lg)
                if lg:
                    last_log = time.time()
        except Exception as e:
            print("waiter:", e)
        waiter_wake.wait(30); waiter_wake.clear()


def awake_view():
    """The torrents kept awake by choice: waiting requests (listening or probed) and gentleman slots, with the budget,
    the ceiling and Distribyted's memory now and over the last 24 h."""
    c = rdb()
    try:
        mem = c.execute("SELECT at, rss, awake, listening, listening_est, probing, gentleman FROM mem_log WHERE at > ? ORDER BY at",
                        (time.time() - 86400,)).fetchall()
    finally:
        c.close()
    g = gentleman_view()
    return {"rss": awake["rss"], "budget": awake.get("budget", 0), "ceiling": awake.get("ceiling", 0), "over_ceiling": awake["over"],
            "waking_allowed": awake["calm"], "listening_estimate": awake["listening_cost"],
            "gentleman_estimate": sum(x["memory_estimate"] for x in g["helped"]),
            "waiting": [{k: v for k, v in x.items() if k != "ranges"} for x in sorted(awake["torrents"].values(), key=lambda x: x["waiting_since"])],
            "probe_steps": PROBE_STEPS, "probe_window": PROBE_WINDOW,
            "memory_log": [dict(zip(("at", "rss", "awake", "listening", "listening_estimate", "probing", "gentleman"), r)) for r in mem]}


# ---------------------------------------------------------------- per folder: ready view, visibility in the share

FOLDER_SETTINGS = "/data/folder_settings.json"   # {folder: {"view": ready view name or absent, "hidden": bool}}
OLD_ARCHIVE_VIEW_SETTINGS = "/data/archive_view_settings.json"   # first prototype, migrated
READY_VIEWS = os.environ.get("DISTRIBYTED_READY_VIEWS", "/data/ready-views.json")   # the list Distribyted reads
HIDDEN_FOLDERS = "/data/smb/hidden-folders.txt"   # read by the Samba container: folders hidden from the share
hidden_folders = set()   # current hidden folders (locations() gives no SMB address for them)


def view_default(folder):
    return sanitize(f"{folder}_READY")


def load_folder_settings():
    try:
        st = json.load(open(FOLDER_SETTINGS))
    except (OSError, ValueError):
        st = {}
        try:   # the prototype's archive views become ready views; the old default name moves to the new default
            for r, v in json.load(open(OLD_ARCHIVE_VIEW_SETTINGS)).items():
                name = v.get("name", "")
                st[r] = {"view": view_default(r) if name == sanitize(f"{r}_ZIP") else name}
            save_folder_settings(st)
            os.remove(OLD_ARCHIVE_VIEW_SETTINGS)
        except (OSError, ValueError):
            pass
    hidden_folders.clear(); hidden_folders.update(r for r, v in st.items() if v.get("hidden"))
    return st


def save_folder_settings(st):
    st = {r: v for r, v in st.items() if v.get("view") or v.get("hidden")}
    with open(FOLDER_SETTINGS + ".tmp", "w") as f:
        json.dump(st, f, indent=1)
    os.replace(FOLDER_SETTINGS + ".tmp", FOLDER_SETTINGS)
    hidden_folders.clear(); hidden_folders.update(r for r, v in st.items() if v.get("hidden"))


def ready_views(st=None):
    st = load_folder_settings() if st is None else st
    return {r: v["view"] for r, v in st.items() if v.get("view")}


def complete_files(views):
    """{folder: [file paths inside the folder]} for the folders with a ready view: the files whose every piece is in the
    cache. Starts from the cached pieces, so the work follows what is cached, not the size of the catalogue."""
    out = {r: [] for r in views}
    if not views:
        return out
    files = cache_files(); cached = set(files); tp = torrent_pieces(); sources = load_sources()
    flat = flat_folders(tp, sources)
    route_of = {source_hash(x): x["route"] for x in sources}
    cand = set()
    for h, owners in piece_owners(files).items():
        for ih, i in owners:
            t = tp.get(ih)
            if t and route_of.get(ih) in views:
                cand.update((ih, k) for k, _ in files_of_piece(t, i, files[h]))
    for ih, k in cand:
        t = tp[ih]; fp, o, n = t["files"][k]
        if n <= 0 or is_padding(fp):
            continue
        pl = t["pl"]
        if all(piece(t, i) in cached for i in range(o // pl, (o + n - 1) // pl + 1)):
            r = route_of[ih]
            out[r].append(share_path(r, t, fp, flat.get(r))[len(r) + 1:])
    return out


# ---------------------------------------------------------------- every file completely in the cache (/api/cached)

# Kept up to date every 30 s from the pieces that arrived in or left the cache since the last pass (a full pass at
# start and when torrents are added, removed or moved: share paths change). generation grows when the list changes.
complete = {"files": {}, "generation": 0, "changed": 0, "checked": 0, "ready": False}
_complete_state = {"owners": {}, "sizes": {}, "key": None}
clock = threading.Lock()


def complete_pass():
    st = _complete_state
    tp = torrent_pieces(); sources = load_sources()
    key = tuple(sorted((source_hash(x) or "", x["route"]) for x in sources)) + (len(tp),)
    files = cache_files()
    full = key != st["key"]
    if full:
        st["owners"], st["sizes"], st["key"] = {}, {}, key
    added = files.keys() - st["sizes"].keys(); removed = st["sizes"].keys() - files.keys()
    if added:
        st["owners"].update(piece_owners(added))
    flat = flat_folders(tp, sources)
    route_of = {source_hash(x): x["route"] for x in sources}
    touched = set()
    for h in added | removed:
        size = files.get(h) or st["sizes"].get(h, 0)
        for ih, i in st["owners"].get(h, ()):
            t = tp.get(ih)
            if t:
                touched.update((ih, k) for k, _ in files_of_piece(t, i, size))
    for h in removed:
        st["owners"].pop(h, None); st["sizes"].pop(h, None)
    for h in added:
        st["sizes"][h] = files[h]
    cached = set(files)
    out = {} if full else dict(complete["files"])
    for ih, k in touched:
        t = tp[ih]; fp, o, n = t["files"][k]
        r = route_of.get(ih)
        if n <= 0 or is_padding(fp) or not r:
            continue
        path = share_path(r, t, fp, flat.get(r))
        pl = t["pl"]
        if all(piece(t, i) in cached for i in range(o // pl, (o + n - 1) // pl + 1)):
            out[path] = {"path": path, "size": n, "hash": ih, "folder": r, "torrent_path": fp, "off": o}
        else:
            out.pop(path, None)
    now = time.time()
    with clock:
        if out.keys() != complete["files"].keys() or not complete["ready"]:
            complete["generation"] += 1; complete["changed"] = now
        complete.update(files=out, checked=now, ready=True)


def complete_indexer():
    while not pieces_ready["done"]:   # the piece table is built by the indexer at start
        time.sleep(5)
    while True:
        try:
            complete_pass()
        except Exception as e:
            print("complete files:", e)
        time.sleep(30)


def cached_view(folder="", fmt="json"):
    """Every file completely in the cache (all of it, no page), sorted by path, optionally only under a folder of the
    share. Hidden folders are included, marked."""
    prefix = folder.strip("/")
    with clock:
        files, gen, changed, checked = complete["files"], complete["generation"], complete["changed"], complete["checked"]
    rows = sorted((f for p, f in files.items() if not prefix or p == prefix or p.startswith(prefix + "/")), key=lambda f: f["path"])
    hidden = set(hidden_folders)
    items = [{"path": f["path"], "size": f["size"], "hash": f["hash"], "folder": f["folder"], "hidden": f["folder"] in hidden}
             for f in rows]
    head = {"generation": gen, "changed": changed, "checked": checked, "folder": prefix, "total": len(items),
            "total_size": sum(f["size"] for f in items)}
    return head, items


ARCHIVE_EXT = (".zip", ".7z", ".rar")
_written = {"views": None, "hidden": None, "lists": {}}


def write_ready_views():
    """Write the list of complete files per ready view for Distribyted (it rereads it every 30 s); only when it changed."""
    views = ready_views()
    lists = complete_files(views)
    text = json.dumps({"views": [{"name": name, "route": r, "files": sorted(lists.get(r, []))} for r, name in views.items()]},
                      indent=1)
    if text != _written["views"]:
        with open(READY_VIEWS + ".tmp", "w") as f:
            f.write(text)
        os.replace(READY_VIEWS + ".tmp", READY_VIEWS)
        _written["views"] = text
    _written["lists"] = lists
    return lists


def write_hidden_folders():
    """Folders hidden from the share, one per line, for the Samba container (it reloads its configuration on change).
    They stay in the mount: search, Cache and the ready view keep working."""
    load_folder_settings()
    text = "".join(f"{r}\n" for r in sorted(hidden_folders))
    if text != _written["hidden"]:
        os.makedirs(os.path.dirname(HIDDEN_FOLDERS), exist_ok=True)
        with open(HIDDEN_FOLDERS + ".tmp", "w") as f:
            f.write(text)
        os.replace(HIDDEN_FOLDERS + ".tmp", HIDDEN_FOLDERS)
        _written["hidden"] = text


def ready_view_writer():
    while True:
        time.sleep(30)
        try:
            write_ready_views()
        except Exception as e:
            print("ready views:", e)


def folder_settings_status():
    st = load_folder_settings()
    lists = complete_files(ready_views(st))
    routes = sorted({x["route"] for x in load_sources()})
    out = []
    for r in routes:
        v = st.get(r, {})
        files = lists.get(r, [])
        row = {"folder": r, "hidden": bool(v.get("hidden")), "view": bool(v.get("view")),
               "view_name": v.get("view") or view_default(r), "complete_files": len(files),
               "complete_archives": sum(1 for f in files if f.lower().endswith(ARCHIVE_EXT))}
        if v.get("view"):
            row["view_location"] = locations(v["view"], True)
        out.append(row)
    return {"folders": out}


def set_ready_view(folder, on, name=""):
    """Turn the ready view of a folder on or off; restarts the service (views are created at start-up)."""
    routes = {x["route"] for x in load_sources()}
    if folder not in routes:
        return {"error": "unknown folder"}
    st = load_folder_settings()
    cur = st.get(folder, {})
    if not on:
        if not cur.get("view"):
            return {"error": "no ready view for that folder"}
        cur.pop("view")
    else:
        n = sanitize(name or cur.get("view") or view_default(folder))
        taken = routes | {v["view"] for r, v in st.items() if r != folder and v.get("view")}
        if n in taken:
            return {"error": f"the name {n} is already a folder or a view"}
        cur["view"] = n
    st[folder] = cur

    def job():
        save_folder_settings(st)
        return f"ready view of {folder}: " + (f"on, as {cur['view']}" if on else "off")
    threading.Thread(target=exclusive, args=(job,), daemon=True).start()
    return {"ok": True, "restarting": True}


def set_hidden(folder, hidden):
    """Show or hide a folder in the share (Samba only: no restart, a few seconds)."""
    if folder not in {x["route"] for x in load_sources()}:
        return {"error": "unknown folder"}
    st = load_folder_settings()
    st.setdefault(folder, {})["hidden"] = bool(hidden)
    save_folder_settings(st)
    write_hidden_folders()
    state["message"] = f"{folder}: {'hidden from' if hidden else 'shown in'} the share"
    return {"ok": True, "hidden": bool(hidden)}


# ---------------------------------------------------------------- gentleman mode: help torrents in difficulty
#
# Every 6 h the trackers are asked (scrape) how many seeders and leechers each torrent has. Torrents with 1 or 2 seeders
# and leechers waiting get a slot (ATHENAFS_GENTLEMAN_SLOTS, 3): Distribyted downloads their rarest pieces into a cache
# of its own (ATHENAFS_GENTLEMAN_SPACE_PCT of the cache cap), separate from normal use, and seeds them under the upload
# cap. A torrent leaves its slot when it has 3 seeders again or after 24 h, and waits 24 h before it can come back.

GENTLEMAN_FILE = "/data/gentleman.json"          # read by Distribyted every minute: the torrents to help, the budget
GENTLEMAN_STATE = "/data/gentleman_state.json"   # {hash: {"since": t}} current slots, {"released": {hash: t}}
GENTLEMAN_CACHE = "/data/metadata/gentleman-cache"
SCRAPE_EVERY = 6 * 3600
BYTES_PER_AWAKE_PIECE = 3500   # measured: 3 helped torrents of 753,000 pieces in all added about 2.6 GB to Distribyted


def gentle_space_bytes(st):
    return max(0, effective_cap_mb(st) * 1e6 * float(st["gentle_pct"]) / 100) if st["gentle_on"] else 0


def scrape_udp(url, hashes, timeout=5):
    """BEP 15 scrape: {info-hash: (seeders, completed, leechers)} for up to 74 hashes."""
    u = urllib.parse.urlparse(url)
    addr = (u.hostname, u.port or 80)
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.settimeout(timeout)
    try:
        tid = secrets.randbits(32)
        sk.sendto(struct.pack(">QII", 0x41727101980, 0, tid), addr)
        data, _ = sk.recvfrom(2048)
        action, rtid, conn = struct.unpack(">IIQ", data[:16])
        if action != 0 or rtid != tid:
            return {}
        tid = secrets.randbits(32)
        sk.sendto(struct.pack(">QII", conn, 2, tid) + b"".join(bytes.fromhex(h) for h in hashes), addr)
        data, _ = sk.recvfrom(8 + 12 * len(hashes) + 64)
        action, rtid = struct.unpack(">II", data[:8])
        if action != 2 or rtid != tid:
            return {}
        out = {}
        for i, h in enumerate(hashes):
            chunk = data[8 + 12 * i:20 + 12 * i]
            if len(chunk) == 12:
                out[h] = struct.unpack(">III", chunk)
        return out
    finally:
        sk.close()


def scrape_http(url, hashes, timeout=10):
    """HTTP scrape (the announce URL with "announce" replaced by "scrape")."""
    head, _, last = url.rpartition("/")
    if not last.startswith("announce"):
        return {}
    q = "&".join("info_hash=" + urllib.parse.quote(bytes.fromhex(h), safe="") for h in hashes)
    target = f"{head}/{last.replace('announce', 'scrape', 1)}{'&' if '?' in last else '?'}{q}"
    check_public_url(target)
    with urllib.request.urlopen(urllib.request.Request(target, headers={"User-Agent": "athenafs"}), timeout=timeout) as r:
        d, _ = bdec(r.read(1 << 20))
    out = {}
    for k, v in (d.get(b"files") or {}).items():
        if isinstance(k, bytes) and len(k) == 20 and isinstance(v, dict):
            out[k.hex()] = (v.get(b"complete", 0), v.get(b"downloaded", 0), v.get(b"incomplete", 0))
    return out


def scrape_all():
    """Ask the trackers about every known torrent; keep the best answer per torrent (most seeders, then leechers)."""
    tp = torrent_pieces()
    hashes = {source_hash(x) for x in load_sources()} & set(tp)
    by_tracker = {}
    for h in hashes:
        for t in tp[h].get("trackers", [])[:6]:
            by_tracker.setdefault(t, []).append(h)
    best, dead = {}, set()
    for tr, hs in sorted(by_tracker.items(), key=lambda x: -len(x[1])):
        if not tr.startswith(("udp://", "http://", "https://")):
            continue
        for k in range(0, len(hs), 70):
            if tr in dead:
                break
            try:
                res = (scrape_udp if tr.startswith("udp://") else scrape_http)(tr, hs[k:k + 70])
            except (OSError, ValueError, KeyError, struct.error):
                dead.add(tr); continue
            for h, (sd, cp, lc) in res.items():
                if h in hashes and (sd, lc) > best.get(h, (-1, -1))[:2]:
                    best[h] = (sd, lc, cp)
    now = int(time.time())
    c = db()
    with c:
        c.execute("CREATE TABLE IF NOT EXISTS swarm(hash TEXT PRIMARY KEY, seeders INTEGER, leechers INTEGER, completed INTEGER, checked INTEGER)")
        c.executemany("INSERT OR REPLACE INTO swarm VALUES (?,?,?,?,?)", [(h, sd, lc, cp, now) for h, (sd, lc, cp) in best.items()])
    c.close()
    print(f"scrape: {len(best)} of {len(hashes)} torrents answered, {len(by_tracker) - len(dead)} trackers up")
    return best


def swarm_info():
    try:
        c = db()
        rows = c.execute("SELECT hash, seeders, leechers, completed, checked FROM swarm").fetchall()
        c.close()
    except sqlite3.Error:
        return {}
    return {h: {"seeders": sd, "leechers": lc, "completed": cp, "checked": ch} for h, sd, lc, cp, ch in rows}


def load_gentle_state():
    try:
        return json.load(open(GENTLEMAN_STATE))
    except (OSError, ValueError):
        return {"slots": {}, "released": {}}


def gentle_memory(st):
    """Memory left to gentleman mode: the shared budget minus what waiting cache requests keep awake."""
    return max(0, awake_limits(st)[0] - awake["listening_cost"])


def slots_over_memory(slots, st, h):
    """True for the slots that no longer fit the memory left to gentleman mode: the most recent ones go first."""
    tp = torrent_pieces()
    total = 0
    for k in sorted(slots, key=lambda k: slots[k]["since"]):
        total += tp[k]["npieces"] * BYTES_PER_AWAKE_PIECE if k in tp else 0
        if k == h:
            return total > gentle_memory(st)
    return False


gentle_lock = threading.Lock()


def plan_gentleman():
    with gentle_lock:   # called by the gentleman loop and by the waiter
        return plan_gentleman_locked()


def plan_gentleman_locked():
    """Choose the torrents to help and write the list Distribyted reads. Torrents of waiting cache requests are left
    to the waiter (they need the normal cache); above the memory ceiling no slot is added, and the waiter names one to drop."""
    st = load_settings(); gs = load_gentle_state(); sw = swarm_info(); now = time.time()
    slots = gs.get("slots", {}); released = {h: t for h, t in gs.get("released", {}).items() if now - t < 86400}
    known = {source_hash(x) for x in load_sources()}
    for h in list(slots):   # leave the slot: enough seeders again, 24 h passed, or the torrent is gone
        x = sw.get(h, {})
        if (h not in known or x.get("seeders", 0) >= 3 or now - slots[h]["since"] > 86400 or not st["gentle_on"]
                or st.get("caching_stopped")
                or slots_over_memory(slots, st, h) or h in awake["torrents"] or h == awake["drop_gentle"]):
            slots.pop(h); released[h] = now
    for h in sorted(slots, key=lambda k: slots[k]["since"])[int(st["gentle_slots"]):]:   # the maximum was lowered:
        slots.pop(h); released[h] = now                                               # the most recent ones leave
    if st["gentle_on"] and awake["calm"] and not st.get("caching_stopped"):
        cands = sorted((h for h, x in sw.items() if h in known and h not in slots and h not in released and h not in awake["torrents"]
                        and 1 <= x["seeders"] <= 2 and x["leechers"] >= 1),
                       key=lambda h: -sw[h]["leechers"] / sw[h]["seeders"])
        # memory budget: an awake torrent costs about BYTES_PER_AWAKE_PIECE per piece (a 30 TB torrent of 475,000 pieces:
        # 1.6 GB); take candidates in order while they fit, skip the ones too large for what is left
        tp = torrent_pieces()
        cost = lambda h: tp[h]["npieces"] * BYTES_PER_AWAKE_PIECE if h in tp else 0
        left = gentle_memory(st) - sum(cost(h) for h in slots)
        for h in cands:
            if len(slots) >= int(st["gentle_slots"]):
                break
            if cost(h) <= left:
                slots[h] = {"since": now}; left -= cost(h)
    gs = {"slots": slots, "released": released}
    with open(GENTLEMAN_STATE + ".tmp", "w") as f:
        json.dump(gs, f)
    os.replace(GENTLEMAN_STATE + ".tmp", GENTLEMAN_STATE)
    space = gentle_space_bytes(st)
    per = int(space / max(1, len(slots))) if slots else 0
    with open(GENTLEMAN_FILE + ".tmp", "w") as f:
        json.dump({"torrents": [{"hash": h, "budget": per} for h in slots]}, f)
    os.replace(GENTLEMAN_FILE + ".tmp", GENTLEMAN_FILE)
    return gs


def gentleman_loop():
    last_scrape = 0
    try:
        last_scrape = max((x["checked"] for x in swarm_info().values()), default=0)
    except Exception:
        pass
    time.sleep(120)   # let the start-up settle
    while True:
        try:
            if time.time() - last_scrape > SCRAPE_EVERY:
                scrape_all(); last_scrape = time.time()
            plan_gentleman()
        except Exception as e:
            print("gentleman:", e)
        time.sleep(600)


def apply_gentleman(on, slots, pct, upload_kbps):
    """Validate and apply the gentleman settings and the upload cap (restarts the service)."""
    try:
        slots, pct, up = int(slots), float(pct), int(float(upload_kbps))
    except ValueError:
        state["message"] = "invalid number: settings unchanged"; return
    if not (0 <= slots <= 20 and 0 <= pct <= 50 and 0 <= up <= 1_000_000):
        state["message"] = "slots 0-20, space 0-50 %, upload 0-1000000 kB/s: settings unchanged"; return
    new = dict(load_settings(), gentle_on=bool(on), gentle_slots=slots, gentle_pct=pct, upload_kbps=up)

    def job():
        save_settings(new)
        plan_gentleman()
        return (f"gentleman mode {'on' if on else 'off'} ({slots} slot(s), {pct:g} % of the cache cap); "
                f"upload cap {'none' if not up else str(up) + ' kB/s'}")
    exclusive(job)


NO_DOWNLOAD_FILE = "/data/no-download"   # exists while caching is stopped: Distribyted then downloads nothing


def caching_stopped():
    return bool(load_settings().get("caching_stopped"))


def write_no_download(st):
    if st.get("caching_stopped"):
        with open(NO_DOWNLOAD_FILE, "w") as f:
            f.write("caching stopped from the page\n")
    else:
        try:
            os.remove(NO_DOWNLOAD_FILE)
        except FileNotFoundError:
            pass


def set_caching(stopped):
    """Stop caching (nothing new is downloaded: reads of what is not cached fail quickly, Cache requests are refused,
    queued and waiting ones pause, gentleman mode takes no slot) or resume it. No restart: Distribyted checks every 5 s."""
    st = dict(load_settings(), caching_stopped=bool(stopped))
    save_settings(st)
    write_no_download(st)
    waiter_wake.set()
    try:
        plan_gentleman()
    except Exception as e:
        print("gentleman:", e)
    state["message"] = ("caching stopped: nothing new is downloaded; what is cached stays available" if stopped
                        else "caching resumed")
    return {"caching_stopped": bool(stopped)}


def apply_awake(mem_mb, ceiling_mb):
    """Memory budget of the torrents kept awake and ceiling of Distribyted's memory: no restart, the waiter applies them."""
    try:
        mem, ceil = int(float(mem_mb)), int(float(ceiling_mb))
    except ValueError:
        state["message"] = "invalid number: settings unchanged"; return
    if not (0 <= mem <= 64000 and 1000 <= ceil <= 256000):
        state["message"] = "budget 0-64000 MB, ceiling 1000-256000 MB: settings unchanged"; return
    save_settings(dict(load_settings(), awake_mem_mb=mem, memory_ceiling_mb=ceil))
    state["message"] = (f"torrents kept awake: {mem} MB estimated at most; Distribyted's memory ceiling: {ceil} MB "
                        "(Go's own memory limit follows it at the next restart)")
    waiter_wake.set()


def memory_tile():
    """Distribyted's memory against the ceiling, and what is kept awake by choice against the shared budget."""
    st = load_settings(); budget, ceiling = awake_limits(st)
    rss, wait = awake["rss"] or distribyted_rss(), awake["listening_cost"]
    gent = sum(x["memory_estimate"] for x in gentleman_view()["helped"])
    used = min(rss, ceiling)
    col = "var(--warn)" if rss > 0.9 * ceiling else "var(--acc)"
    pie = donut([(used, col), (max(ceiling - used, 0), "var(--chip)")], f"Distribyted {mb(rss)} of a {mb(ceiling)} ceiling")
    state_ = ("over the ceiling: putting torrents to sleep" if rss > ceiling
              else "near the ceiling: nothing new is woken" if rss > 0.9 * ceiling else f"of a {mb(ceiling)} ceiling")
    why = (f"Distribyted's memory. Torrents kept awake by choice (estimated): waiting cache requests {mb(wait)}, "
           f"gentleman mode {mb(gent)}, budget {mb(budget)}. Settings to change both.")
    return (f"<div class=\"tile pie\" title=\"{html.escape(why, quote=True)}\"><div><span>Memory</span><b>{mb(rss)}</b>"
            f"<span>{state_} · awake by choice {mb(wait + gent)} of {mb(budget)}</span></div>{pie}</div>")


def caching_pill():
    """Header: the Stop caching button, or, while stopped, a badge and Resume caching."""
    form = '<form method=post action=/caching style="display:inline;margin-left:8px"><input type=hidden name=stopped value={v}>{b}</form>'
    if caching_stopped():
        return ('<span class="pill warn" title="Nothing new is downloaded: what is cached stays available over SMB, the rest fails '
                'at once; Cache requests are refused, queued and waiting ones pause.">caching stopped</span>'
                + form.format(v=0, b="<button class=primary>Resume caching</button>"))
    return form.format(v=1, b='<button data-confirm="stop caching? Nothing new will be downloaded until you resume; what is cached '
                             'stays available">Stop caching</button>')


def request_badges():
    """(requests waiting, waiting requests that arrived in the last 24 h) for the header."""
    try:
        c = rdb()
        try:
            return (c.execute("SELECT count(*) FROM requests WHERE state='waiting'").fetchone()[0],
                    c.execute("SELECT count(*) FROM requests WHERE state='done' AND arrived > ?", (time.time() - 86400,)).fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return 0, 0


def activity_view():
    now = time.time()
    return {"down": activity["down"], "up": activity["up"], "net_rx": activity["net_rx"], "net_tx": activity["net_tx"],
            "window_s": WINDOW,
            "torrents": [x for x in activity["torrents"] if x["down"] > 0 or x["up"] > 0],
            "files": [dict(f, rate=f["bytes"] / WINDOW, ago=round(now - f["last"])) for f in activity["files"]],
            "prefetch": prefetch_view()}


served = {"at": 0, "json": b"", "html": b""}   # last snapshot, as served to the page and /api


def snapshotter():
    """Computes the status and the page every few seconds, so a request never waits for it (with hundreds of torrents
    a snapshot takes seconds, and Distribyted's API can be slow while torrents are being added)."""
    while True:
        try:
            s = snapshot()
            served.update(at=time.time(), data=s, json=json.dumps(s).encode(), html=render(s).encode())
        except Exception as e:
            print("snapshot error:", e)
        time.sleep(3)


def watchdog():
    """Distribyted can die on its own (a crash, a kill): report it and restart it, unless it keeps dying."""
    crashes = []
    while True:
        time.sleep(5)
        p = state.get("proc")
        if not p or p.poll() is None or not lock.acquire(blocking=False):
            continue
        try:
            if state.get("proc") is not p:
                continue   # replaced meanwhile by a change from the page
            crashes = [t for t in crashes if t > time.time() - 600] + [time.time()]
            stop()
            if len(crashes) >= 3:
                state["state"] = f"stopped: Distribyted exited (code {p.returncode}) 3 times in 10 minutes; see the container log"
                continue
            state["message"] = f"Distribyted exited on its own (code {p.returncode}): restarted"
            start()
        finally:
            lock.release()


def main():
    load_health()
    load_requests()
    start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=indexer, daemon=True).start()
    threading.Thread(target=snapshotter, daemon=True).start()
    threading.Thread(target=ready_view_writer, daemon=True).start()
    threading.Thread(target=gentleman_loop, daemon=True).start()
    for _ in range(PREFETCH_WORKERS):
        threading.Thread(target=prefetch_worker, daemon=True).start()
    threading.Thread(target=waiter, daemon=True).start()
    threading.Thread(target=complete_indexer, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H)
    signal.signal(signal.SIGTERM, lambda *a: (stop(), os._exit(0)))
    srv.serve_forever()


main()
