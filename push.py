"""Web-Push-Benachrichtigungen für das Familiendashboard.

- VAPID (RFC 8292) und Verschlüsselung aes128gcm (RFC 8291), umgesetzt mit der Bibliothek
  `cryptography`. Fehlt sie, läuft das Dashboard normal weiter, nur ohne Benachrichtigungen.
- Abos (eines pro Gerät) und Einstellungen (pro Konto) liegen in SQLite.
- Ereignisse werden pro Empfänger kurz gesammelt und gebündelt verschickt. Wer etwas
  auslöst, bekommt selbst keine Nachricht; benachrichtigt wird nur die eigene Familie.
"""
import base64
import json
import os
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True
except ImportError:  # pragma: no cover - ohne Bibliothek bleibt Push aus
    AVAILABLE = False

DELAY = float(os.environ.get("PUSH_DELAY_SECONDS", "20"))  # Bündelungsfenster
CONTACT = os.environ.get("PUSH_CONTACT", "").strip()        # z. B. mailto:du@example.de
TTL = 24 * 3600
MAX_SUBS_PER_USER = 10

# Nur bekannte Push-Dienste der Browser (verhindert Anfragen an beliebige Adressen)
PUSH_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")

EVENTS = {  # Schlüssel: (Bezeichnung im Profil, standardmäßig an)
    "task": ("Aufgabe erledigt", True),
    "shop": ("Neuer Artikel auf der Einkaufsliste", True),
    "plan": ("Essensplan geändert", False),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (
  endpoint TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  family_id TEXT NOT NULL,
  p256dh TEXT NOT NULL,
  auth TEXT NOT NULL,
  created TEXT NOT NULL DEFAULT (datetime('now')),
  last_ok TEXT
);
CREATE INDEX IF NOT EXISTS push_subs_user ON push_subs(user_id);
CREATE TABLE IF NOT EXISTS push_prefs (
  user_id TEXT PRIMARY KEY,
  prefs TEXT NOT NULL
);
"""


class PushError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(s):
    s = str(s).strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------- Schlüssel ----------
_KEY = None


def load_key(data_dir):
    """Lädt oder erzeugt den VAPID-Schlüssel (bleibt im Datenordner, damit Abos gültig bleiben)."""
    global _KEY
    if not AVAILABLE:
        return None
    f = Path(data_dir) / "vapid_private.pem"
    if f.is_file():
        _KEY = serialization.load_pem_private_key(f.read_bytes(), password=None)
    else:
        _KEY = ec.generate_private_key(ec.SECP256R1())
        pem = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_bytes(pem)
        os.chmod(tmp, 0o600)
        tmp.replace(f)
        print("Push: neuer VAPID-Schlüssel erzeugt", flush=True)
    return _KEY


def public_key():
    if _KEY is None:
        return None
    return b64u(_KEY.public_key().public_bytes(serialization.Encoding.X962,
                                               serialization.PublicFormat.UncompressedPoint))


def enabled():
    return _KEY is not None


# ---------- Verschlüsselung (RFC 8291, aes128gcm) ----------
def _hkdf(salt, ikm, info, length):
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload, p256dh_b64, auth_b64, _eph=None, _salt=None):
    ua_pub = unb64u(p256dh_b64)
    auth_secret = unb64u(auth_b64)
    if len(ua_pub) != 65 or ua_pub[0] != 4 or len(auth_secret) < 16:
        raise PushError("Ungültige Schlüssel im Abo.")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    eph = _eph or ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = _salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    record = payload + b"\x02"  # letzter (einziger) Datensatz
    rs = 4096
    if len(record) + 16 > rs:
        raise PushError("Nachricht zu lang.")
    body = AESGCM(cek).encrypt(nonce, record, None)
    return salt + struct.pack("!IB", rs, len(as_pub)) + as_pub + body


# ---------- VAPID (RFC 8292) ----------
def _vapid_header(endpoint, subject):
    u = urlparse(endpoint)
    claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject}
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64u(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    r, s = decode_dss_signature(_KEY.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return f"vapid t={head}.{body}.{b64u(sig)}, k={public_key()}"


def check_endpoint(endpoint):
    if not isinstance(endpoint, str) or len(endpoint) > 1000:
        return False
    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def send_one(sub, payload, subject):
    """Schickt eine Nachricht an ein Gerät. Rückgabe: HTTP-Status (0 bei Netzfehler)."""
    body = encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"), sub["p256dh"], sub["auth"])
    req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "TTL": str(TTL),
        "Urgency": "normal",
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "Authorization": _vapid_header(sub["endpoint"], subject),
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        detail = e.read(300).decode("utf-8", "replace")
        if e.code not in (404, 410):
            print(f"Push: {urlparse(sub['endpoint']).hostname} antwortet {e.code} {detail}", flush=True)
        return e.code
    except (urllib.error.URLError, OSError) as e:
        print(f"Push: {urlparse(sub['endpoint']).hostname} nicht erreichbar ({e})", flush=True)
        return 0


# ---------- Abos und Einstellungen ----------
def init(c):
    c.executescript(SCHEMA)


def get_prefs(c, user_id):
    row = c.execute("SELECT prefs FROM push_prefs WHERE user_id=?", (user_id,)).fetchone()
    stored = json.loads(row[0]) if row else {}
    return {k: bool(stored.get(k, default)) for k, (_, default) in EVENTS.items()}


def set_prefs(c, user_id, prefs):
    if not isinstance(prefs, dict):
        raise PushError("Ungültige Einstellungen.")
    cur = get_prefs(c, user_id)
    for k in EVENTS:
        if k in prefs:
            cur[k] = bool(prefs[k])
    c.execute("INSERT INTO push_prefs(user_id, prefs) VALUES(?, ?) "
              "ON CONFLICT(user_id) DO UPDATE SET prefs=excluded.prefs", (user_id, json.dumps(cur)))
    return cur


def subscribe(c, user, sub):
    if not isinstance(sub, dict):
        raise PushError("Ungültiges Abo.")
    endpoint, keys = sub.get("endpoint"), sub.get("keys") or {}
    if not check_endpoint(endpoint):
        raise PushError("Dieser Benachrichtigungsdienst wird nicht unterstützt.")
    p256dh, auth_ = keys.get("p256dh"), keys.get("auth")
    try:
        if len(unb64u(p256dh)) != 65 or len(unb64u(auth_)) < 16:
            raise ValueError
    except (ValueError, TypeError):
        raise PushError("Ungültiges Abo.")
    c.execute("INSERT INTO push_subs(endpoint, user_id, family_id, p256dh, auth) VALUES(?,?,?,?,?) "
              "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, family_id=excluded.family_id, "
              "p256dh=excluded.p256dh, auth=excluded.auth",
              (endpoint, user["id"], user["family_id"], p256dh, auth_))
    # Zu viele alte Geräte: die ältesten entfernen
    rows = c.execute("SELECT endpoint FROM push_subs WHERE user_id=? ORDER BY created DESC", (user["id"],)).fetchall()
    for (ep,) in rows[MAX_SUBS_PER_USER:]:
        c.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,))


def unsubscribe(c, user, endpoint):
    c.execute("DELETE FROM push_subs WHERE endpoint=? AND user_id=?", (str(endpoint or ""), user["id"]))


def device_count(c, user_id):
    return c.execute("SELECT COUNT(*) FROM push_subs WHERE user_id=?", (user_id,)).fetchone()[0]


# ---------- Ereignisse aus gespeicherten Daten erkennen ----------
def _done_keys(chores):
    out = {}
    for ch in chores if isinstance(chores, list) else []:
        if not isinstance(ch, dict):
            continue
        for d, v in (ch.get("done") or {}).items():
            if v:
                out[f"task|{ch.get('id')}|{d}"] = str(ch.get("title") or "Aufgabe")
    return out


def _manual(shopping):
    out = {}
    items = (shopping or {}).get("manual") if isinstance(shopping, dict) else None
    for m in items if isinstance(items, list) else []:
        if isinstance(m, dict) and m.get("id"):
            out[f"shop|{m['id']}"] = str(m.get("text") or "Artikel")
    return out


def diff(key, old, new, actor):
    """Vergleicht alten und neuen Stand. Rückgabe: (neue Ereignisse, zurückgenommene Schlüssel)."""
    added, removed = [], []
    if key == "chores":
        a, b = _done_keys(old), _done_keys(new)
        added = [(k, "task", f"{actor} hat „{b[k]}“ erledigt") for k in b if k not in a]
        removed = [k for k in a if k not in b]
    elif key == "shopping":
        a, b = _manual(old), _manual(new)
        added = [(k, "shop", f"{actor} hat „{b[k]}“ auf die Einkaufsliste gesetzt") for k in b if k not in a]
        removed = [k for k in a if k not in b]
    elif key == "plan" and old is not None and old != new:
        added = [("plan|" + actor, "plan", f"{actor} hat den Essensplan geändert")]
    return added, removed


# ---------- Bündeln und Versenden ----------
_PENDING = {}   # family_id -> {"actor": user_id, "events": {key: (kind, text)}, "timer": Timer}
_PLOCK = threading.Lock()
_DB = None       # Funktion, die eine DB-Verbindung liefert (vom Server gesetzt)
_DBLOCK = None


def setup(db_func, db_lock):
    global _DB, _DBLOCK
    _DB, _DBLOCK = db_func, db_lock


def record(family_id, actor_id, added, removed):
    """Merkt Ereignisse vor. Pro Auslöser und Familie wird nach DELAY Sekunden gebündelt verschickt."""
    if not enabled() or not (added or removed):
        return
    slot = (family_id, actor_id)
    with _PLOCK:
        p = _PENDING.get(slot)
        if p is None:
            if not added:
                return
            p = _PENDING[slot] = {"events": {}, "timer": None}
        for k in removed:  # z. B. Haken gleich wieder entfernt: keine Nachricht
            p["events"].pop(k, None)
        for k, kind, text in added:
            p["events"][k] = (kind, text)
        if p["timer"] is None:
            t = threading.Timer(DELAY, _flush, (slot,))
            t.daemon = True
            p["timer"] = t
            t.start()


def _subject(c):
    if CONTACT:
        return CONTACT if CONTACT.startswith(("mailto:", "https://")) else "mailto:" + CONTACT
    row = c.execute("SELECT email FROM users WHERE superadmin=1 ORDER BY created LIMIT 1").fetchone()
    return "mailto:" + row[0] if row else "mailto:admin@example.com"


def _message(events):
    texts = [t for _, t in events]
    if len(texts) == 1:
        kind = events[0][0]
        title = {"task": "✅ Erledigt", "shop": "🛒 Einkaufsliste", "plan": "🍽️ Essensplan"}.get(kind, "Familie")
        return {"title": title, "body": texts[0], "url": {"task": "/#chores", "shop": "/#shop", "plan": "/#week"}.get(kind, "/")}
    body = "\n".join(texts[:4]) + (f"\n… und {len(texts) - 4} weitere" if len(texts) > 4 else "")
    return {"title": f"🏡 {len(texts)} Neuigkeiten", "body": body, "url": "/"}


def _flush(slot):
    family_id, actor_id = slot
    with _PLOCK:
        p = _PENDING.pop(slot, None)
    if not p or not p["events"]:
        return
    events = list(p["events"].values())
    with _DBLOCK, _DB() as c:
        subject = _subject(c)
        rows = c.execute("SELECT s.endpoint, s.user_id, s.p256dh, s.auth FROM push_subs s "
                         "JOIN users u ON u.id = s.user_id AND u.family_id = s.family_id "
                         "WHERE s.family_id=? AND s.user_id<>?", (family_id, actor_id)).fetchall()
        prefs = {uid: get_prefs(c, uid) for uid in {r[1] for r in rows}}
    deliver([{"endpoint": r[0], "user_id": r[1], "p256dh": r[2], "auth": r[3]} for r in rows], events, prefs, subject)


def deliver(subs, events, prefs, subject):
    gone, ok = [], []
    for s in subs:
        mine = [e for e in events if prefs.get(s["user_id"], {}).get(e[0])]
        if not mine:
            continue
        try:
            status = send_one(s, _message(mine), subject)
        except PushError:
            status = 410
        if status in (404, 410):
            gone.append(s["endpoint"])
        elif 200 <= status < 300:
            ok.append(s["endpoint"])
    if gone or ok:
        with _DBLOCK, _DB() as c:
            for ep in gone:
                c.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,))
            for ep in ok:
                c.execute("UPDATE push_subs SET last_ok=datetime('now') WHERE endpoint=?", (ep,))
    return len(ok), len(gone)


def send_test(c, user):
    """Testnachricht an alle Geräte des Nutzers selbst, sofort."""
    rows = c.execute("SELECT endpoint, user_id, p256dh, auth FROM push_subs WHERE user_id=?", (user["id"],)).fetchall()
    if not rows:
        raise PushError("Auf keinem Gerät aktiviert.")
    subj = _subject(c)
    subs = [{"endpoint": r[0], "user_id": r[1], "p256dh": r[2], "auth": r[3]} for r in rows]
    return subs, subj


def send_test_now(subs, subject):
    ok = gone = 0
    for s in subs:
        status = send_one(s, {"title": "🔔 Test", "body": "Benachrichtigungen funktionieren auf diesem Gerät.", "url": "/#profile"}, subject)
        if 200 <= status < 300:
            ok += 1
        elif status in (404, 410):
            gone += 1
            with _DBLOCK, _DB() as c:
                c.execute("DELETE FROM push_subs WHERE endpoint=?", (s["endpoint"],))
    return ok, gone
