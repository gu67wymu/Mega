#!/usr/bin/env python3
"""
MegaSearch Crawler
------------------
Durchsucht öffentliche Webseiten (Startpunkte in seeds.txt) nach MEGA- und
drload-Links und speichert sie in data/index.json. Läuft in GitHub Actions.

Regeln:
- robots.txt jeder Seite wird beachtet (gesperrte Seiten werden übersprungen).
- Höchstens 1 Anfrage pro Sekunde pro Website.
- Nur Seiten derselben Website wie der Startpunkt werden verfolgt.
- Für MEGA-Links mit Schlüssel werden Name, Größe und Status über die
  öffentliche MEGA-API abgefragt.
"""
import base64
import html
import json
import os
import re
import struct
import sys
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag
from urllib import robotparser

import requests

try:
    from Crypto.Cipher import AES  # pycryptodome
except Exception:  # Namen können dann nicht entschlüsselt werden
    AES = None

# ---------------- Einstellungen ----------------
ROOT = os.path.dirname(os.path.abspath(__file__))
SEEDS_FILE = os.path.join(ROOT, "seeds.txt")
DATA_DIR = os.path.join(ROOT, "data")
INDEX_FILE = os.path.join(DATA_DIR, "index.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")

MAX_PAGES_PER_SITE = int(os.environ.get("MAX_PAGES_PER_SITE", "120"))
MAX_DEPTH = int(os.environ.get("MAX_DEPTH", "3"))
DELAY_PER_SITE = 1.0              # Sekunden zwischen Anfragen an dieselbe Website
TIME_BUDGET = int(os.environ.get("TIME_BUDGET", str(40 * 60)))  # Sekunden pro Lauf
MAX_API_CHECKS = int(os.environ.get("MAX_API_CHECKS", "300"))
RECHECK_DAYS = 3
MAX_ENTRIES = 50000
UA = "MegaSearchBot/1.0 (+https://gu67wymu.github.io/Mega/)"

SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|mp4|mp3|zip|rar|7z|pdf|exe|apk|dmg|iso|woff2?|ttf)(\?|$)", re.I)

MEGA_RX = re.compile(
    r"(?:https?://)?(?:www\.)?mega(?:\.co)?\.nz/"
    r"(?:(file|folder)/([A-Za-z0-9_-]{8})(?:#([A-Za-z0-9_-]{16,}))?"
    r"|#(F?)!([A-Za-z0-9_-]{8})(?:!([A-Za-z0-9_-]{16,}))?)"
)
DRLOAD_RX = re.compile(r"(?:https?://)?(?:www\.)?drload\.com/(file|folder)/([A-Za-z0-9_-]{4,})")
HREF_RX = re.compile(r"""href\s*=\s*["']([^"'#][^"']*)["']""", re.I)
TITLE_RX = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
TAG_RX = re.compile(r"<head.*?</head>|<script.*?</script>|<style.*?</style>|<[^>]+>", re.I | re.S)

session = requests.Session()
session.headers.update({"User-Agent": UA, "Accept-Language": "de,en;q=0.8"})


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log(*a):
    print(*a, flush=True)


# ---------------- Index laden/speichern ----------------
def load_index():
    try:
        with open(INDEX_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {e["k"]: e for e in data.get("items", [])}
    except Exception:
        return {}


def save_index(items, stats):
    os.makedirs(DATA_DIR, exist_ok=True)
    lst = sorted(items.values(), key=lambda e: e.get("found", ""), reverse=True)[:MAX_ENTRIES]
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": now_iso(), "count": len(lst), "items": lst}, f, ensure_ascii=False, separators=(",", ":"))
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)


# ---------------- Seeds ----------------
def load_seeds():
    seeds = []
    with open(SEEDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("http"):
                line = "https://" + line.rstrip("/*") + "/"
            seeds.append(line)
    return seeds


# ---------------- robots.txt ----------------
_robots = {}


def allowed(url):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    rp = _robots.get(base)
    if rp is None:
        rp = robotparser.RobotFileParser()
        try:
            r = session.get(base + "/robots.txt", timeout=15)
            if r.status_code in (401, 403):
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except Exception:
            rp.allow_all = True
        _robots[base] = rp
    return rp.can_fetch(UA, url)


# ---------------- Link-Erkennung ----------------
def clean_text(page_html):
    return re.sub(r"\s+", " ", html.unescape(TAG_RX.sub(" ", page_html))).strip()


def context_for(text, link_id, width=160):
    i = text.find(link_id)
    if i < 0:
        return ""
    a = max(0, i - width)
    s = text[a:i + len(link_id) + 40]
    s = MEGA_RX.sub("", s)
    s = DRLOAD_RX.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -|:·")[:260]


def extract_links(page_html, page_url):
    found = []
    raw = html.unescape(page_html)
    text = clean_text(page_html)
    m = TITLE_RX.search(page_html)
    title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:200] if m else ""
    for m in MEGA_RX.finditer(raw):
        typ = m.group(1) or ("folder" if m.group(4) == "F" else "file")
        lid = m.group(2) or m.group(5)
        key = m.group(3) or m.group(6) or ""
        full = bool(key) and (len(key) == 22 if typ == "folder" else len(key) == 43)
        if key and not full:
            key = ""  # gekürzte Schlüssel verwerfen
        found.append({"h": "mega", "t": typ, "id": lid, "key": key,
                      "ctx": context_for(text, lid), "src": page_url, "srcTitle": title})
    for m in DRLOAD_RX.finditer(raw):
        found.append({"h": "drload", "t": m.group(1), "id": m.group(2), "key": "",
                      "ctx": context_for(text, m.group(2)), "src": page_url, "srcTitle": title})
    return found


def merge(items, link):
    k = f'{link["h"]}:{link["t"]}:{link["id"]}'
    e = items.get(k)
    ts = now_iso()
    if not e:
        e = {"k": k, "h": link["h"], "t": link["t"], "id": link["id"], "key": "",
             "found": ts, "seen": ts, "src": [], "ctx": "", "srcTitle": ""}
        items[k] = e
        new = True
    else:
        new = False
    e["seen"] = ts
    if link["key"] and not e.get("key"):
        e["key"] = link["key"]
        e.pop("checked", None)
    if link["ctx"] and len(link["ctx"]) > len(e.get("ctx", "")):
        e["ctx"] = link["ctx"]
    if link["srcTitle"] and not e.get("srcTitle"):
        e["srcTitle"] = link["srcTitle"]
    if link["src"] not in e["src"]:
        e["src"] = (e["src"] + [link["src"]])[-5:]
    return new


# ---------------- Crawlen ----------------
def same_site(a, b):
    ha, hb = urlparse(a).netloc.lower(), urlparse(b).netloc.lower()
    ha, hb = ha.removeprefix("www."), hb.removeprefix("www.")
    return ha == hb or ha.endswith("." + hb)


def crawl_site(seed, items, stats, deadline):
    host = urlparse(seed).netloc
    st = {"seed": seed, "pages": 0, "links": 0, "new": 0, "blocked": 0, "errors": 0}
    stats["sites"].append(st)
    queue = deque([(seed, 0)])
    seen = {seed}
    last = 0.0
    while queue and st["pages"] < MAX_PAGES_PER_SITE and time.time() < deadline:
        url, depth = queue.popleft()
        if not allowed(url):
            st["blocked"] += 1
            continue
        wait = DELAY_PER_SITE - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.time()
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
        except Exception:
            st["errors"] += 1
            continue
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            st["errors"] += r.status_code != 200
            continue
        st["pages"] += 1
        body = r.text[:3_000_000]
        for link in extract_links(body, r.url):
            st["links"] += 1
            if merge(items, link):
                st["new"] += 1
        if depth >= MAX_DEPTH:
            continue
        for href in HREF_RX.findall(body):
            nxt = urldefrag(urljoin(r.url, html.unescape(href)))[0]
            if not nxt.startswith("http") or nxt in seen or SKIP_EXT.search(nxt):
                continue
            if not same_site(nxt, seed):
                continue
            seen.add(nxt)
            queue.append((nxt, depth + 1))
    log(f"{host}: {st['pages']} Seiten, {st['links']} Links ({st['new']} neu), "
        f"{st['blocked']} durch robots.txt gesperrt, {st['errors']} Fehler")


# ---------------- MEGA-API: Name, Größe, Status ----------------
def b64d(s):
    s = s.replace("-", "+").replace("_", "/").replace(",", "")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def a32(b):
    return list(struct.unpack(">%dI" % (len(b) // 4), b))


def a32_to_bytes(a):
    return struct.pack(">%dI" % len(a), *a)


def file_aes_key(k32):
    k = a32(k32)
    return a32_to_bytes([k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7]])


def dec_attr(at_b64, aes_key):
    raw = AES.new(aes_key, AES.MODE_CBC, b"\0" * 16).decrypt(b64d(at_b64))
    raw = raw.rstrip(b"\0")
    if not raw.startswith(b"MEGA{"):
        return None
    try:
        return json.loads(raw[4:].decode("utf-8", "ignore"))
    except Exception:
        return None


def mega_api(payload, folder=None):
    url = "https://g.api.mega.co.nz/cs?id=1" + (f"&n={folder}" if folder else "")
    r = session.post(url, data=json.dumps([payload]), timeout=20)
    r.raise_for_status()
    res = r.json()
    if isinstance(res, int):
        return res
    return res[0]


def check_mega(e):
    """Setzt name, size, files, status für einen MEGA-Eintrag mit Schlüssel."""
    key = b64d(e["key"])
    if e["t"] == "file":
        res = mega_api({"a": "g", "p": e["id"]})
        if isinstance(res, int):
            e["status"] = "offline"
            return
        e["status"] = "online"
        e["size"] = res.get("s")
        if AES and len(key) == 32 and res.get("at"):
            attr = dec_attr(res["at"], file_aes_key(key))
            if attr and attr.get("n"):
                e["name"] = attr["n"][:200]
    else:
        res = mega_api({"a": "f", "c": 1, "ca": 1, "r": 1}, folder=e["id"])
        if isinstance(res, int):
            e["status"] = "offline"
            return
        nodes = res.get("f", [])
        e["status"] = "online"
        e["files"] = sum(1 for n in nodes if n.get("t") == 0)
        e["size"] = sum(n.get("s", 0) for n in nodes if n.get("t") == 0)
        if AES and len(key) == 16:
            handles = {n.get("h") for n in nodes}
            root = next((n for n in nodes if n.get("t") == 1 and n.get("p") not in handles), None)
            if root and ":" in root.get("k", ""):
                enc = b64d(root["k"].split(":", 1)[1])
                nk = AES.new(key, AES.MODE_ECB).decrypt(enc)
                attr = dec_attr(root.get("a", ""), nk[:16] if len(nk) == 16 else file_aes_key(nk))
                if attr and attr.get("n"):
                    e["name"] = attr["n"][:200]


def enrich(items, deadline):
    cutoff = time.time() - RECHECK_DAYS * 86400
    todo = [e for e in items.values()
            if e["h"] == "mega" and e.get("key")
            and (not e.get("checked") or datetime.fromisoformat(e["checked"]).timestamp() < cutoff)]
    todo.sort(key=lambda e: e.get("checked", ""))
    done = 0
    for e in todo[:MAX_API_CHECKS]:
        if time.time() > deadline:
            break
        try:
            check_mega(e)
        except Exception as ex:
            e["status"] = e.get("status", "unbekannt")
            log("MEGA-API Fehler", e["id"], type(ex).__name__)
        e["checked"] = now_iso()
        done += 1
        time.sleep(1)
    log(f"MEGA-API: {done} Links geprüft")
    return done


# ---------------- Start ----------------
def main():
    start = time.time()
    deadline = start + TIME_BUDGET
    items = load_index()
    before = len(items)
    stats = {"run": now_iso(), "sites": []}
    seeds = load_seeds()
    per_site = max(60, int(TIME_BUDGET * 0.8 / max(1, len(seeds))))
    for seed in seeds:
        if time.time() > deadline:
            break
        crawl_site(seed, items, stats, min(deadline, time.time() + per_site))
    stats["checked"] = enrich(items, deadline + 600)
    stats["total"] = len(items)
    stats["added"] = len(items) - before
    stats["seconds"] = int(time.time() - start)
    save_index(items, stats)
    log(f"Fertig: {len(items)} Links im Index ({len(items) - before} neu) in {stats['seconds']} s")


if __name__ == "__main__":
    sys.exit(main())
