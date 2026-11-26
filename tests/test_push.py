#!/usr/bin/env python3
"""Prüft push.py: Verschlüsselung (RFC 8291), VAPID-Signatur, Ereigniserkennung, Bündelung.
Gegenprobe mit den unabhängigen Bibliotheken http_ece und py_vapid, falls installiert."""
import base64, json, os, sys, tempfile, time, sqlite3, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import push
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

fails = 0
def ok(name, cond, extra=""):
    global fails
    print(("  ok    " if cond else "  FEHLER ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond: fails += 1

d = tempfile.mkdtemp()
push.load_key(d)
ok("Schlüssel erzeugt", (Path(d) / "vapid_private.pem").is_file())
k1 = push.public_key(); push.load_key(d)
ok("Schlüssel bleibt nach Neustart gleich", push.public_key() == k1)
ok("Öffentlicher Schlüssel 65 Byte", len(push.unb64u(k1)) == 65)

# Browser-Seite simulieren
ua = ec.generate_private_key(ec.SECP256R1())
ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
auth_secret = os.urandom(16)
msg = json.dumps({"title": "✅ Erledigt", "body": "Anna hat „Wäsche“ erledigt"}, ensure_ascii=False).encode()
blob = push.encrypt(msg, push.b64u(ua_pub), push.b64u(auth_secret))
try:
    import http_ece
    plain = http_ece.decrypt(blob, private_key=ua, auth_secret=auth_secret, version="aes128gcm")
    ok("http_ece entschlüsselt die Nachricht", plain == msg)
except ImportError:
    print("  (http_ece nicht installiert, Gegenprobe übersprungen)")

# VAPID-Kopf prüfen
ep = "https://fcm.googleapis.com/fcm/send/abc"
hdr = push._vapid_header(ep, "mailto:test@example.de")
t = hdr.split("t=")[1].split(",")[0]; k = hdr.split("k=")[1]
h, b, s = t.split(".")
claims = json.loads(push.unb64u(b))
ok("aud = Herkunft des Push-Dienstes", claims["aud"] == "https://fcm.googleapis.com")
ok("exp höchstens 24 h", claims["exp"] - time.time() <= 24 * 3600)
ok("sub ohne Leerzeichen", claims["sub"] == "mailto:test@example.de")
sig = push.unb64u(s)
pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), push.unb64u(k))
try:
    pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")), f"{h}.{b}".encode(), ec.ECDSA(hashes.SHA256()))
    ok("Signatur gültig", True)
except Exception as e:
    ok("Signatur gültig", False, e)

# Endpunkte
ok("FCM erlaubt", push.check_endpoint("https://fcm.googleapis.com/fcm/send/x"))
ok("Apple erlaubt", push.check_endpoint("https://web.push.apple.com/abc"))
ok("Mozilla erlaubt", push.check_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x"))
ok("Edge erlaubt", push.check_endpoint("https://wns2-par02p.notify.windows.com/w/?token=x"))
ok("fremde Adresse abgelehnt", not push.check_endpoint("https://evil.example/fcm.googleapis.com"))
ok("ähnlicher Name abgelehnt", not push.check_endpoint("https://evilpush.apple.com.example/x"))
ok("http abgelehnt", not push.check_endpoint("http://fcm.googleapis.com/x"))
ok("interne Adresse abgelehnt", not push.check_endpoint("https://192.168.1.1/x"))

# Ereigniserkennung
old = [{"id": "c1", "title": "Wäsche", "done": {}}]
new = [{"id": "c1", "title": "Wäsche", "done": {"2026-11-26": True}}]
a, r = push.diff("chores", old, new, "Anna")
ok("Aufgabe erledigt erkannt", len(a) == 1 and a[0][1] == "task" and "Wäsche" in a[0][2] and "Anna" in a[0][2])
a, r = push.diff("chores", new, old, "Anna")
ok("Haken entfernt wird zurückgenommen", not a and r == ["task|c1|2026-11-26"])
a, r = push.diff("shopping", {"manual": []}, {"manual": [{"id": "m1", "text": "Milch"}]}, "Ben")
ok("Neuer Einkaufsartikel erkannt", len(a) == 1 and "Milch" in a[0][2])
a, r = push.diff("shopping", {"manual": [{"id": "m1", "text": "Milch", "done": False}]}, {"manual": [{"id": "m1", "text": "Milch", "done": True}]}, "Ben")
ok("Abhaken im Einkauf meldet nichts", not a and not r)
a, r = push.diff("plan", {"x": 1}, {"x": 2}, "Ben")
ok("Essensplan geändert erkannt", len(a) == 1 and a[0][1] == "plan")
a, r = push.diff("chores", "kaputt", None, "X")
ok("Kaputte Daten führen nicht zu Fehlern", a == [] )

# Bündelung + Versand (Versand wird abgefangen)
dbf = Path(d) / "t.db"
def db(): return sqlite3.connect(dbf)
with db() as c:
    c.executescript("CREATE TABLE users(id TEXT, family_id TEXT, email TEXT, superadmin INT, created INT);")
    push.init(c)
    c.executemany("INSERT INTO users VALUES(?,?,?,?,?)", [("ua", "f1", "anna@example.de", 1, 1), ("ub", "f1", "ben@example.de", 0, 2), ("uc", "f2", "c@example.de", 0, 3)])
lock = threading.Lock(); push.setup(db, lock); push.DELAY = 0.3
def sub(endpoint):
    return {"endpoint": endpoint, "keys": {"p256dh": push.b64u(ua_pub), "auth": push.b64u(auth_secret)}}
with db() as c:
    push.subscribe(c, {"id": "ua", "family_id": "f1"}, sub("https://fcm.googleapis.com/a"))
    push.subscribe(c, {"id": "ub", "family_id": "f1"}, sub("https://fcm.googleapis.com/b"))
    push.subscribe(c, {"id": "uc", "family_id": "f2"}, sub("https://fcm.googleapis.com/c"))
    try:
        push.subscribe(c, {"id": "ua", "family_id": "f1"}, sub("https://example.org/x")); ok("fremder Dienst abgelehnt", False)
    except push.PushError:
        ok("fremder Dienst abgelehnt", True)
sent = []
def fake_send(s, payload, subject):
    plain = http_ece.decrypt(push.encrypt(json.dumps(payload).encode(), s["p256dh"], s["auth"]), private_key=ua, auth_secret=auth_secret, version="aes128gcm")
    sent.append((s["endpoint"], json.loads(plain), subject)); return 201
push.send_one = fake_send
push.record("f1", "ua", *push.diff("chores", old, new, "Anna"))
push.record("f1", "ua", *push.diff("shopping", {"manual": []}, {"manual": [{"id": "m1", "text": "Milch"}]}, "Anna"))
time.sleep(0.8)
eps = [e for e, _, _ in sent]
ok("Nur Ben bekommt die Nachricht", eps == ["https://fcm.googleapis.com/b"], eps)
ok("Zwei Ereignisse gebündelt", sent and sent[0][1]["title"].startswith("🏡 2"), sent and sent[0][1])
ok("Absender = Admin-Mail", sent and sent[0][2] == "mailto:anna@example.de")
sent.clear()
push.record("f1", "ua", *push.diff("chores", old, new, "Anna"))
push.record("f1", "ua", *push.diff("chores", new, old, "Anna"))
time.sleep(0.8)
ok("Gleich wieder abgehakt: keine Nachricht", not sent, sent)
with db() as c:
    push.set_prefs(c, "ub", {"task": False})
push.record("f1", "ua", *push.diff("chores", old, new, "Anna"))
time.sleep(0.8)
ok("Abgeschaltete Ereignisse werden nicht gemeldet", not sent, sent)
push.send_one = lambda s, p, subj: 410
with db() as c:
    push.set_prefs(c, "ub", {"task": True})
push.record("f1", "ua", *push.diff("chores", old, new, "Anna"))
time.sleep(0.8)
with db() as c:
    left = [r[0] for r in c.execute("SELECT endpoint FROM push_subs")]
ok("Abgelaufene Abos werden entfernt", "https://fcm.googleapis.com/b" not in left and len(left) == 2, left)

print("\nAlle Push-Prüfungen bestanden." if not fails else f"\n{fails} Fehler.")
sys.exit(1 if fails else 0)
