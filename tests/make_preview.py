#!/usr/bin/env python3
"""Baut aus static/index.html eine eigenständige Vorschau-Seite mit eingebautem Schein-Server
(Testdaten im Speicher). Die echte Oberfläche läuft unverändert; nur die API-Aufrufe werden
im Browser beantwortet. Ausgabe: preview.html (Pfad als Argument)."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
html = (root / "static" / "index.html").read_text(encoding="utf-8")

MOCK = r"""<script>
/* VORSCHAU: Schein-Server im Browser, nur Testdaten, nichts wird gespeichert */
(() => {
  const DB = {users: [], families: [], sessions: null, invites: {}, state: {}, code: 'vorschau1', seq: 1, prefs: {}, subs: new Set()};
  /* Benachrichtigungen simulieren: Service Worker und Push-Dienst gibt es in der Vorschau nicht */
  let pvSub = null;
  const pm = {getSubscription: async () => pvSub, subscribe: async () => (pvSub = {endpoint: 'https://fcm.googleapis.com/vorschau', toJSON: () => ({}), unsubscribe: async () => { pvSub = null; return true; }})};
  const fakeReg = {pushManager: pm};
  try { Object.defineProperty(navigator, 'serviceWorker', {configurable: true, value: {register: async () => fakeReg, ready: Promise.resolve(fakeReg), getRegistration: async () => fakeReg, addEventListener() {}}}); } catch (e) {}
  if (!('PushManager' in window)) window.PushManager = function(){};
  let pvPerm = 'default';
  window.Notification = {get permission(){ return pvPerm; }, requestPermission: async () => (pvPerm = 'granted')};
  window.__pvNotify = (title, body) => {
    const n = document.createElement('div');
    n.style.cssText = 'position:fixed;left:50%;top:36px;transform:translateX(-50%);z-index:100;width:min(92vw,380px);background:rgba(40,40,45,.94);color:#fff;border-radius:16px;padding:10px 14px;font:14px/1.35 system-ui;box-shadow:0 8px 24px rgba(0,0,0,.3)';
    n.innerHTML = '<div style="opacity:.7;font-size:12px">Familie · jetzt</div><b></b><div></div>';
    n.querySelector('b').textContent = title; n.querySelector('div:last-child').textContent = body;
    document.body.append(n); setTimeout(() => n.remove(), 4000);
  };
  const COL = ['#3b82f6','#ec4899','#10b981','#f59e0b','#8b5cf6','#ef4444'];
  const id = p => p + (DB.seq++).toString(16).padStart(10, '0');
  const res = (status, obj, extra) => new Response(JSON.stringify(obj), {status, headers: {'Content-Type': 'application/json'}});
  const err = (s, m) => res(s, {error: 'bad_request', message: m});
  const cur = () => DB.users.find(u => u.id === DB.sessions);
  const payload = u => { const f = DB.families.find(x => x.id === u.family_id);
    return {user: {id:u.id, name:u.name, email:u.email, role:u.role, superadmin:u.superadmin, color:u.color},
            family: {id:f.id, name:f.name, ort:f.ort, plz:f.plz, radius:f.radius,
                     members: DB.users.filter(x => x.family_id === f.id).map(x => ({id:x.id, name:x.name, role:x.role, color:x.color}))}}; };
  function mkUser(fid, email, name, pw, role, sup){
    email = (email || '').trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email)) throw [400, 'Bitte eine gültige E-Mail-Adresse angeben.'];
    if (!(name || '').trim()) throw [400, 'Bitte Name angeben (höchstens 60 Zeichen).'];
    if ((pw || '').length < 10) throw [400, 'Das Passwort braucht mindestens 10 Zeichen.'];
    if (DB.users.some(u => u.email === email)) throw [409, 'Diese E-Mail-Adresse ist bereits registriert.'];
    const n = DB.users.filter(u => u.family_id === fid).length;
    const u = {id: id('u'), family_id: fid, email, name: name.trim(), pw, role, superadmin: !!sup, color: COL[n % COL.length]};
    DB.users.push(u); return u;
  }
  function mkFamily(name){
    if (!(name || '').trim()) throw [400, 'Bitte einen Familiennamen angeben (höchstens 60 Zeichen).'];
    const f = {id: id('f'), name: name.trim(), ort: '', plz: '', radius: 25}; DB.families.push(f); DB.state[f.id] = {}; return f.id;
  }
  async function handle(method, path, body){
    try {
      if (path === 'api/invite/' + path.split('/')[2] && method === 'GET') {
        const inv = DB.invites[path.split('/')[2]];
        if (!inv || inv.used) return err(404, 'Der Einladungslink ist ungültig oder abgelaufen.');
        const f = DB.families.find(x => x.id === inv.fid);
        return res(200, {family_name: f ? f.name : null, new_family: inv.newFamily});
      }
      if (path === 'api/auth/login') {
        const u = DB.users.find(x => x.email === (body.email || '').trim().toLowerCase());
        if (!u || u.pw !== body.password) return err(401, 'E-Mail oder Passwort stimmt nicht.');
        DB.sessions = u.id; return res(200, payload(u));
      }
      if (path === 'api/auth/setup') {
        if (DB.users.length) return err(403, 'Die Einrichtung ist bereits abgeschlossen.');
        if ((body.code || '').trim().toLowerCase() !== DB.code) return err(403, 'Der Einrichtungscode stimmt nicht (steht im Container-Log).');
        const fid = mkFamily(body.family_name); const u = mkUser(fid, body.email, body.name, body.password, 'admin', true);
        DB.sessions = u.id; return res(200, payload(u));
      }
      if (path === 'api/auth/register') {
        const inv = DB.invites[body.token]; if (!inv || inv.used) return err(404, 'Der Einladungslink ist ungültig oder abgelaufen.');
        const fid = inv.newFamily ? mkFamily(body.family_name) : inv.fid;
        const u = mkUser(fid, body.email, body.name, body.password, inv.newFamily ? 'admin' : 'member', false);
        inv.used = true; DB.sessions = u.id; return res(200, payload(u));
      }
      if (path === 'api/auth/logout') { DB.sessions = null; return res(200, {ok: true}); }
      const me = cur();
      if (!me) return res(401, {error: 'auth', setup: DB.users.length === 0});
      if (path === 'api/me' && method === 'GET') return res(200, payload(me));
      if (path === 'api/me' && method === 'POST') { if (body.name != null) me.name = body.name.trim() || me.name; if (body.color) me.color = body.color; return res(200, payload(me)); }
      if (path === 'api/me/password') {
        if (body.old !== me.pw) return err(403, 'Das aktuelle Passwort stimmt nicht.');
        if ((body.new || '').length < 10) return err(400, 'Das Passwort braucht mindestens 10 Zeichen.');
        me.pw = body.new; return res(200, {ok: true});
      }
      if (path === 'api/invites') {
        if (me.role !== 'admin') return err(403, 'Nur Admins können einladen.');
        if (body.new_family && !me.superadmin) return err(403, 'Neue Familien kann nur der Betreiber einladen.');
        const t = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2) + 'abcdef';
        DB.invites[t] = {fid: body.new_family ? null : me.family_id, newFamily: !!body.new_family, used: false};
        window.__lastInvite = t; document.dispatchEvent(new Event('preview-invite'));
        return res(200, {token: t, expires: Date.now() / 1000 + 7 * 86400});
      }
      if (path === 'api/family' && method === 'PUT') {
        if (me.role !== 'admin') return err(403, 'Nur Admins können die Familie ändern.');
        const f = DB.families.find(x => x.id === me.family_id);
        if (body.name != null) { if (!body.name.trim()) return err(400, 'Bitte einen Familiennamen angeben (höchstens 60 Zeichen).'); f.name = body.name.trim(); }
        if (body.ort != null) f.ort = body.ort.trim();
        if (body.plz != null) { if (!/^\d{0,5}$/.test(body.plz.trim())) return err(400, 'Die Postleitzahl besteht aus bis zu 5 Ziffern.'); f.plz = body.plz.trim(); }
        if (body.radius != null) f.radius = body.radius;
        return res(200, payload(me));
      }
      let m = path.match(/^api\/family\/members\/(.+)$/);
      if (m && method === 'DELETE') {
        if (me.role !== 'admin') return err(403, 'Nur Admins können Mitglieder entfernen.');
        const t = DB.users.find(x => x.id === m[1] && x.family_id === me.family_id);
        if (!t) return err(404, 'Mitglied nicht gefunden.');
        if (t.id === me.id) return err(400, 'Du kannst dich nicht selbst entfernen.');
        DB.users = DB.users.filter(x => x !== t); return res(200, {ok: true});
      }
      if (path === 'api/push' && method === 'GET') return res(200, {available: true, key: 'BPreviewKeyPreviewKeyPreviewKeyPreviewKeyPreviewKeyPreviewKeyPreviewKeyPreviewKeyPrevie',
        prefs: DB.prefs[me.id] ||= {task: true, shop: true, shopdone: true, plan: false}, labels: {task: 'Aufgabe erledigt', shop: 'Neuer Artikel auf der Einkaufsliste', shopdone: 'Einkauf abgeschlossen', plan: 'Essensplan geändert'}, devices: DB.subs.has(me.id) ? 1 : 0});
      if (path === 'api/push/subscribe') { DB.subs.add(me.id); return res(200, {prefs: DB.prefs[me.id], devices: 1}); }
      if (path === 'api/push/unsubscribe') { DB.subs.delete(me.id); return res(200, {prefs: DB.prefs[me.id], devices: 0}); }
      if (path === 'api/push/prefs') { Object.assign(DB.prefs[me.id] ||= {}, body.prefs); return res(200, {prefs: DB.prefs[me.id], devices: DB.subs.has(me.id) ? 1 : 0}); }
      if (path === 'api/push/test') { setTimeout(() => window.__pvNotify('🔔 Test', 'Benachrichtigungen funktionieren auf diesem Gerät.'), 600); return res(200, {sent: 1, removed: 0, failed: 0, devices: 1}); }
      if (path === 'api/state') return res(200, DB.state[me.family_id]);
      if (path === 'api/events') return res(200, {enabled: true, auto: false, status: 'idle', updated: null, error: null, items: [], window: null, stale: false});
      if (path === 'api/events/refresh') return res(200, {status: 'idle', recent: true});
      m = path.match(/^api\/(recipes|plan|activities|chores|shopping|meta)$/);
      if (m && method === 'PUT') { DB.state[me.family_id][m[1]] = body; return res(200, {ok: true}); }
      return res(404, {error: 'not found'});
    } catch (e) { if (Array.isArray(e)) return err(e[0], e[1]); throw e; }
  }
  window.fetch = async (u, o = {}) => {
    const path = String(u).split('?')[0];
    if (!path.startsWith('api/')) return res(404, {});
    return handle((o.method || 'GET').toUpperCase(), path, o.body ? JSON.parse(o.body) : {});
  };
  /* kleine Leiste für die Vorschau */
  document.addEventListener('DOMContentLoaded', () => {
    const bar = document.createElement('div');
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99;background:#1f2937;color:#e5e7eb;font:12px/1.4 system-ui;padding:5px 10px;display:flex;gap:10px;align-items:center;flex-wrap:wrap';
    bar.innerHTML = '<b>Vorschau mit Testdaten</b><span id="pvhint"></span><span style="flex:1"></span><button id="pvinv" hidden style="font:inherit">Einladungslink öffnen</button><button id="pvreset" style="font:inherit">Zurücksetzen</button>';
    document.body.prepend(bar); document.body.style.paddingTop = '28px';
    const hint = () => { document.getElementById('pvhint').textContent = DB.users.length ? '' : 'Einrichtungscode: ' + DB.code; };
    hint();
    document.addEventListener('preview-invite', () => { document.getElementById('pvinv').hidden = false; });
    document.getElementById('pvinv').onclick = async () => { window.__INVITE = window.__lastInvite; DB.sessions = null; document.getElementById('pvinv').hidden = true; await boot(); };
    document.getElementById('pvreset').onclick = () => { DB.users = []; DB.families = []; DB.sessions = null; DB.invites = {}; DB.state = {}; window.__INVITE = null; hint(); boot(); };
    setInterval(hint, 800);
  });
})();
</script>
"""

html = html.replace("<script>\n'use strict';", MOCK + "<script>\n'use strict';", 1)
html = html.replace("new URLSearchParams(location.search).get('invite')", "(window.__INVITE ?? new URLSearchParams(location.search).get('invite'))")
html = html.replace("location.href = location.pathname; },", "window.__INVITE = null; showAuth({mode:'login'}); },")
html = html.replace("<title>Familie</title>", "<title>Familiendashboard v18 – Vorschau</title>")
assert "VORSCHAU" in html and "window.__INVITE" in html
Path(sys.argv[1]).write_text(html, encoding="utf-8")
