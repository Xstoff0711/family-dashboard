#!/usr/bin/env python3
"""Familiendashboard: winziger Server ohne Abhängigkeiten (nur Python-Standardbibliothek).

- Speichert pro Bereich ein JSON-Dokument in SQLite (/data/familie.db)
- Liefert die Oberfläche aus ./static
- Kein Login: gedacht für den Zugriff über Tailscale + Caddy
"""
import ipaddress
import socket
import datetime as dt
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlencode, urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DB_PATH = os.environ.get("DB_PATH", "/data/familie.db")
PORT = int(os.environ.get("PORT", "8080"))
STATIC = (Path(__file__).parent / "static").resolve()
KEYS = {"recipes", "plan", "activities", "chores", "shopping", "meta"}
PHOTO_DIR = Path(os.environ.get("PHOTO_DIR", str(Path(DB_PATH).parent / "photos")))
MAX_PHOTO = 4 * 1024 * 1024
ID_RE = re.compile(r"^[a-z0-9]{2,32}$")
MAX_BODY = 2 * 1024 * 1024
LOCK = threading.Lock()

# --- Event-Abfrage (krefeld.de) ---
TZ_NAME = os.environ.get("TZ", "Europe/Berlin")
EVENTS_AUTO = os.environ.get("EVENTS_AUTO", "1") != "0"
EVENTS_REFRESH_H = float(os.environ.get("EVENTS_REFRESH_HOURS", "24"))
EVENTS_MIN_H = float(os.environ.get("EVENTS_MIN_INTERVAL_HOURS", "3"))
EVENTS_WINDOW = int(os.environ.get("EVENTS_WINDOW_DAYS", "31"))
KREFELD_URL = os.environ.get("KREFELD_URL", "https://www.krefeld.de").rstrip("/")
KREFELD_MAX_PAGES = 60
KINDALING_URL = os.environ.get("KINDALING_URL", "https://www.kindaling.de").rstrip("/")
KINDALING_REGIONS = [r.strip() for r in os.environ.get("KINDALING_REGIONS", "duesseldorf,koeln").split(",") if r.strip()]
KINDALING_MAX_PAGES = 25
CITIES = [c.strip() for c in os.environ.get(
    "EVENTS_CITIES",
    "Krefeld,Willich,Meerbusch,Kempen,Viersen,Moers,Duisburg,Neuss,Mönchengladbach,Düsseldorf,Korschenbroich,Nettetal,"
    "Grevenbroich,Ratingen,Oberhausen,Mülheim,Dormagen,Wesel,Venlo,Hilden,Essen,Langenfeld,Solingen,Leverkusen,Kleve,Köln"
).split(",") if c.strip()]
EV_LOCK = threading.Lock()
EV = {"status": "idle", "updated": None, "error": None, "items": [], "window": None}
EV_LAST_START = 0.0

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "k TEXT PRIMARY KEY, v TEXT NOT NULL, "
            "updated TEXT NOT NULL DEFAULT (datetime('now')))"
        )


class EventError(Exception):
    pass


# ---------------------------------------------------------------- Rezept per Link

IMPORT_MAX_BYTES = 3 * 1024 * 1024
IMPORT_ALLOW_PRIVATE = os.environ.get("IMPORT_ALLOW_PRIVATE", "0") == "1"  # nur für Tests


class ImportError_(Exception):
    pass


def _check_public_host(host):
    """Schutz davor, dass der Server über einen Link interne Adressen abfragt."""
    if IMPORT_ALLOW_PRIVATE:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ImportError_("Die Adresse wurde nicht gefunden.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ImportError_("Diese Adresse ist nicht erlaubt (nur öffentliche Webseiten).")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlparse(newurl)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ImportError_("Ungültige Weiterleitung.")
        _check_public_host(u.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or len(url) > 2000:
        raise ImportError_("Bitte einen vollständigen Link mit https:// einfügen.")
    _check_public_host(u.hostname)
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; Familiendashboard/1.0; privat)",
        "Accept": "text/html,application/xhtml+xml", "Accept-Language": "de,en;q=0.7"})
    try:
        with opener.open(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            if "html" not in ctype and "xml" not in ctype:
                raise ImportError_("Der Link führt nicht zu einer Webseite.")
            raw = r.read(IMPORT_MAX_BYTES + 1)
            charset = r.headers.get_content_charset() or "utf-8"
    except ImportError_:
        raise
    except urllib.error.HTTPError as e:
        raise ImportError_(f"Die Seite antwortet mit Fehler {e.code}.")
    except (urllib.error.URLError, OSError, ValueError):
        raise ImportError_("Die Seite konnte nicht geladen werden.")
    if len(raw) > IMPORT_MAX_BYTES:
        raise ImportError_("Die Seite ist zu groß.")
    try:
        return raw.decode(charset, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _walk_recipes(node, out):
    if isinstance(node, list):
        for x in node:
            _walk_recipes(x, out)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Recipe" in types:
            out.append(node)
        for k in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
            if k in node:
                _walk_recipes(node[k], out)


def _steps(v):
    out = []
    if isinstance(v, str):
        parts = re.split(r"\r?\n+", _txt(v.replace("<br>", "\n").replace("</p>", "\n")) if "<" in v else v)
        out += [p.strip() for p in parts if p.strip()]
    elif isinstance(v, list):
        for x in v:
            out += _steps(x)
    elif isinstance(v, dict):
        if "itemListElement" in v:
            out += _steps(v["itemListElement"])
        elif v.get("text"):
            out += _steps(v["text"])
        elif v.get("name"):
            out += _steps(v["name"])
    return out


def parse_recipe_page(page_html):
    found = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            _walk_recipes(json.loads(html.unescape(m.group(1).strip()) if m.group(1).lstrip().startswith("&") else m.group(1).strip()), found)
        except ValueError:
            continue
    for rec in found:
        ing_raw = rec.get("recipeIngredient") or rec.get("ingredients") or []
        if isinstance(ing_raw, str):
            ing_raw = [ing_raw]
        ing = [_txt(str(x)) for x in ing_raw if _txt(str(x))]
        steps = [_txt(x) for x in _steps(rec.get("recipeInstructions")) if _txt(x)]
        name = _txt(str(rec.get("name", "")))
        if name and (ing or steps):
            return {"name": name[:120], "keto": False, "kid": True, "ing": ing[:60], "steps": steps[:40]}
    return None


def import_url(url):
    try:
        rec = parse_recipe_page(fetch_page(url.strip()))
    except ImportError_ as e:
        return 422, {"error": "import", "message": str(e)}
    except Exception as e:
        print("Import-Fehler:", repr(e), flush=True)
        return 500, {"error": "import", "message": "Der Import ist fehlgeschlagen."}
    if not rec:
        return 422, {"error": "no_recipe", "message": "Auf dieser Seite sind keine Rezeptdaten hinterlegt. Probier den Foto-Import oder tippe es ein."}
    return 200, rec


# ---------------------------------------------------------------- Events

def now_local():
    try:
        return dt.datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return dt.datetime.now()


def _txt(h):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def parse_krefeld(page_html):
    """Liest die Eventliste der Stadt Krefeld (serverseitig erzeugtes HTML) aus."""
    out = []
    for chunk in page_html.split('<div class="a5-event">')[1:]:
        chunk = chunk.split("</li>")[0]
        m_href = re.search(r'<a href="(/[^"]+)"[^>]*rel="bookmark"', chunk)
        m_title = re.search(r"<strong>(.*?)</strong>", chunk, re.S)
        times = re.findall(r'<time datetime="(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})', chunk)
        if not (m_href and m_title and times):
            continue
        tags_html = re.search(r'<p class="event-tags">(.*?)</p>', chunk, re.S)
        tags = [_txt(t) for t in re.findall(r"<a [^>]*>(.*?)</a>", tags_html.group(1), re.S)] if tags_html else []
        desc = ""
        for p in re.findall(r"<p>(.*?)</p>", chunk, re.S):
            desc = _txt(p)
            if desc:
                break
        d0, t0 = times[0]
        d1 = times[-1][0]
        title = _txt(m_title.group(1))
        if not title:
            continue
        out.append({
            "title": title[:140], "from": d0, "to": max(d0, d1),
            "time": f"{t0} Uhr" if t0 != "00:00" else "", "place": "", "cost": "",
            "note": desc[:200], "url": KREFELD_URL + m_href.group(1),
            "cat": "other", "tags": tags[:6], "src": "krefeld.de", "city": "Krefeld",
        })
    return out


def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Familiendashboard/1.0 (privat, 1x taeglich)", "Accept-Language": "de"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


ADULT_TAGS = ("Konzerte", "Theater", "Märkte", "Party", "Vortrag", "Ausstellungen")


def fetch_krefeld(start, end):
    """Holt alle Events im Zeitraum (jedes Datum einer Terminserie einzeln) und ordnet sie ein."""
    found = {}
    for page in range(KREFELD_MAX_PAGES):
        q = urlencode({"date_begin": start.isoformat(), "date_end": end.isoformat(), "page": page})
        body = _http_get(f"{KREFELD_URL}/events?{q}")
        items = parse_krefeld(body)
        for it in items:
            tags = it["tags"]
            if "Kinder & Familie" in tags:
                it["cat"] = "family"
            elif any(t.startswith(ADULT_TAGS) for t in tags):
                it["cat"] = "adult"
            else:
                it["cat"] = "other"
            found.setdefault(f'{it["url"]}|{it["from"]}|{it["time"]}', it)
        time.sleep(0.4)
        if len(items) < 10 or f"page={page + 1}" not in body:
            break
    s_iso, e_iso = start.isoformat(), end.isoformat()
    keep = []
    for it in found.values():
        if it["to"] < s_iso or it["from"] > e_iso:
            continue
        try:
            if (dt.date.fromisoformat(it["to"]) - dt.date.fromisoformat(it["from"])).days > 60:
                continue  # Dauerausstellungen o.ä.
        except ValueError:
            continue
        keep.append(it)
    keep.sort(key=lambda e: (e["from"], e["time"], e["title"]))
    return keep[:800]


MONTHS = {m: i + 1 for i, m in enumerate(
    ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "dezember"])}


def _city_of(place):
    low = (place or "").lower()
    for c in CITIES:
        if low.startswith(c.lower()):
            return c
    return ""


def parse_kindaling(page_html, today, last_date=None):
    """Liest eine kindaling.de-Eventliste. Gibt (Einträge, letztes Datum) zurück."""
    out = []
    cur = last_date
    tokens = list(re.finditer(
        r'<div class="activity-list__activities__container-title">(.*?)</div>'
        r'|<a ([^>]*class="activity-list__activiti[ "][^>]*)>(.*?)</a>',
        page_html, re.S))
    for m in tokens:
        if m.group(1) is not None:
            hm = re.search(r"(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)", _txt(m.group(1)))
            if hm and hm.group(2).lower() in MONTHS:
                mon, day = MONTHS[hm.group(2).lower()], int(hm.group(1))
                year = today.year + (1 if mon < today.month - 1 else 0)
                try:
                    cur = dt.date(year, mon, day)
                except ValueError:
                    cur = None
            continue
        if not cur:
            continue
        hm2 = re.search(r'href="(/[^"]+)"', m.group(2))
        if not hm2:
            continue
        href, inner = hm2.group(1), m.group(3)
        title = _txt((re.search(r'activity-list__activiti__title">(.*?)</span>', inner, re.S) or [None, ""])[1])
        if not title:
            continue
        spans = re.search(r'activiti__address">(.*?)</span>\s*</span>', inner, re.S)
        place = _txt(spans.group(1)) if spans else ""
        city = _city_of(place)
        tags = [_txt(t).lstrip("#").strip() for t in re.findall(r'class="category-tag">(.*?)</span>', inner, re.S)]
        tags = [t for t in tags if t]
        if not city or any("Online" in t for t in tags):
            continue
        tm = re.search(r"(\d{1,2}:\d{2})\s*Uhr", _txt((re.search(r"icon_clock.*?<span>(.*?)</span>", inner, re.S) or [None, ""])[1]))
        price = _txt((re.search(r"activiti__price.*?<span>(.*?)</span>\s*</span>", inner, re.S) or [None, ""])[1])
        age = _txt((re.search(r"activiti__age.*?<span>(.*?)</span>", inner, re.S) or [None, ""])[1])
        out.append({
            "title": title[:140], "from": cur.isoformat(), "to": cur.isoformat(),
            "time": f"{tm.group(1)} Uhr" if tm else "", "place": place[:80], "cost": price,
            "note": (f"Alter: {age}" if age else ""), "url": KINDALING_URL + href,
            "cat": "family", "tags": tags[:6], "src": "kindaling.de", "city": city,
        })
    return out, cur


def fetch_kindaling(region, start, end):
    found, last = {}, None
    s_iso, e_iso = start.isoformat(), end.isoformat()
    for page in range(1, KINDALING_MAX_PAGES + 1):
        body = _http_get(f"{KINDALING_URL}/veranstaltungen/{region}" + (f"?page={page}" if page > 1 else ""))
        items, last = parse_kindaling(body, start, last)
        for it in items:
            if s_iso <= it["from"] <= e_iso:
                found.setdefault(f'{it["url"]}|{it["from"]}|{it["time"]}', it)
        time.sleep(0.5)
        if not items and not last:
            break
        if last and last > end:
            break
        if f"page={page + 1}" not in body:
            break
    return list(found.values())


def events_save():
    with LOCK, db() as c:
        c.execute(
            "INSERT INTO kv(k, v, updated) VALUES('events', ?, datetime('now')) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
            (json.dumps({k: EV[k] for k in ("updated", "items", "window")}, ensure_ascii=False),),
        )


def events_load():
    try:
        with db() as c:
            row = c.execute("SELECT v FROM kv WHERE k='events'").fetchone()
        if row:
            EV.update(json.loads(row[0]))
    except (sqlite3.Error, ValueError):
        pass


def events_worker():
    try:
        today = now_local().date()
        end = today + dt.timedelta(days=EVENTS_WINDOW - 1)
        items, warn = [], None
        try:
            items = fetch_krefeld(today, end)
        except Exception as e:
            print("krefeld.de-Fehler:", repr(e), flush=True)
            warn = "krefeld.de war nicht erreichbar."
        failed = []
        for reg in KINDALING_REGIONS:
            try:
                items += fetch_kindaling(reg, today, end)
            except Exception as e:
                print("kindaling-Fehler", reg, repr(e), flush=True)
                failed.append(reg)
        if failed and not warn:
            warn = "Nicht erreichbar: kindaling.de (" + ", ".join(failed) + ")."
        seen, uniq = set(), []
        for it in items:  # gleiche Veranstaltung aus mehreren Regionen nur einmal
            k = (it["url"], it["from"], it["time"])
            if k not in seen:
                seen.add(k)
                uniq.append(it)
        items = uniq[:2500]
        if not items and warn:
            raise EventError(warn)
        items.sort(key=lambda e: (e["from"], e.get("time", ""), e["title"]))
        with EV_LOCK:
            EV.update(items=items, updated=now_local().isoformat(timespec="seconds"), error=warn,
                      window={"from": today.isoformat(), "to": end.isoformat()})
        events_save()
        print(f"Events aktualisiert: {len(items)} Einträge", flush=True)
    except EventError as e:
        with EV_LOCK:
            EV["error"] = str(e)
    except Exception as e:  # unerwartet: nie den Server gefährden
        print("Events-Fehler:", repr(e), flush=True)
        with EV_LOCK:
            EV["error"] = "Die Event-Abfrage ist fehlgeschlagen."
    finally:
        with EV_LOCK:
            EV["status"] = "idle"


def events_public():
    with EV_LOCK:
        snap = {k: EV[k] for k in ("status", "updated", "error", "items", "window")}
    stale = True
    if snap["updated"]:
        try:
            age = (now_local() - dt.datetime.fromisoformat(snap["updated"])).total_seconds() / 3600
            stale = age >= EVENTS_REFRESH_H
        except (ValueError, TypeError):
            stale = True
    snap.update(enabled=True, auto=EVENTS_AUTO, stale=stale)
    return snap


def events_refresh():
    """Startet die Aktualisierung im Hintergrund. Gibt (HTTP-Status, Antwort) zurück."""
    global EV_LAST_START
    with EV_LOCK:
        if EV["status"] == "running":
            return 202, {"status": "running"}
        if time.time() - EV_LAST_START < EVENTS_MIN_H * 3600:
            return 200, {"status": "idle", "recent": True}
        EV_LAST_START = time.time()
        EV["status"] = "running"
    threading.Thread(target=events_worker, daemon=True).start()
    return 202, {"status": "running"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Familiendashboard/1.0"

    def log_message(self, fmt, *args):
        print("%s %s" % (self.address_string(), fmt % args), flush=True)

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path == "/api/state":
            with LOCK, db() as c:
                rows = c.execute("SELECT k, v FROM kv").fetchall()
            return self._json(200, {k: json.loads(v) for k, v in rows if k in KEYS})
        if path == "/api/events":
            return self._json(200, events_public())
        if path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if path.startswith("/photos/"):
            m = re.fullmatch(r"/photos/([a-z0-9]{2,32})\.jpg", path)
            f = PHOTO_DIR / f"{m.group(1)}.jpg" if m else None
            if not f or not f.is_file():
                return self._json(404, {"error": "not found"})
            return self._send(200, f.read_bytes(), "image/jpeg", {"Cache-Control": "public, max-age=31536000, immutable"})
        self._static(path)

    def _static(self, path):
        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC / rel).resolve()
        if STATIC not in target.parents and target != STATIC:
            return self._json(403, {"error": "forbidden"})
        if not target.is_file():
            return self._json(404, {"error": "not found"})
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/events/refresh":
            status, body = events_refresh()
            return self._json(status, body)
        m = re.fullmatch(r"/api/photo/([a-z0-9]{2,32})", path)
        if m:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._json(400, {"error": "bad length"})
            if length <= 0 or length > MAX_PHOTO:
                return self._json(413, {"error": "bad size"})
            data = self.rfile.read(length)
            if data[:3] != b"\xff\xd8\xff":
                return self._json(400, {"error": "nur JPEG erlaubt"})
            PHOTO_DIR.mkdir(parents=True, exist_ok=True)
            tmp = PHOTO_DIR / f".{m.group(1)}.tmp"
            tmp.write_bytes(data)
            tmp.replace(PHOTO_DIR / f"{m.group(1)}.jpg")
            return self._json(200, {"ok": True})
        if path == "/api/import-url":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError
                url = str(json.loads(self.rfile.read(length).decode("utf-8")).get("url", ""))
            except (ValueError, TypeError, AttributeError):
                return self._json(400, {"error": "bad_request", "message": "Ungültiger Link."})
            status, result = import_url(url)
            return self._json(status, result)
        return self._json(404, {"error": "not found"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/photo/([a-z0-9]{2,32})", self.path.split("?", 1)[0])
        if not m:
            return self._json(404, {"error": "not found"})
        f = PHOTO_DIR / f"{m.group(1)}.jpg"
        if f.is_file():
            f.unlink()
        self._json(200, {"ok": True})

    def do_PUT(self):
        path = self.path.split("?", 1)[0]
        if not path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        key = path[len("/api/"):]
        if key not in KEYS:
            return self._json(404, {"error": "unknown key"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._json(400, {"error": "bad length"})
        if length <= 0 or length > MAX_BODY:
            return self._json(413, {"error": "bad size"})
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "invalid json"})
        with LOCK, db() as c:
            c.execute(
                "INSERT INTO kv(k, v, updated) VALUES(?, ?, datetime('now')) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        self._json(200, {"ok": True})


if __name__ == "__main__":
    init_db()
    events_load()
    print(f"Familiendashboard läuft auf Port {PORT}, Datenbank: {DB_PATH}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
