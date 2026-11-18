// Oberflächentest im Headless-Browser gegen einen laufenden Server.
// Aufruf: BASE=http://127.0.0.1:18081 CODE=<Einrichtungscode> OUT=/tmp/shots node tests/e2e_ui.js
const { chromium } = require(process.env.PLAYWRIGHT || 'playwright');
const BASE = process.env.BASE, CODE = process.env.CODE, OUT = process.env.OUT || '/tmp/shots';
require('fs').mkdirSync(OUT, { recursive: true });
let fails = 0;
const ok = (n, c, x = '') => { console.log((c ? '  ok    ' : '  FEHLER ') + n + (!c && x ? `  [${x}]` : '')); if (!c) fails++; };

(async () => {
  const browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', args: ['--no-sandbox'] });
  const mk = async () => {
    const ctx = await browser.newContext({ viewport: { width: 420, height: 860 }, locale: 'de-DE' });
    const page = await ctx.newPage();
    page.errors = [];
    page.on('pageerror', e => page.errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error' && !/401|403|404|409/.test(m.text())) page.errors.push(m.text()); });
    return page;
  };
  const text = (p, sel) => p.locator(sel).innerText();

  // --- Einrichtung ---
  let p = await mk();
  await p.goto(BASE);
  await p.waitForSelector('#authform');
  ok('Einrichtung wird angezeigt', (await text(p, '#auth h1')).includes('Willkommen'));
  await p.screenshot({ path: `${OUT}/1-setup.png` });
  await p.fill('#code', CODE); await p.fill('#family_name', 'Familie Test'); await p.fill('#name', 'Anna');
  await p.fill('#email', 'anna@example.de'); await p.fill('#password', 'passwort-12345'); await p.fill('#password2', 'anders-12345');
  await p.click('#authform button[type=submit]');
  ok('ungleiche Passwörter werden abgefangen', (await text(p, '#aerr')).includes('stimmen nicht'));
  await p.fill('#password2', 'passwort-12345');
  await p.click('#authform button[type=submit]');
  await p.waitForSelector('#auth', { state: 'hidden' });
  await p.waitForSelector('nav button');
  ok('nach Einrichtung ist die App sichtbar', await p.locator('#nav button').count() === 6);
  ok('Header zeigt Familienname', (await text(p, '#fam')) === 'Familie Test');
  ok('Avatar zeigt Initial', (await text(p, '#me')) === 'A');
  ok('Altdaten/Startrezepte geladen (Tab Essen)', (await (async () => { await p.click('#nav button:nth-child(2)'); return await text(p, '#view'); })()).length > 50);
  await p.screenshot({ path: `${OUT}/2-app.png` });

  // --- Profil ---
  await p.click('#me');
  await p.waitForSelector('[data-form=profile]');
  ok('Profilseite zeigt E-Mail', (await text(p, '#view')).includes('anna@example.de'));
  await p.fill('#pn', 'Anna Beispiel');
  await p.click('.sw button:nth-child(3)');
  await p.click('[data-form=profile] button[type=submit]');
  await p.waitForFunction(() => [...document.querySelectorAll('.mem b')].some(b => b.textContent === 'Anna Beispiel'));
  ok('Name gespeichert (Mitgliederliste)', true);
  const col = await p.evaluate(() => document.querySelector('#me').style.background);
  ok('Farbe übernommen (Avatar)', /16, 185, 129|#10b981/i.test(col), col);
  await p.fill('#fn', 'Die Testers'); await p.click('[data-form=famname] button');
  await p.waitForFunction(() => document.querySelector('#fam').textContent === 'Die Testers');
  ok('Familienname im Header aktualisiert', true);
  await p.fill('#lo', 'Krefeld'); await p.fill('#lp', '47798'); await p.selectOption('#lr', '40');
  await p.click('[data-form=loc] button[type=submit]');
  await p.waitForTimeout(400);
  await p.reload(); await p.waitForSelector('nav button'); await p.click('#me'); await p.waitForSelector('#lo');
  ok('Standort bleibt nach Neuladen', (await p.inputValue('#lo')) === 'Krefeld' && (await p.inputValue('#lp')) === '47798' && (await p.inputValue('#lr')) === '40');
  await p.fill('#pwo', 'falsch-falsch'); await p.fill('#pwn', 'neues-passwort-1'); await p.fill('#pwn2', 'neues-passwort-1');
  await p.click('[data-form=pw] button[type=submit]');
  await p.waitForFunction(() => document.querySelector('#perr2').textContent.length > 0);
  ok('falsches altes Passwort gemeldet', (await text(p, '#perr2')).includes('stimmt nicht'));
  await p.screenshot({ path: `${OUT}/3-profil.png`, fullPage: true });

  // --- Einladung ---
  await p.click('[data-a=invite]');
  await p.waitForSelector('#invurl');
  const link = await p.inputValue('#invurl');
  ok('Einladungslink erzeugt', /\?invite=[\w-]{20,}$/.test(link), link);
  await p.screenshot({ path: `${OUT}/4-einladung.png` });
  await p.click('.sheet [data-a=close]');
  ok('Superadmin sieht „Neue Familie einladen“', await p.locator('[data-a=inviteFamily]').count() === 1);

  const p2 = await mk();
  await p2.goto(link);
  await p2.waitForSelector('#authform');
  ok('Einladungsseite nennt die Familie', (await text(p2, '#auth')).includes('Die Testers'));
  await p2.screenshot({ path: `${OUT}/5-register.png` });
  await p2.fill('#name', 'Paula'); await p2.fill('#email', 'paula@example.de');
  await p2.fill('#password', 'paulas-passwort-1'); await p2.fill('#password2', 'paulas-passwort-1');
  await p2.click('#authform button[type=submit]');
  await p2.waitForSelector('#auth', { state: 'hidden' });
  await p2.waitForSelector('nav button');
  ok('Paula ist in der Familie angemeldet', (await text(p2, '#fam')) === 'Die Testers');
  await p2.click('#me'); await p2.waitForSelector('[data-form=profile]');
  const v2 = await text(p2, '#view');
  ok('Paula sieht beide Mitglieder', v2.includes('Anna Beispiel') && v2.includes('Paula'));
  ok('Paula (Mitglied) sieht keine Einlade-Knöpfe', await p2.locator('[data-a=invite]').count() === 0);
  ok('Paula kann Standort nicht ändern', await p2.locator('#lo').isDisabled());
  ok('URL ohne ?invite nach Registrierung', !p2.url().includes('invite='));
  await p2.screenshot({ path: `${OUT}/6-mitglied.png`, fullPage: true });

  // --- Neue Familie: getrennte Daten ---
  await p.click('[data-a=inviteFamily]'); await p.waitForSelector('#invurl');
  const link2 = await p.inputValue('#invurl'); await p.click('.sheet [data-a=close]');
  const p3 = await mk(); await p3.goto(link2); await p3.waitForSelector('#family_name');
  await p3.fill('#family_name', 'Familie Zwei'); await p3.fill('#name', 'Ben'); await p3.fill('#email', 'ben@example.de');
  await p3.fill('#password', 'bens-passwort-123'); await p3.fill('#password2', 'bens-passwort-123');
  await p3.click('#authform button[type=submit]'); await p3.waitForSelector('#auth', { state: 'hidden' });
  await p3.waitForSelector('nav button');
  ok('Ben hat eigene Familie', (await text(p3, '#fam')) === 'Familie Zwei');
  await p3.click('#me'); await p3.waitForSelector('[data-form=profile]');
  const v3 = await text(p3, '#view');
  ok('Ben sieht Anna/Paula nicht', !v3.includes('Anna') && !v3.includes('Paula'));
  await p3.screenshot({ path: `${OUT}/7-neue-familie.png` });

  // --- Mitglied entfernen + Sitzung abgelaufen ---
  p.on('dialog', d => d.accept());
  await p.reload(); await p.waitForSelector('nav button'); await p.click('#me'); await p.waitForSelector('[data-a=rmMember]');
  await p.click('[data-a=rmMember]');
  await p.waitForFunction(() => !document.querySelector('[data-a=rmMember]'));
  ok('Paula wurde entfernt', !(await text(p, '#view')).includes('Paula'));
  await p2.reload();
  await p2.waitForSelector('#authform');
  ok('Paula ist abgemeldet und sieht die Anmeldung', (await text(p2, '#auth h1')).includes('Anmelden'));
  await p2.fill('#email', 'paula@example.de'); await p2.fill('#password', 'paulas-passwort-1');
  await p2.click('#authform button[type=submit]');
  await p2.waitForFunction(() => document.querySelector('#aerr').textContent.length > 0);
  ok('entferntes Konto kann sich nicht anmelden', (await text(p2, '#aerr')).includes('stimmt nicht'));
  await p2.screenshot({ path: `${OUT}/8-login-fehler.png` });

  // --- Abmelden/Anmelden ---
  await p.click('[data-a=logout]'); await p.waitForSelector('#authform');
  ok('Abmelden führt zur Anmeldung', (await text(p, '#auth h1')).includes('Anmelden'));
  await p.fill('#email', 'anna@example.de'); await p.fill('#password', 'passwort-12345');
  await p.click('#authform button[type=submit]'); await p.waitForSelector('nav button');
  ok('erneut anmelden klappt', (await text(p, '#fam')) === 'Die Testers');

  for (const [i, pg] of [p, p2, p3].entries()) ok(`keine JS-Fehler in Seite ${i + 1}`, pg.errors.length === 0, pg.errors.join(' | '));
  await browser.close();
  console.log(fails ? `\n${fails} FEHLER` : '\nAlle UI-Prüfungen bestanden.');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
