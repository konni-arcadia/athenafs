#!/usr/bin/env python3
"""Distribyted supervisor + status and management page.

Why a supervisor: changing what Distribyted serves (adding or removing a torrent) or erasing cached pieces while it runs
breaks reads (it still believes the pieces are complete, `unexpected EOF`). So every change is made with the service
stopped: stop, apply the change, restart (~10 s). The pieces of a torrent are found through their hashes, which are listed
in its .torrent file (for a magnet, in the metadata that the patched Distribyted saves once it has been resolved).

The torrents served are kept in /data/sources.json; the Distribyted configuration actually used is generated from the base
configuration (/config/config.yaml, everything above `routes:`) plus those sources.
"""
import base64, bisect, collections, functools, hashlib, sqlite3, hmac, html, http.server, ipaddress, json, os, re, secrets, shutil, signal, socket, subprocess
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
    return {"hash": ih, "name": info[b"name"].decode("utf-8", "replace"), "total": total, "multi": b"files" in info,
            "pieces": p, "npieces": len(p) // 20, "pl": info[b"piece length"], "files": fl}


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
        routes = {}   # one route per folder, holding every source of that folder (insertion order kept)
        for s in sources:
            key, value = ("magnet_uri" if s["kind"] == "magnet" else "torrent_path"), s["value"]
            if s["kind"] == "magnet" and (saved := saved_metainfo(magnet_hash(value))):
                key, value = "torrent_path", saved
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
    return "0" if n < 1e3 else f"{n / 1e3:.0f} kB/s" if n < 1e6 else f"{n / 1e6:.1f} MB/s"


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
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:0 0 14px}}
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
a{{color:var(--acc)}}a.seg{{color:inherit;text-decoration:none;border-bottom:1px dotted var(--mut)}}a.seg:hover{{color:var(--acc)}}
.pill.mut{{color:var(--mut)}}.warn{{color:var(--warn)}}.brk{{word-break:break-word}}#sr td.small{{word-break:break-word}}#sr td button{{padding:1px 8px;font-size:12px}}
</style>
<div class=page>
<header><h1>athenafs</h1><span id=pill class="pill {pillcls}">{state}</span></header>
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
<div class="mut small" style="margin-top:4px">Click a folder in a result to list everything under it (<code>folder:path</code>); "Cache" reads files into the cache.</div>
<div id=srmsg style="margin-top:8px"></div><div id=sr class="small" style="margin-top:8px"></div>
</section>
<section class=card><h2>Largest in the cache</h2>
<div class=add><span class="mut small">by</span>
<label class=small><input type=radio name=uby value=file checked> file</label>
<label class=small><input type=radio name=uby value=folder> folder</label>
<label class=small><input type=radio name=uby value=torrent> torrent</label>
<span id=utot class="mut small"></span></div>
<div id=ures class="small wrap"></div>
</section>
<main id=m>
{msg}
<div class=tiles>
<div class=tile><span>Cache</span><b>{cache}</b><span>{cap}</span><div class=bar><i style="width:{cachepct}%"></i></div></div>
<div class=tile><span>Disk free</span><b>{dfree}</b><span>of {dtotal}</span></div>
<div class=tile><span>Download</span><b>{down}</b><span>upload {up} · received since start {rx}</span></div>
<div class=tile><span>Torrents</span><b>{ntor}</b><span>in {nfold} folders · {nawake} awake</span></div>
</div>
<section class=card><h2>Activity</h2>{act}</section>
<section class=card><h2>Folders</h2><div class=wrap><table>
<tr><th>Folder</th><th>Torrents</th><th class=n>Size</th><th>Status</th></tr>
{sources}
</table></div></section>
<section class=card><h2>Cache</h2><div class=wrap><table>
<tr><th>Torrent</th><th class=n>Size</th><th class=n>Cached</th><th></th></tr>
{rows}
</table></div>{unknown}</section>
</main>
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
const badge={{'cached':'ok','partial':'','not cached':'mut','queued':'busy','caching':'busy','downloading':'busy'}};
let sq='',so=0,st;
function pathHtml(p){{  // each folder of the path lists that folder when clicked
  const parts=p.split('/'); let acc='';
  return parts.map((x,i)=>{{acc+=(i?'/':'')+x; return i<parts.length-1?'<a href=# class=seg data-f="'+esc(acc)+'">'+esc(x)+'</a>':'<b>'+esc(x)+'</b>'}}).join('/');
}}
function row(f){{
  const pct=f.size?Math.min(100,Math.round(f.cached*100/f.size)):0;
  const label=f.status=='partial'?'partial '+pct+'%':f.status;
  const hl='<div class="small '+(f.dead?'warn':'mut')+'">'+esc(f.health_text||'')+'</div>';
  const btn=['cached','queued','caching'].includes(f.status)?'':'<button class=cache data-p="'+esc(f.path)+'">Cache</button>';
  const lk=' <span class=mut>· <a href="'+esc(f.smb_url)+'" title="'+esc(f.smb_url)+'">open</a> · <a href=# data-copy="'+esc(f.unc_path)+'" title="'+esc(f.unc_path)+'">copy \\\\\\\\ path</a></span>';
  return '<tr><td class=small>'+pathHtml(f.path)+lk+hl+'</td><td class=n>'+sz(f.size)+'</td><td class=n><span class="pill '+(badge[f.status]||'')+'">'+esc(label)+'</span></td><td class=n>'+btn+'</td></tr>';
}}
async function runSearch(more){{
  const q=document.getElementById('q').value.trim(), out=document.getElementById('sr');
  if(!more){{so=0}} sq=q; if(!q){{out.innerHTML='';return}}
  const r=await (await fetch('/api/search?q='+encodeURIComponent(q)+'&limit=100&offset='+so)).json();
  if(q!==sq)return;
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
async function cache(d){{
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
document.querySelectorAll('input[name=uby]').forEach(i=>i.addEventListener('change',loadUsage));
loadUsage(); setInterval(()=>{{if(!document.querySelector('#ures [data-armed]'))loadUsage()}},30000);
document.getElementById('q').addEventListener('input',()=>{{clearTimeout(st);st=setTimeout(()=>runSearch(false),250)}});
document.addEventListener('click',e=>{{
  const t=e.target;
  if(t.dataset.copy!==undefined){{e.preventDefault();navigator.clipboard?.writeText(t.dataset.copy).then(()=>{{t.textContent='copied'}},()=>{{prompt('Path:',t.dataset.copy)}})}}
  else if(t.classList.contains('seg')||t.classList.contains('browse')){{e.preventDefault();const q=document.getElementById('q');q.value='folder:'+t.dataset.f;q.scrollIntoView({{block:'center'}});runSearch(false)}}
  else if(t.classList.contains('cache'))cache({{path:t.dataset.p}});
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
    if(t.dataset.armed){{t.disabled=true;t.textContent='Queuing…';cache({{q:document.getElementById('q').value.trim()}})}}
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


def ask_form(field, value, dis, label, question, current):
    """A button that asks for a folder name, then posts it to /rename."""
    q = json.dumps(question).replace('"', "&quot;"); c = json.dumps(current).replace('"', "&quot;")
    return (f"<form method=post action=/rename onsubmit=\"const n=prompt({q},{c});if(!n||n=={c})return false;this.new.value=n\">"
            f"<input type=hidden name={field} value=\"{html.escape(value, quote=True)}\"><input type=hidden name=new>"
            f"<button {dis}>{label}</button></form>")


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
    folders = {}
    for x in s["sources"]:
        folders.setdefault(x["route"], []).append(x)
    srows = ""
    for route, xs in folders.items():
        counts = {}
        for x in xs:
            counts[x["status"]] = counts.get(x["status"], 0) + 1
        status = ", ".join(f"{n} {st}" if len(xs) > 1 else st for st, n in counts.items())
        awake = sum(1 for x in xs if x.get("awake"))
        dead = sum(1 for x in xs if looks_dead(x["hash"]))
        checked = sum(1 for x in xs if x["hash"] in health)
        status += f" · {awake} awake" if len(xs) > 1 else (" · awake" if awake else " · dormant")
        status += (f" · health: {checked}/{len(xs)} checked, {dead} with no peer found" if len(xs) > 1
                   else f" · {health_text(xs[0]['hash'])}")
        names = sorted({x["name"] or (x["hash"] or "")[:12] for x in xs})
        label = html.escape(names[0]) + (f" <span class=mut>+{len(xs) - 1}</span>" if len(xs) > 1 else "")
        inner = ""
        for x in xs[:200]:
            inner += (f"<tr class=sub><td>{html.escape(x['name'] or (x['hash'] or '')[:12])} <span class=\"mut small\">{x['kind']} · {(x['hash'] or '')[:8]} · {health_text(x['hash'])}</span></td>"
                      f"<td class=\"n small\">{mb(x['size']) if x['size'] else ''}</td><td class=small>{html.escape(x['status'])}</td>"
                      f"<td class=n>{ask_form('id', x['id'], dis, 'Move', 'Move this torrent to the folder (an existing folder name merges it there):', route)} "
                      f"<form method=post action=/remove><input type=hidden name=id value={x['id']}>"
                      f"<button class=danger {dis} name=erase value=1 data-confirm=\"Remove this torrent and erase its local data?\">Remove</button> "
                      f"<button {dis} name=erase value=0 data-confirm=\"Remove this torrent and keep its local data?\">Remove, keep data</button></form></td></tr>")
        srows += (f"<tr><td><b>{html.escape(route)}</b> <a href=# class=browse data-f=\"{html.escape(route, quote=True)}\">browse</a> {ask_form('route', route, dis, 'Rename', 'New name for this folder (an existing folder name merges the two; later torrents of the old name follow):', route)}</td><td>{label}</td><td class=n>{mb(sum(x['size'] for x in xs))}</td>"
                  f"<td>{html.escape(status)}</td></tr>"
                  f"<tr><td colspan=4 style=\"padding-top:0\"><details data-k=\"src/{html.escape(route, quote=True)}\"><summary class=\"mut small\">"
                  f"{len(xs)} torrent{'s' if len(xs) > 1 else ''}</summary><table>{inner}</table>"
                  + (f"<div class=\"mut small\">… and {len(xs) - 200} more (the full list is in <a href=/api>/api</a>)</div>" if len(xs) > 200 else "")
                  + "</details></td></tr>")
    if not srows:
        srows = "<tr><td colspan=4 class=mut>No torrent yet: add one above.</td></tr>"
    msg = f"<div class=msg>{html.escape(s['message'])}</div>" if s["message"] else ""
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
    a = activity_view()
    act = ""
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
        act += "".join(f"<div class=\"mut small\">could not cache {html.escape(f['path'])}: {html.escape(f['error'])}</div>" for f in pf["failed"][-3:])
    if a["files"]:
        act += (f"<div class=\"mut small\" style=\"margin-top:10px\">Files that received data in the last {WINDOW} s "
                "(counted per completed piece):</div><div class=wrap><table>")
        for f in a["files"][:15]:
            act += (f"<tr><td class=small>{html.escape(f['path'])}</td><td class=\"n small\">{mb(f['bytes'])}</td>"
                    f"<td class=\"n small mut\">{f['ago']} s ago</td></tr>")
        act += "</table></div>"
    return PAGE.format(capform=capform, act=act, down=rate(a["down"]), up=rate(a["up"]), dfree=mb(s["disk_free"]), dtotal=mb(s["disk_total"]), dmax=mb(s["max_cap_mb"] * 1e6),
                       state=html.escape(s["state"]), pillcls="ok" if s["state"] == "running" else "busy",
                       cache=mb(s["total_cache"]), cap=cap, cachepct=pct, msg=msg, rows=rows, sources=srows, unknown=unk,
                       dis=dis, rx=mb(s["network_rx"]), tx=mb(s["network_tx"]), ntor=len(s["sources"]), nfold=len(folders),
                       nawake=sum(1 for x in s["sources"] if x.get("awake")))



# ---------------------------------------------------------------- API (JSON): status, torrents, files, usage, erase

SMB_SHARE = os.environ.get("ATHENAFS_SMB_SHARE", "torrents")


def smb_host():
    """Name clients use for the SMB server: ATHENAFS_SMB_HOST, else the host of the page's address."""
    return os.environ.get("ATHENAFS_SMB_HOST") or urllib.parse.urlparse(PAGE_URL).hostname or "localhost"


def locations(path, is_dir=False):
    """Where a share path is found from a client: smb:// URL (macOS Finder, Linux) and UNC path (Windows)."""
    h = smb_host()
    p = path.strip("/")
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
                         "done": a["prefetch"]["done"], "failed": len(a["prefetch"]["failed"])},
            "smb": locations("", True)}


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
    return {"path": p, "size": size, "status": st, "cached": have, "torrent": {"hash": h, "name": t["name"] if t else "",
            "path_in_torrent": tpath}, "health": health.get(h), "health_text": health_text(h), **locations(p)}


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


def erase_many(targets):
    """Erase several (torrent, path) targets with one restart."""
    tp = torrent_pieces()

    def job():
        wanted, kept = set(), 0
        for ih, p in targets:
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
    ("GET", "/api/search", "q (words, all must match; or folder:path), limit (≤ 500), offset",
     "Files whose path contains every word. Each result: size, status (cached, partial, not cached, downloading, queued, caching), cached bytes, health, smb_url, unc_path."),
    ("GET", "/api/file", "path (as in the share)", "One file: status, cached bytes, torrent, health, smb_url, unc_path."),
    ("GET", "/api/usage", "by (file, folder or torrent), depth (folder levels, default 3), folder (only under this share path), limit, offset",
     "What takes the most room in the cache, largest first."),
    ("GET", "/api/activity", "", "Rates per torrent, files that received data in the last 30 s, prefetch queue."),
    ("GET", "/api", "", "Full status as used by the page (sources, cache tree, settings)."),
    ("POST", "/api/prefetch", "path (repeatable) or q (a search)", "Read files into the cache ahead of time. Refused when larger than 90 % of the cache cap."),
    ("POST", "/api/prefetch/cancel", "", "Empty the prefetch queue and stop the reads in progress."),
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
        try:
            if u.path == "/api":
                body = served["json"] or json.dumps(snapshot()).encode()
            elif u.path == "/api/search":
                res = search(one("q"), one("limit", "50"), one("offset", "0"))
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
            elif u.path == "/api/usage":
                res = usage(one("by", "file"), one("limit", "50"), one("offset", "0"), one("depth", "3"), one("folder"))
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

    def do_POST(self):
        if not self.authorized():
            return
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_TORRENT:
            self.send_response(413); self.send_header("Content-Length", "0"); self.end_headers(); return
        raw = self.rfile.read(n)
        go = lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start()
        if self.path in ("/api/prefetch", "/api/prefetch/cancel", "/api/erase"):
            q = body_params(raw, self.headers.get("Content-Type"))
            one = lambda k: str((q.get(k) or [""])[0])
            if self.path == "/api/erase":
                h = one("hash").lower()
                targets = erase_targets(one("path"), h if re.fullmatch(r"[0-9a-f]{40}", h) else "")
                if targets and state["state"] == "running":
                    go(erase_many, targets)
                    res = {"erasing": len(targets), "note": "the service restarts; see /api/status for the result"}
                else:
                    res = {"error": "nothing to erase there" if not targets else "the service is busy, retry in a few seconds"}
            elif self.path.endswith("cancel"):
                res = prefetch_cancel()
            else:
                res = prefetch_add(one("q"), [str(x) for x in q.get("path") or []])
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
            elif self.path == "/settings":
                go(apply_settings, one("mode"), one("cache_gb") or "0", one("reserve_pct") or "20")
            elif self.path == "/add" and one("link"):
                go(add_links, one("link"), one("route"))
            elif self.path == "/rename" and one("new") and (one("route") or re.fullmatch(r"[0-9a-f]{8}", one("id"))):
                go(rename, one("new"), one("route"), one("id") if not one("route") else "")
            elif self.path == "/remove" and re.fullmatch(r"[0-9a-f]{8}", one("id")):
                go(remove_source, one("id"), one("erase") != "0")
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()


# ---------------------------------------------------------------- activity (rates, files receiving pieces)

WINDOW = 30   # seconds of history for "files receiving data"
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
    """(status, cached bytes) of one file. Status: cached, partial, not cached, or queued / caching (prefetch) and
    downloading (pieces of it reached the cache in the last seconds)."""
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
        if path in prefetch["active"]:
            return "caching", have
        if path in prefetch["queued"]:
            return "queued", have
    if total and present == total:
        return "cached", size
    if any(f["path"] == path for f in activity["files"]):
        return "downloading", have
    return ("partial" if present else "not cached"), have


def search(q, limit=50, offset=0):
    """Files whose path (as seen in the share) contains every word of q, case-insensitive; or folder:<path>."""
    limit, offset = max(1, min(int(limit), 500)), max(0, int(offset))
    base, args = query_sql(q)
    if not base:
        return {"query": q, "total": 0, "results": []}
    c = db()
    try:
        total, total_size = c.execute(f"SELECT count(*), coalesce(sum(size), 0) FROM (SELECT files.size {base} LIMIT {SEARCH_CAP + 1})",
                                      args).fetchone()
        rows = c.execute(f"SELECT files.hash, files.path, files.tpath, files.size, files.off {base} LIMIT ? OFFSET ?",   # rowid order
                         args + [limit, offset]).fetchall()
    except sqlite3.OperationalError as e:
        return {"query": q, "total": 0, "results": [], "error": str(e)}
    finally:
        c.close()
    tp, cached = torrent_pieces(), set(cache_files())
    out = []
    for h, path, tpath, size, off in rows:
        st, have = file_state(h, path, off, size, tp, cached)
        out.append({"path": path, "size": size, "hash": h, "torrent_path": tpath, "cached": have, "status": st,
                    "health": health.get(h), "health_text": health_text(h), "dead": looks_dead(h), **locations(path)})
    return {"query": q, "total": min(total, SEARCH_CAP), "total_capped": total > SEARCH_CAP, "total_size": total_size,
            "offset": offset, "results": out}


# ---------------------------------------------------------------- prefetch ("add to cache"): read files through the mount

PREFETCH_WORKERS = 2
plock = threading.Lock()
prefetch = {"queue": collections.deque(), "queued": {}, "hash": {}, "active": {}, "done": 0, "failed": [], "bytes": 0}


def prefetch_add(q="", paths=()):
    """Queue the files of a search (q, as in search(), at most SEARCH_CAP) or the given share paths for reading.
    Refused when the total is larger than the cache cap: the first files would be evicted before the last are read."""
    c = db()
    try:
        if q:
            base, args = query_sql(q)
            if not base:
                return {"error": "empty query"}
            rows = c.execute(f"SELECT files.path, files.size, files.hash {base} LIMIT {SEARCH_CAP}", args).fetchall()
        else:
            rows = [r for p in paths for r in c.execute("SELECT path, size, hash FROM files WHERE path = ?", (p,)).fetchall()]
    finally:
        c.close()
    capmb = effective_cap_mb(load_settings())
    with plock:
        new = [(p, n, h) for p, n, h in rows if p not in prefetch["queued"] and p not in prefetch["active"]]
        size = sum(n for _, n, _ in new)
        if size + sum(prefetch["queued"].values()) > capmb * 1e6 * 0.9:
            return {"error": f"{mb(size)} is more than the cache can hold ({mb(capmb * 1e6)}): nothing queued", "size": size}
        for p, n, h in new:
            prefetch["queue"].append(p); prefetch["queued"][p] = n; prefetch["hash"][p] = h
    dead = sum(1 for _, _, h in new if looks_dead(h))
    return {"queued": len(new), "size": size, "already": len(rows) - len(new), "no_peer": dead}


def prefetch_cancel():
    with plock:
        n = len(prefetch["queue"])
        prefetch["queue"].clear(); prefetch["queued"].clear(); prefetch["hash"].clear()
        for a in prefetch["active"].values():
            a["cancel"] = True
    return {"cancelled": n}


def prefetch_worker():
    while True:
        with plock:
            path = prefetch["queue"].popleft() if prefetch["queue"] and state["state"] == "running" else None
            if path:
                size = prefetch["queued"].pop(path, 0)
                job = prefetch["active"][path] = {"path": path, "size": size, "read": 0, "cancel": False,
                                                  "hash": prefetch["hash"].pop(path, ""), "since": time.time()}
        if not path:
            time.sleep(1); continue
        ok, err = False, ""
        for attempt in range(3):   # a read can time out (no peer yet) or fail during a restart
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
                err = str(e); time.sleep(5)
        with plock:
            prefetch["active"].pop(path, None)
            prefetch["bytes"] += job["read"]
            if ok:
                prefetch["done"] += 1
            elif not job["cancel"]:
                prefetch["failed"] = (prefetch["failed"] + [{"path": path, "error": err}])[-20:]


def prefetch_view():
    with plock:
        return {"queued": len(prefetch["queue"]), "queued_size": sum(prefetch["queued"].values()),
                "active": [{k: v for k, v in a.items() if k != "cancel"} for a in prefetch["active"].values()],
                "done": prefetch["done"], "failed": list(prefetch["failed"]), "bytes_read": prefetch["bytes"]}


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
    start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=indexer, daemon=True).start()
    threading.Thread(target=snapshotter, daemon=True).start()
    for _ in range(PREFETCH_WORKERS):
        threading.Thread(target=prefetch_worker, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 8080), H)
    signal.signal(signal.SIGTERM, lambda *a: (stop(), os._exit(0)))
    srv.serve_forever()


main()
