// Pro Tracker service worker: push notifications ONLY.
// It deliberately has no "fetch" handler and caches nothing, so the app loads
// exactly as it does without it. Served from "/sw.js" so its scope is the whole site.

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {}; }
  event.waitUntil(self.registration.showNotification(data.title || 'Pro Tracker', {
    body: data.body || '',
    icon: '/frontend/icon-192.png',
    tag: data.tag,
    lang: 'he',
    dir: 'rtl',
    silent: Boolean(data.silent), // Midnight Munch: no sound/vibration where supported
    data: { url: data.url || '/' },
  }));
});

// Focus an open Pro Tracker window and tell it where to go; open one only if none exists.
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const existing = windows.find(client => new URL(client.url).origin === self.location.origin);
    if (existing) {
      await existing.focus();
      existing.postMessage({ type: 'pro-tracker-open', url });
      return;
    }
    await self.clients.openWindow(url);
  })());
});
