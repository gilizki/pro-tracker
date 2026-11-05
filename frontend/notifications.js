// Push notifications UI. Self-contained like friends.js: it does not touch the main
// script's state or functions. It adds:
//   * a small "הפעל התראות" control (only while this device is not subscribed yet),
//   * on iPhone outside the Home Screen app, a short hint instead of the button,
//   * opening the right place when a notification is tapped (?open=friends or the main screen),
//   * unsubscribing this device on logout.
(() => {
  const TOKEN_KEY = 'proTrackerToken'; // Same key the main script in index.html uses.
  const trackerView = document.getElementById('tracker-view');
  if (!trackerView) return;

  const TEXT = {
    enable: '🔔 הפעל התראות',
    iosHint: '📲 כדי לקבל התראות באייפון: לחצו על "שיתוף" ← "הוספה למסך הבית", ופתחו את האפליקציה משם.',
    blocked: '🔕 ההתראות חסומות. אפשר להפעיל אותן בהגדרות הדפדפן.',
    enabled: '✅ ההתראות הופעלו!',
    failed: 'לא הצלחנו להפעיל התראות. נסו שוב.',
  };

  const hasServiceWorker = 'serviceWorker' in navigator;
  const canPush = hasServiceWorker && 'PushManager' in window && 'Notification' in window;
  const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const isStandalone = window.navigator.standalone === true || window.matchMedia('(display-mode: standalone)').matches;

  const style = document.createElement('style');
  style.textContent = `
    .notif-box { text-align: center; margin: 0 0 12px; }
    .notif-box button { width: auto; margin: 0; padding: 7px 14px; }
    .notif-box .notif-text { font-size: 13px; color: #666; margin: 6px 0 0; }
  `;
  document.head.append(style);

  const box = document.createElement('div');
  box.className = 'notif-box';
  box.hidden = true;
  box.innerHTML = `<button data-n="enable"></button><p class="notif-text" data-n="text"></p>`;
  const el = name => box.querySelector(`[data-n="${name}"]`);
  el('enable').textContent = TEXT.enable;
  const userBar = trackerView.querySelector('.user-bar');
  if (userBar) userBar.after(box); else trackerView.prepend(box);

  function show(withButton, text = '') {
    el('enable').hidden = !withButton;
    el('text').textContent = text;
    el('text').hidden = !text;
    box.hidden = !withButton && !text;
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) headers.Authorization = `Bearer ${token}`;
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  // ----- service worker & subscription -------------------------------------
  const registrationReady = hasServiceWorker
    ? navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => null)
    : Promise.resolve(null);
  let publicKey = null; // null = not loaded yet, '' = push is not configured on the server

  const toBase64Url = bytes => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  function keyBytes(base64Url) {
    const base64 = (base64Url + '='.repeat((4 - base64Url.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/');
    return Uint8Array.from(atob(base64), c => c.charCodeAt(0));
  }
  function usesCurrentKey(subscription) {
    const key = subscription.options && subscription.options.applicationServerKey;
    return !key || toBase64Url(new Uint8Array(key)) === publicKey.replace(/=+$/, '');
  }
  const saveSubscription = subscription =>
    api('/notifications/subscriptions', { method: 'POST', body: JSON.stringify(subscription.toJSON()) });

  async function currentSubscription() {
    const registration = await registrationReady;
    return registration && registration.pushManager ? registration.pushManager.getSubscription() : null;
  }

  const loggedIn = () => !trackerView.classList.contains('hidden') && Boolean(localStorage.getItem(TOKEN_KEY));

  // Decides what (if anything) to show, and re-links an existing subscription to the logged-in user.
  async function refresh() {
    show(false);
    if (!loggedIn()) return;
    try {
      if (publicKey === null) publicKey = (await api('/notifications/config')).public_key || '';
      if (!publicKey) return;
      if (isIOS && !isStandalone) return show(false, TEXT.iosHint);
      if (!canPush) return;
      if (Notification.permission === 'denied') return show(false, TEXT.blocked);
      let subscription = await currentSubscription();
      if (subscription && !usesCurrentKey(subscription)) { await subscription.unsubscribe(); subscription = null; }
      if (subscription && Notification.permission === 'granted') { await saveSubscription(subscription); return; }
      show(true);
    } catch (_) { show(false); }
  }

  el('enable').onclick = async () => {
    // Must be the first call in the tap handler: browsers (iPhone especially) require a user gesture.
    let permission;
    try { permission = await Notification.requestPermission(); } catch (_) { permission = Notification.permission; }
    if (permission !== 'granted') return refresh();
    try {
      const registration = await registrationReady;
      const subscription = (await registration.pushManager.getSubscription())
        || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) });
      await saveSubscription(subscription);
      show(false, TEXT.enabled);
      setTimeout(() => { if (el('text').textContent === TEXT.enabled) show(false); }, 4000);
    } catch (_) { show(true, TEXT.failed); }
  };

  // Logout: stop this device from receiving the previous user's notifications.
  // Capture phase, so this starts before the main script's logout handler.
  document.addEventListener('click', event => {
    if (!event.target.closest || !event.target.closest('#logout')) return;
    (async () => {
      try {
        const subscription = await currentSubscription();
        if (!subscription) return;
        fetch('/notifications/unsubscribe', {
          method: 'POST', keepalive: true, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: subscription.endpoint }),
        }).catch(() => {});
        await subscription.unsubscribe();
      } catch (_) { /* logout continues regardless */ }
    })();
  }, true);

  // ----- tapping a notification -----------------------------------------------
  let pendingTarget = null;
  const targetOf = url => (new URL(url, location.origin).searchParams.get('open') === 'friends' ? 'friends' : 'main');

  function goTo(target) {
    if (trackerView.classList.contains('hidden')) { pendingTarget = target; return; } // after login
    pendingTarget = null;
    if (target !== 'friends') { window.scrollTo({ top: 0, behavior: 'smooth' }); return; }
    const friends = trackerView.querySelector('details.friends');
    if (!friends) return;
    if (friends.open) friends.dispatchEvent(new Event('toggle')); // friends.js reloads on "toggle" while open
    else friends.open = true;                                       // opening fires "toggle" itself
    friends.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  const params = new URLSearchParams(location.search);
  if (params.has('open')) {
    pendingTarget = params.get('open') === 'friends' ? 'friends' : 'main';
    params.delete('open');
    history.replaceState(null, '', location.pathname + (params.toString() ? `?${params}` : '') + location.hash);
  }

  // The app was already open: the service worker focused it and tells us where to go.
  if (hasServiceWorker) {
    navigator.serviceWorker.addEventListener('message', event => {
      if (event.data && event.data.type === 'pro-tracker-open') goTo(targetOf(event.data.url));
    });
  }

  function onTrackerVisibility() {
    if (trackerView.classList.contains('hidden')) { show(false); return; }
    refresh();
    if (pendingTarget) goTo(pendingTarget);
  }
  new MutationObserver(onTrackerVisibility).observe(trackerView, { attributes: true, attributeFilter: ['class'] });
  onTrackerVisibility();
})();
