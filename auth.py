"""Anmeldung, Familien, Sitzungen und Einladungen (nur Standardbibliothek).

Alle Funktionen bekommen eine offene SQLite-Verbindung `c` und werfen bei
Fehleingaben `AuthError` mit einer verständlichen deutschen Meldung.
"""
import hashlib
import hmac
import re
import secrets
import time

SESSION_DAYS = 30
INVITE_DAYS = 7
MIN_PW = 10
MAX_PW = 200
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,200}\.[^@\s]{2,}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLORS = ["#3b82f6", "#ec4899", "#10b981", "#f59e0b", "#8b5cf6", "#ef4444"]
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ort TEXT NOT NULL DEFAULT '',
    plz TEXT NOT NULL DEFAULT '',
    radius INTEGER NOT NULL DEFAULT 25,
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL REFERENCES families(id),
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    pw TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    superadmin INTEGER NOT NULL DEFAULT 0,
    color TEXT NOT NULL DEFAULT '#3b82f6',
    created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created INTEGER NOT NULL,
    expires INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    family_id TEXT REFERENCES families(id),
    new_family INTEGER NOT NULL DEFAULT 0,
    role TEXT NOT NULL DEFAULT 'member',
    created_by TEXT NOT NULL,
    created INTEGER NOT NULL,
    expires INTEGER NOT NULL,
    used_by TEXT
);
CREATE TABLE IF NOT EXISTS fkv (
    family_id TEXT NOT NULL REFERENCES families(id),
    k TEXT NOT NULL,
    v TEXT NOT NULL,
    updated TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (family_id, k)
);
"""


class AuthError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now():
    return int(time.time())


def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${h.hex()}"


def verify_pw(pw, stored):
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(calc, bytes.fromhex(h))
    except (ValueError, TypeError):
        return False


_DUMMY = hash_pw("dummy-passwort-fuer-gleiche-laufzeit")


def init(c):
    c.executescript(SCHEMA)


def user_count(c):
    return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _check_pw(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PW:
        raise AuthError(f"Das Passwort braucht mindestens {MIN_PW} Zeichen.")
    if len(pw) > MAX_PW:
        raise AuthError("Das Passwort ist zu lang.")


def _check_name(name, what="Name"):
    name = (name or "").strip() if isinstance(name, str) else ""
    if not 1 <= len(name) <= 60:
        raise AuthError(f"Bitte {what} angeben (höchstens 60 Zeichen).")
    return name


def _check_email(email):
    email = (email or "").strip().lower() if isinstance(email, str) else ""
    if not EMAIL_RE.match(email):
        raise AuthError("Bitte eine gültige E-Mail-Adresse angeben.")
    return email


def create_family(c, name):
    fid = "f" + secrets.token_hex(5)
    c.execute("INSERT INTO families(id, name, created) VALUES(?,?,?)", (fid, _check_name(name, "einen Familiennamen"), now()))
    return fid


def create_user(c, family_id, email, name, pw, role="member", superadmin=False):
    email = _check_email(email)
    name = _check_name(name)
    _check_pw(pw)
    if role not in ("admin", "member"):
        raise AuthError("Ungültige Rolle.")
    if c.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
        raise AuthError("Diese E-Mail-Adresse ist bereits registriert.", 409)
    n = c.execute("SELECT COUNT(*) FROM users WHERE family_id=?", (family_id,)).fetchone()[0]
    uid = "u" + secrets.token_hex(5)
    c.execute(
        "INSERT INTO users(id, family_id, email, name, pw, role, superadmin, color, created) VALUES(?,?,?,?,?,?,?,?,?)",
        (uid, family_id, email, name, hash_pw(pw), role, 1 if superadmin else 0, DEFAULT_COLORS[n % len(DEFAULT_COLORS)], now()),
    )
    return uid


def login(c, email, pw):
    """Gibt die User-ID zurück oder None. Gleiche Laufzeit, ob die E-Mail existiert oder nicht."""
    email = (email or "").strip().lower() if isinstance(email, str) else ""
    row = c.execute("SELECT id, pw FROM users WHERE email=?", (email,)).fetchone()
    ok = verify_pw(pw if isinstance(pw, str) else "", row[1] if row else _DUMMY)
    return row[0] if (row and ok) else None


def new_session(c, user_id):
    token = secrets.token_urlsafe(32)
    t = now()
    c.execute("DELETE FROM sessions WHERE expires < ?", (t,))
    c.execute("INSERT INTO sessions(token_hash, user_id, created, expires) VALUES(?,?,?,?)",
              (_hash_token(token), user_id, t, t + SESSION_DAYS * 86400))
    return token


def end_session(c, token):
    c.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token),))


def _user_row(row):
    return {"id": row[0], "family_id": row[1], "email": row[2], "name": row[3], "role": row[4],
            "superadmin": bool(row[5]), "color": row[6], "family_name": row[7]}


_USER_SQL = ("SELECT u.id, u.family_id, u.email, u.name, u.role, u.superadmin, u.color, f.name "
             "FROM users u JOIN families f ON f.id = u.family_id ")


def session_user(c, token):
    if not token:
        return None
    t = now()
    row = c.execute(
        _USER_SQL + "JOIN sessions s ON s.user_id = u.id WHERE s.token_hash=? AND s.expires > ?",
        (_hash_token(token), t),
    ).fetchone()
    if not row:
        return None
    # Sitzung bei Nutzung verlängern, aber höchstens einmal am Tag schreiben
    c.execute("UPDATE sessions SET expires=? WHERE token_hash=? AND expires < ?",
              (t + SESSION_DAYS * 86400, _hash_token(token), t + (SESSION_DAYS - 1) * 86400))
    return _user_row(row)


def family_info(c, family_id):
    row = c.execute("SELECT id, name, ort, plz, radius FROM families WHERE id=?", (family_id,)).fetchone()
    members = c.execute("SELECT id, name, role, color FROM users WHERE family_id=? ORDER BY created", (family_id,)).fetchall()
    return {"id": row[0], "name": row[1], "ort": row[2], "plz": row[3], "radius": row[4],
            "members": [{"id": m[0], "name": m[1], "role": m[2], "color": m[3]} for m in members]}


def update_family(c, family_id, name=None, ort=None, plz=None, radius=None):
    if name is not None:
        c.execute("UPDATE families SET name=? WHERE id=?", (_check_name(name, "einen Familiennamen"), family_id))
    if ort is not None:
        if not isinstance(ort, str) or len(ort.strip()) > 80:
            raise AuthError("Der Ort ist zu lang.")
        c.execute("UPDATE families SET ort=? WHERE id=?", (ort.strip(), family_id))
    if plz is not None:
        if not isinstance(plz, str) or not re.fullmatch(r"\d{0,5}", plz.strip()):
            raise AuthError("Die Postleitzahl besteht aus bis zu 5 Ziffern.")
        c.execute("UPDATE families SET plz=? WHERE id=?", (plz.strip(), family_id))
    if radius is not None:
        if not isinstance(radius, int) or isinstance(radius, bool) or not 1 <= radius <= 200:
            raise AuthError("Der Umkreis liegt zwischen 1 und 200 km.")
        c.execute("UPDATE families SET radius=? WHERE id=?", (radius, family_id))


def update_profile(c, user_id, name=None, color=None):
    if name is not None:
        c.execute("UPDATE users SET name=? WHERE id=?", (_check_name(name), user_id))
    if color is not None:
        if not isinstance(color, str) or not COLOR_RE.match(color):
            raise AuthError("Ungültige Farbe.")
        c.execute("UPDATE users SET color=? WHERE id=?", (color.lower(), user_id))


def change_password(c, user_id, old, new, keep_token=None):
    row = c.execute("SELECT pw FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not verify_pw(old if isinstance(old, str) else "", row[0]):
        raise AuthError("Das aktuelle Passwort stimmt nicht.", 403)
    _check_pw(new)
    c.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(new), user_id))
    # alle anderen Geräte abmelden
    keep = _hash_token(keep_token) if keep_token else ""
    c.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, keep))


def remove_member(c, admin, member_id):
    row = c.execute("SELECT family_id, role FROM users WHERE id=?", (member_id,)).fetchone()
    if not row or row[0] != admin["family_id"]:
        raise AuthError("Mitglied nicht gefunden.", 404)
    if member_id == admin["id"]:
        raise AuthError("Du kannst dich nicht selbst entfernen.")
    c.execute("DELETE FROM sessions WHERE user_id=?", (member_id,))
    c.execute("DELETE FROM users WHERE id=?", (member_id,))


def create_invite(c, user, new_family=False, role="member"):
    if role not in ("admin", "member"):
        raise AuthError("Ungültige Rolle.")
    if user["role"] != "admin":
        raise AuthError("Nur Admins können einladen.", 403)
    if new_family and not user["superadmin"]:
        raise AuthError("Neue Familien kann nur der Betreiber einladen.", 403)
    token = secrets.token_urlsafe(24)
    t = now()
    c.execute(
        "INSERT INTO invites(token_hash, family_id, new_family, role, created_by, created, expires) VALUES(?,?,?,?,?,?,?)",
        (_hash_token(token), None if new_family else user["family_id"], 1 if new_family else 0,
         "admin" if new_family else role, user["id"], t, t + INVITE_DAYS * 86400),
    )
    return token, t + INVITE_DAYS * 86400


def invite_info(c, token):
    """Gibt (family_name | None, new_family, role) zurück oder None, wenn ungültig/abgelaufen/benutzt."""
    if not isinstance(token, str) or not token:
        return None
    row = c.execute(
        "SELECT i.family_id, i.new_family, i.role, f.name FROM invites i LEFT JOIN families f ON f.id = i.family_id "
        "WHERE i.token_hash=? AND i.used_by IS NULL AND i.expires > ?",
        (_hash_token(token), now()),
    ).fetchone()
    if not row:
        return None
    return {"family_id": row[0], "new_family": bool(row[1]), "role": row[2], "family_name": row[3]}


def register(c, token, email, name, pw, family_name=None):
    inv = invite_info(c, token)
    if not inv:
        raise AuthError("Der Einladungslink ist ungültig oder abgelaufen.", 404)
    fid = inv["family_id"]
    if inv["new_family"]:
        fid = create_family(c, family_name or "")
    uid = create_user(c, fid, email, name, pw, role=inv["role"])
    c.execute("UPDATE invites SET used_by=? WHERE token_hash=?", (uid, _hash_token(token)))
    return uid
