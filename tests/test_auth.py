#!/usr/bin/env python3
"""Integrationstest: startet den Server mit einer Test-Datenbank im v14-Format und prüft
Einrichtung, Migration, Anmeldung, Einladungen und vor allem die Trennung der Familien.

Aufruf:  python3 tests/test_auth.py
"""
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18080
JPEG_A = b"\xff\xd8\xff" + b"A" * 50
JPEG_B = b"\xff\xd8\xff" + b"B" * 50
PW = "ein-gutes-passwort-1"

failures = []


def check(name, cond, extra=""):
    print(("  ok    " if cond else "  FEHLER ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        failures.append(name)


class Client:
    def __init__(self, ip=None):
        self.cookie = None
        self.ip = ip

    def req(self, method, path, body=None, raw=None, headers=None):
        h = dict(headers or {})
        if self.cookie:
            h["Cookie"] = self.cookie
        if self.ip:
            h["X-Forwarded-For"] = self.ip
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        conn.request(method, path, body=data, headers=h)
        r = conn.getresponse()
        payload = r.read()
        sc = r.getheader("Set-Cookie")
        if sc:
            val = sc.split(";")[0]
            self.cookie = None if val.endswith("=") else val
        try:
            obj = json.loads(payload) if payload and r.getheader("Content-Type", "").startswith("application/json") else payload
        except ValueError:
            obj = payload
        conn.close()
        return r.status, obj, r


def main():
    tmp = Path(tempfile.mkdtemp(prefix="fd-test-"))
    db_path = tmp / "familie.db"
    photos = tmp / "photos"
    photos.mkdir()
    # --- Altbestand im v14-Format ---
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated TEXT NOT NULL DEFAULT (datetime('now')))")
    legacy = {"recipes": [{"id": "abc123", "name": "Altes Rezept"}], "plan": {"x": 1}, "meta": {"cities": ["Krefeld"]}}
    for k, v in legacy.items():
        con.execute("INSERT INTO kv(k, v) VALUES(?,?)", (k, json.dumps(v)))
    con.execute("INSERT INTO kv(k, v) VALUES('events', ?)", (json.dumps({"updated": None, "items": [], "window": None}),))
    con.commit()
    con.close()
    (photos / "abc123.jpg").write_bytes(JPEG_A)

    env = dict(os.environ, DB_PATH=str(db_path), PHOTO_DIR=str(photos), PORT=str(PORT), EVENTS_AUTO="0")
    proc = subprocess.Popen([sys.executable, "-u", str(ROOT / "server.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    code = None
    try:
        for _ in range(100):
            line = proc.stdout.readline()
            m = re.search(r"Einrichtungscode: (\w+)", line)
            if m:
                code = m.group(1)
            if "läuft auf Port" in line:
                break
        check("Server startet und nennt Einrichtungscode", code is not None)

        anon = Client()
        print("\n1) Ohne Anmeldung")
        for path in ("/api/state", "/api/me", "/api/events", "/photos/abc123.jpg"):
            s, b, _ = anon.req("GET", path)
            check(f"GET {path} -> 401", s == 401, s)
        check("401 meldet, dass Einrichtung offen ist", b.get("setup") is True)
        s, _, _ = anon.req("PUT", "/api/recipes", body=[])
        check("PUT /api/recipes -> 401", s == 401)
        s, _, _ = anon.req("POST", "/api/import-url", body={"url": "http://127.0.0.1"})
        check("import-url -> 401 (kein offener Proxy)", s == 401)
        s, _, _ = anon.req("GET", "/")
        check("Oberfläche (Login-Seite) bleibt erreichbar", s == 200)
        s, _, _ = anon.req("GET", "/healthz")
        check("healthz erreichbar", s == 200)

        print("\n2) Einrichtung und Migration")
        a = Client()
        s, b, _ = a.req("POST", "/api/auth/setup", body={"code": "falsch", "email": "a@example.de", "name": "Anna",
                                                         "password": PW, "family_name": "Familie A"})
        check("falscher Code -> 403", s == 403, s)
        s, b, r = a.req("POST", "/api/auth/setup", body={"code": code, "email": "A@Example.de", "name": "Anna",
                                                         "password": "kurz", "family_name": "Familie A"})
        check("zu kurzes Passwort -> 400", s == 400, s)
        s, b, r = a.req("POST", "/api/auth/setup", body={"code": code, "email": "A@Example.de", "name": "Anna",
                                                         "password": PW, "family_name": "Familie A"})
        check("Einrichtung mit richtigem Code -> 200", s == 200, (s, b))
        sc = r.getheader("Set-Cookie") or ""
        check("Cookie ist HttpOnly + SameSite=Lax", "HttpOnly" in sc and "SameSite=Lax" in sc, sc)
        check("Cookie ohne Secure bei http", "Secure" not in sc)
        check("Betreiber ist Admin + Superadmin", b["user"]["role"] == "admin" and b["user"]["superadmin"] is True)
        check("E-Mail wird klein gespeichert", b["user"]["email"] == "a@example.de")
        s, st, _ = a.req("GET", "/api/state")
        check("Altdaten sind in Familie A", st.get("recipes") == legacy["recipes"] and st.get("plan") == legacy["plan"], st)
        check("meta (Orte) übernommen", st.get("meta") == legacy["meta"])
        check("Events nicht im Familien-State", "events" not in st)
        s, ph, _ = a.req("GET", "/photos/abc123.jpg")
        check("Altes Foto für Familie A abrufbar", s == 200 and ph == JPEG_A, s)
        check("Backup der Datenbank angelegt", len(list(tmp.glob("familie.db.vor-auth-*"))) == 1)
        check("Originalfoto bleibt liegen (Rückweg auf v14)", (photos / "abc123.jpg").exists())
        s, b, _ = Client().req("POST", "/api/auth/setup", body={"code": code, "email": "x@example.de", "name": "X",
                                                                "password": PW, "family_name": "X"})
        check("zweite Einrichtung -> 403", s == 403, s)
        s, b, _ = anon.req("GET", "/api/me")
        check("danach meldet 401 keine offene Einrichtung mehr", b.get("setup") is False)

        print("\n3) Einladung in dieselbe Familie")
        s, b, _ = a.req("POST", "/api/invites", body={"role": "member"})
        check("Admin erzeugt Einladung", s == 200 and b.get("token"), (s, b))
        tok = b["token"]
        s, b, _ = anon.req("GET", f"/api/invite/{tok}")
        check("Einladung ist öffentlich prüfbar (Familienname)", s == 200 and b["family_name"] == "Familie A", (s, b))
        a2 = Client()
        s, b, _ = a2.req("POST", "/api/auth/register", body={"token": tok, "email": "partner@example.de", "name": "Paula", "password": PW})
        check("Registrierung mit Einladung", s == 200 and b["family"]["name"] == "Familie A", (s, b))
        check("neues Mitglied ist kein Admin", b["user"]["role"] == "member")
        check("Familie zeigt beide Mitglieder", len(b["family"]["members"]) == 2)
        s, b, _ = Client().req("POST", "/api/auth/register", body={"token": tok, "email": "z@example.de", "name": "Z", "password": PW})
        check("Einladung nur einmal nutzbar -> 404", s == 404, s)
        s, st, _ = a2.req("GET", "/api/state")
        check("Mitglied sieht die Daten der Familie", st.get("recipes") == legacy["recipes"])
        s, b, _ = a2.req("POST", "/api/invites", body={})
        check("Mitglied darf nicht einladen -> 403", s == 403, s)
        s, b, _ = a2.req("PUT", "/api/family", body={"name": "Hack"})
        check("Mitglied darf Familie nicht ändern -> 403", s == 403, s)
        s, b, _ = a.req("POST", "/api/invites", body={"role": "member"})
        s2, b2, _ = Client().req("POST", "/api/auth/register", body={"token": b["token"], "email": "PARTNER@example.de", "name": "Dup", "password": PW})
        check("doppelte E-Mail (Groß/Klein egal) -> 409", s2 == 409, s2)

        print("\n4) Neue Familie und Mandantentrennung")
        s, b, _ = a2.req("POST", "/api/invites", body={"new_family": True})
        check("Nur Superadmin darf Familien einladen (Mitglied -> 403)", s == 403)
        s, b, _ = a.req("POST", "/api/invites", body={"new_family": True})
        check("Superadmin erzeugt Familien-Einladung", s == 200, (s, b))
        s, info, _ = anon.req("GET", f"/api/invite/{b['token']}")
        check("Einladung zeigt 'neue Familie'", info["new_family"] is True and info["family_name"] is None)
        bcl = Client()
        s, bb, _ = bcl.req("POST", "/api/auth/register", body={"token": b["token"], "email": "bruder@example.de", "name": "Ben",
                                                               "password": PW, "family_name": "Familie B"})
        check("Familie B angelegt, Ben ist Admin", s == 200 and bb["user"]["role"] == "admin" and bb["family"]["name"] == "Familie B", (s, bb))
        check("Ben ist kein Superadmin", bb["user"]["superadmin"] is False)
        check("Familie B hat nur Ben", [m["name"] for m in bb["family"]["members"]] == ["Ben"])
        s, st, _ = bcl.req("GET", "/api/state")
        check("Familie B sieht KEINE Daten von A", st == {"_ver": {}}, st)
        s, vv, _ = bcl.req("GET", "/api/versions")
        check("Familie B sieht keine Versionen von A", vv == {}, vv)
        s, ph, _ = bcl.req("GET", "/photos/abc123.jpg")
        check("Familie B kann Foto von A nicht abrufen -> 404", s == 404, s)
        s, _, _ = bcl.req("PUT", "/api/recipes", body=[{"id": "zzz", "name": "Bens Rezept"}])
        check("Familie B speichert eigene Daten", s == 200)
        s, st, _ = a.req("GET", "/api/state")
        check("Daten von A unverändert", st.get("recipes") == legacy["recipes"], st.get("recipes"))
        s, _, _ = bcl.req("POST", "/api/photo/abc123", raw=JPEG_B)
        check("Familie B lädt Foto mit gleicher ID hoch", s == 200)
        s, ph, _ = a.req("GET", "/photos/abc123.jpg")
        check("Foto von A bleibt unverändert", ph == JPEG_A)
        s, ph, _ = bcl.req("GET", "/photos/abc123.jpg")
        check("Foto von B ist das eigene", ph == JPEG_B)
        s, _, _ = bcl.req("DELETE", "/api/photo/abc123")
        s, ph, _ = a.req("GET", "/photos/abc123.jpg")
        check("Löschen bei B lässt Foto von A stehen", s == 200 and ph == JPEG_A)
        for path in ("/photos/../familie.db", "/photos/abc123.jpg/../../x.jpg", "/photos/%2e%2e/familie.db"):
            s, _, _ = bcl.req("GET", path)
            check(f"Pfadtrick {path} -> 404", s == 404, s)
        s, b, _ = bcl.req("DELETE", f"/api/family/members/{a2_id(a)}")
        check("Admin B kann Mitglied aus Familie A nicht entfernen -> 404", s == 404, (s, b))
        s, b, _ = bcl.req("PUT", "/api/family", body={"name": "Familie B", "ort": "Hamburg", "plz": "20095", "radius": 30})
        check("Admin B setzt Standort", s == 200 and b["family"]["ort"] == "Hamburg" and b["family"]["radius"] == 30, (s, b))
        s, b, _ = a.req("GET", "/api/me")
        check("Standort von A unberührt", b["family"]["ort"] == "")
        s, b, _ = bcl.req("PUT", "/api/family", body={"plz": "ABC"})
        check("ungültige PLZ -> 400", s == 400, s)

        print("\n5) Anmeldung, Passwort, Abmeldung")
        c = Client()
        s, b, _ = c.req("POST", "/api/auth/login", body={"email": "bruder@example.de", "password": "falsch"})
        check("falsches Passwort -> 401", s == 401, s)
        s, b, _ = c.req("POST", "/api/auth/login", body={"email": "gibtsnicht@example.de", "password": PW})
        check("unbekannte E-Mail -> 401 (gleiche Meldung)", s == 401 and b["message"] == "E-Mail oder Passwort stimmt nicht.", (s, b))
        s, b, r = c.req("POST", "/api/auth/login", body={"email": "Bruder@Example.de", "password": PW},
                        headers={"X-Forwarded-Proto": "https"})
        check("Anmeldung klappt", s == 200 and b["user"]["name"] == "Ben", (s, b))
        check("Cookie mit Secure hinter HTTPS-Proxy", "Secure" in (r.getheader("Set-Cookie") or ""))
        s, _, _ = bcl.req("POST", "/api/me/password", body={"old": "falsch", "new": "neues-passwort-22"})
        check("Passwortwechsel mit falschem alten Passwort -> 403", s == 403, s)
        s, _, _ = bcl.req("POST", "/api/me/password", body={"old": PW, "new": "kurz"})
        check("zu kurzes neues Passwort -> 400", s == 400, s)
        s, _, _ = bcl.req("POST", "/api/me/password", body={"old": PW, "new": "neues-passwort-22"})
        check("Passwort ändern", s == 200, s)
        s, _, _ = c.req("GET", "/api/me")
        check("anderes Gerät wird danach abgemeldet", s == 401, s)
        s, _, _ = bcl.req("GET", "/api/me")
        check("aktuelles Gerät bleibt angemeldet", s == 200, s)
        c2 = Client()
        s, _, _ = c2.req("POST", "/api/auth/login", body={"email": "bruder@example.de", "password": PW})
        check("altes Passwort funktioniert nicht mehr", s == 401)
        s, _, _ = c2.req("POST", "/api/auth/login", body={"email": "bruder@example.de", "password": "neues-passwort-22"})
        check("neues Passwort funktioniert", s == 200)
        s, b, _ = c2.req("POST", "/api/me", body={"name": "Benjamin", "color": "#112233"})
        check("Profil ändern (Name, Farbe)", s == 200 and b["user"]["name"] == "Benjamin" and b["user"]["color"] == "#112233", (s, b))
        s, _, _ = c2.req("POST", "/api/me", body={"color": "rot; evil"})
        check("ungültige Farbe -> 400", s == 400)
        s, _, _ = c2.req("POST", "/api/auth/logout")
        s, _, _ = c2.req("GET", "/api/me")
        check("nach Abmeldung -> 401", s == 401)

        print("\n6) Schutz vor Missbrauch")
        s, _, _ = a.req("PUT", "/api/recipes", body=[], headers={"Origin": "https://boese.example"})
        check("fremde Origin bei PUT -> 403", s == 403, s)
        s, _, _ = a.req("POST", "/api/me", body={"name": "X"}, headers={"Origin": "https://boese.example"})
        check("fremde Origin bei POST -> 403", s == 403, s)
        s, _, _ = a.req("PUT", "/api/recipes", body=legacy["recipes"], headers={"Origin": "http://127.0.0.1:%d" % PORT})
        check("gleiche Origin erlaubt", s == 200, s)
        s, _, _ = a.req("PUT", "/api/unbekannt", body={})
        check("unbekannter Schlüssel -> 404", s == 404)
        s, _, _ = a.req("PUT", "/api/events", body={"items": ["manipuliert"]})
        check("Events lassen sich nicht überschreiben", s == 404)
        s, _, _ = a.req("PUT", "/api/family", body={"radius": 9999})
        check("Umkreis außerhalb 1-200 -> 400", s == 400)

        print("\n7) Mitglied entfernen, abgelaufene Einladung")
        s, me, _ = a.req("GET", "/api/me")
        pid = [m["id"] for m in me["family"]["members"] if m["name"] == "Paula"][0]
        s, _, _ = a.req("DELETE", f"/api/family/members/{pid}")
        check("Admin entfernt Mitglied", s == 200, s)
        s, _, _ = a2.req("GET", "/api/me")
        check("entferntes Mitglied ist sofort abgemeldet", s == 401, s)
        s, b, _ = a.req("DELETE", f"/api/family/members/{me['user']['id']}")
        check("sich selbst entfernen -> 400", s == 400, s)
        s, b, _ = a.req("POST", "/api/invites", body={})
        con = sqlite3.connect(db_path)
        con.execute("UPDATE invites SET expires = 1")
        con.commit()
        con.close()
        s, _, _ = anon.req("GET", f"/api/invite/{b['token']}")
        check("abgelaufene Einladung -> 404", s == 404)
        con = sqlite3.connect(db_path)
        stored = con.execute("SELECT pw FROM users LIMIT 1").fetchone()[0]
        toks = [r[0] for r in con.execute("SELECT token_hash FROM sessions")]
        con.close()
        check("Passwörter als scrypt gespeichert", stored.startswith("scrypt$"))
        check("Sitzungs-Tokens nur als Hash gespeichert", all(len(t) == 64 for t in toks) and a.cookie.split("=")[1] not in toks)

        print("\n8) Bremse bei Fehlversuchen (eigene IP)")
        rl = Client(ip="10.9.9.9")
        codes = [rl.req("POST", "/api/auth/login", body={"email": "a@example.de", "password": "falsch"})[0] for _ in range(12)]
        check("nach 10 Fehlversuchen -> 429", codes[:10] == [401] * 10 and codes[10] == 429, codes)
        s, _, _ = rl.req("POST", "/api/auth/login", body={"email": "a@example.de", "password": PW})
        check("auch richtiges Passwort wird dann kurz gesperrt", s == 429)
        s, _, _ = Client(ip="10.8.8.8").req("POST", "/api/auth/login", body={"email": "a@example.de", "password": PW})
        check("andere IP nicht betroffen", s == 200)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if failures:
        print(f"{len(failures)} FEHLER:", *failures, sep="\n  - ")
        sys.exit(1)
    print("Alle Prüfungen bestanden.")


def a2_id(client):
    s, me, _ = client.req("GET", "/api/me")
    return [m["id"] for m in me["family"]["members"] if m["role"] == "admin"][0]


if __name__ == "__main__":
    main()
