/* Service Worker nur für Benachrichtigungen. Kein fetch-Handler: nichts wird zwischengespeichert,
   neue Versionen kommen wie bisher sofort beim Neuladen an. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (_) { d = {body: e.data ? e.data.text() : ''}; }
  e.waitUntil(self.registration.showNotification(d.title || 'Familie', {
    body: d.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    data: {url: d.url || '/'}
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  e.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(list => {
    for (const c of list) {
      if (new URL(c.url).origin === self.location.origin && 'focus' in c) {
        c.postMessage({open: url});
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
