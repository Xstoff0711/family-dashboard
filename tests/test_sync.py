#!/usr/bin/env python3
"""Prüft den Konfliktschutz beim Speichern (Versionen, If-Match, 409) gegen einen frisch gestarteten Server."""
import json, os, re, subprocess, sys, tempfile, time, urllib.request, urllib.error, http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18190
B = f"http://127.0.0.1:{PORT}/"
fails = 0
def ok(n, c, x=""):
    global fails
    print(("  ok    " if c else "  FEHLER ") + n + (f"  [{x}]" if x and not c else ""))
    if not c: fails += 1

def client():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(m, p, b=None, h=None):
        req = urllib.request.Request(B + p, method=m, data=json.dumps(b).encode() if b is not None else None,
                                     headers={"Content-Type": "application/json", **(h or {})})
        try:
            with op.open(req) as r: return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
    return call

d = tempfile.mkdtemp()
env = dict(os.environ, DB_PATH=f"{d}/f.db", PORT=str(PORT), EVENTS_AUTO="0")
srv = subprocess.Popen([sys.executable, "-u", str(ROOT / "server.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    code = None
    for line in srv.stdout:
        m = re.search(r"Einrichtungscode: ([0-9a-f]+)", line)
        if m: code = m.group(1)
        if "läuft auf Port" in line: break
    a = client(); a("POST", "api/auth/setup", {"code": code, "family_name": "F", "name": "Ich", "email": "i@x.de", "password": "passwort-12345"})
    inv = a("POST", "api/invites", {})[1]["token"]
    b = client(); b("POST", "api/auth/register", {"token": inv, "email": "p@x.de", "name": "Partnerin", "password": "passwort-12345"})

    s, r = a("PUT", "api/chores", [{"id": "c1", "done": {}}], {"If-Match": "0"})
    ok("Erstes Speichern mit Version 0", s == 200 and r.get("ver") == 1, (s, r))
    s, st = b("GET", "api/state")
    ok("Zustand enthält Versionen", st.get("_ver", {}).get("chores") == 1, st.get("_ver"))
    s, v = b("GET", "api/versions")
    ok("Versionsabfrage", v == {"chores": 1}, v)
    s, r = a("PUT", "api/chores", [{"id": "c1", "done": {"d": True}}], {"If-Match": "1"})
    ok("Speichern auf aktuellem Stand", s == 200 and r["ver"] == 2, (s, r))
    s, r = b("PUT", "api/chores", [{"id": "c1", "done": {}}, {"id": "c2"}], {"If-Match": "1"})
    ok("Veralteter Stand wird abgelehnt (409)", s == 409 and r["ver"] == 2 and r["value"][0]["done"] == {"d": True}, (s, r))
    s, st = a("GET", "api/state")
    ok("Abgelehntes hat nichts überschrieben", st["chores"][0]["done"] == {"d": True} and len(st["chores"]) == 1, st["chores"])
    s, r = b("PUT", "api/chores", [{"id": "c1", "done": {"d": True}}, {"id": "c2"}], {"If-Match": "2"})
    ok("Nach Zusammenführen klappt es", s == 200 and r["ver"] == 3, (s, r))
    s, r = a("PUT", "api/chores", [{"id": "x"}])
    ok("Ohne If-Match (alte Oberfläche) weiter möglich", s == 200 and r["ver"] == 4, (s, r))
    s, r = a("PUT", "api/chores", [], {"If-Match": "abc"})
    ok("Ungültige Version abgelehnt", s == 400, s)
    s, r = a("PUT", "api/shopping", {"manual": []}, {"If-Match": "5"})
    ok("Konflikt bei noch nicht vorhandenem Bereich", s == 409 and r["ver"] == 0 and r["value"] is None, (s, r))
    c = client()
    s, r = c("GET", "api/versions")
    ok("Versionen nur angemeldet", s == 401, s)
finally:
    srv.terminate()
print("\nAlle Sync-Prüfungen bestanden." if not fails else f"\n{fails} Fehler.")
sys.exit(1 if fails else 0)
