// Zwei Geräte gleichzeitig: Haken dürfen sich nicht überschreiben, Änderungen erscheinen ohne Neuladen.
// Aufruf: BASE=http://127.0.0.1:18085 CODE=<Einrichtungscode> node tests/e2e_sync.js
const { chromium } = require(process.env.PLAYWRIGHT || 'playwright');
const BASE = process.env.BASE, CODE = process.env.CODE;
let fails = 0;
const ok = (n, c, x = '') => { console.log((c ? '  ok    ' : '  FEHLER ') + n + (!c && x ? `  [${x}]` : '')); if (!c) fails++; };
(async () => {
  const b = await chromium.launch({ executablePath: '/usr/bin/google-chrome', args: ['--no-sandbox'] });
  const mk = async () => { const p = await (await b.newContext({ viewport: { width: 400, height: 860 } })).newPage(); p.errs = []; p.on('pageerror', e => p.errs.push(e.message)); return p; };
  const a = await mk(), p = await mk();
  await a.goto(BASE); await a.waitForSelector('#authform');
  await a.fill('#code', CODE); await a.fill('#family_name', 'Fam'); await a.fill('#name', 'Ich'); await a.fill('#email', 'ich@example.de');
  await a.fill('#password', 'passwort-12345'); await a.fill('#password2', 'passwort-12345'); await a.click('#authform button[type=submit]');
  await a.waitForSelector('nav button'); await a.waitForTimeout(800);
  const inv = await a.evaluate(async () => (await api('POST', 'api/invites', {})).j.token);
  await p.goto(BASE + '/?invite=' + inv); await p.waitForSelector('#authform');
  await p.fill('#name', 'Partnerin'); await p.fill('#email', 'p@example.de'); await p.fill('#password', 'passwort-12345'); await p.fill('#password2', 'passwort-12345');
  await p.click('#authform button[type=submit]'); await p.waitForSelector('nav button');
  for (const x of [a, p]) { await x.click('nav button[data-t=chores]'); await x.waitForSelector('[data-a=chkTask]'); }
  const boxes = async x => x.locator('[data-a=chkTask]');
  // Aufgaben der laufenden Woche (überfällige verschwinden nach dem Abhaken aus ihrer Liste)
  const ids = await a.evaluate(() => { const mon = iso(monday(new Date())); return [...new Set([...document.querySelectorAll('[data-a=chkTask]')].filter(e => e.dataset.d >= mon && !e.checked).map(e => e.dataset.id + '|' + e.dataset.d))].slice(0, 3); });
  const sel = s => `[data-a=chkTask][data-id="${s.split('|')[0]}"][data-d="${s.split('|')[1]}"]`;
  // gleichzeitig: ich hake Aufgabe 1 ab, sie Aufgabe 2 (ihr Gerät kennt meinen Haken noch nicht)
  await a.click(sel(ids[0])); await p.click(sel(ids[1]));
  await a.waitForTimeout(7000);
  const st = async x => x.evaluate(i => i.map(s => { const [id, d] = s.split('|'); const c = S.chores.find(c => c.id === id); return !!(c.done && c.done[d]); }), ids);
  const sa = await st(a), sp = await st(p);
  ok('Gerät 1 sieht beide Haken', sa[0] && sa[1], JSON.stringify(sa));
  ok('Gerät 2 sieht beide Haken', sp[0] && sp[1], JSON.stringify(sp));
  ok('Haken auch im Bild (Gerät 2)', await p.locator(sel(ids[0])).isChecked());
  const srv = await a.evaluate(async i => { const s = await (await fetch('api/state')).json(); return i.map(x => { const [id, d] = x.split('|'); const c = s.chores.find(c => c.id === id); return !!(c.done && c.done[d]); }); }, ids);
  ok('Server hat beide Haken', srv[0] && srv[1], JSON.stringify(srv));
  // Live: ich hake Aufgabe 3 ab, sie sieht es ohne Neuladen
  await a.click(sel(ids[2]));
  await p.waitForFunction(s => { const e = document.querySelector(s); return e && e.checked; }, sel(ids[2]), { timeout: 9000 }).then(() => ok('Live-Aktualisierung ohne Neuladen', true)).catch(() => ok('Live-Aktualisierung ohne Neuladen', false));
  // Einkauf: beide fügen gleichzeitig eigene Artikel hinzu
  for (const x of [a, p]) await x.click('nav button[data-t=shop]');
  await a.click('[data-a=manualNew]'); await a.fill('#mi', 'Kaffee'); await a.press('#mi', 'Enter');
  await p.click('[data-a=manualNew]'); await p.fill('#mi', 'Tee'); await p.press('#mi', 'Enter');
  await a.waitForTimeout(7000);
  const ma = await a.evaluate(() => S.shopping.manual.map(m => m.text).sort()), mp = await p.evaluate(() => S.shopping.manual.map(m => m.text).sort());
  ok('Beide Artikel auf beiden Geräten', JSON.stringify(ma) === '["Kaffee","Tee"]' && JSON.stringify(mp) === '["Kaffee","Tee"]', JSON.stringify([ma, mp]));
  // Offener Dialog wird nicht unterbrochen
  await p.click('[data-a=manualNew]'); await p.fill('#mi', 'Zuck');
  await a.click('[data-a=manualNew]'); await a.fill('#mi', 'Salz'); await a.press('#mi', 'Enter');
  await p.waitForTimeout(6500);
  ok('Dialog bleibt offen, Eingabe bleibt', await p.isVisible('#mi') && (await p.inputValue('#mi')) === 'Zuck');
  await p.press('#mi', 'Enter'); await p.waitForTimeout(7000);
  const mp2 = await p.evaluate(() => S.shopping.manual.map(m => m.text).sort());
  ok('Nach dem Dialog sind alle Artikel da', JSON.stringify(mp2) === '["Kaffee","Salz","Tee","Zuck"]', JSON.stringify(mp2));
  ok('Keine JS-Fehler', !a.errs.length && !p.errs.length, [...a.errs, ...p.errs].join(' | '));
  await b.close();
  console.log(fails ? `\n${fails} Fehler.` : '\nAlle Zwei-Geräte-Prüfungen bestanden.');
  process.exit(fails ? 1 : 0);
})();
