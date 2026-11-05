// Friends feature UI. Self-contained: it adds the "חברים" panel (#panel-friends) to the
// tracker screen, opened from the menu, and does not touch the main script's state or functions.
// The main script shows/hides the panel and fires "pro-tracker:panel" when one is opened.
// Friends' data comes only from /friends endpoints, which return daily totals only.
(() => {
  const TOKEN_KEY = 'proTrackerToken'; // Same key the main script in index.html uses.
  const trackerView = document.getElementById('tracker-view');
  if (!trackerView) return;

  const style = document.createElement('style');
  style.textContent = `
    .friends h3 { font-size: 15px; margin: 16px 0 6px; }
    .friends .friends-code { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .friends .friends-code b { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 18px; letter-spacing: 1px; direction: ltr; }
    .friends button.friends-small { width: auto; margin: 0; padding: 7px 10px; }
    .friends .friends-actions { display: flex; gap: 6px; }
    .friends .friends-empty { color: #666; font-size: 14px; margin: 4px 0; }
    .friends .friends-message { text-align: center; font-size: 14px; min-height: 20px; color: #2b8a3e; }
  `;
  document.head.append(style);

  const section = document.createElement('section');
  section.id = 'panel-friends';
  section.className = 'friends panel hidden';
  section.innerHTML = `
    <h2>חברים 👥</h2>
    <div data-f="friends-block" hidden>
      <h3>החברים שלי – היום</h3>
      <div data-f="friends"></div>
    </div>
    <h3>קוד ההזמנה שלי</h3>
    <div class="friends-code">
      <b data-f="code">…</b>
      <button class="friends-small secondary" data-f="copy">העתק</button>
      <button class="friends-small secondary" data-f="regenerate">קוד חדש</button>
    </div>
    <h3>הוספת חבר</h3>
    <div class="row"><input data-f="username" autocomplete="off" placeholder="שם משתמש"><button class="friends-small" data-f="send-username">שלח בקשה</button></div>
    <div class="row"><input data-f="invite-code" autocomplete="off" placeholder="קוד הזמנה" dir="ltr"><button class="friends-small" data-f="send-code">שלח בקשה</button></div>
    <p class="friends-message" data-f="message"></p>
    <p class="error" data-f="error"></p>
    <h3>בקשות שממתינות לאישורך</h3>
    <div data-f="incoming"></div>
    <h3>בקשות ששלחתי</h3>
    <div data-f="outgoing"></div>
  `;
  trackerView.append(section);
  const el = name => section.querySelector(`[data-f="${name}"]`);

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) headers.Authorization = `Bearer ${token}`;
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof body.detail === 'string' ? body.detail : '';
      throw new Error(detail || `שגיאת שרת (${response.status}). נסה שוב בעוד רגע.`);
    }
    return body;
  }

  function notify(message = '', error = '') { el('message').textContent = message; el('error').textContent = error; }
  const fail = error => notify('', error.message);

  function smallButton(label, className, onClick) {
    const button = document.createElement('button');
    button.className = `friends-small ${className}`;
    button.textContent = label;
    button.onclick = onClick;
    return button;
  }

  function row(mainText, subText, buttons) {
    const item = document.createElement('div');
    item.className = 'meal';
    const text = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = mainText;
    text.append(strong);
    if (subText) { const small = document.createElement('small'); small.textContent = subText; text.append(document.createElement('br'), small); }
    const actions = document.createElement('div');
    actions.className = 'friends-actions';
    actions.append(...buttons);
    item.append(text, actions);
    return item;
  }

  function fill(container, items, emptyText) {
    container.replaceChildren();
    if (!items.length) { const p = document.createElement('p'); p.className = 'friends-empty'; p.textContent = emptyText; container.append(p); return; }
    container.append(...items);
  }

  async function loadCode() { el('code').textContent = (await api('/friends/invite-code')).code; }

  async function loadRequests() {
    const data = await api('/friends/requests');
    fill(el('incoming'), data.incoming.map(r => row(r.username, '', [
      smallButton('אשר', '', () => act(`/friends/requests/${r.id}/accept`, 'POST', `את/ה ו-${r.username} חברים עכשיו.`)),
      smallButton('דחה', 'danger', () => act(`/friends/requests/${r.id}/decline`, 'POST', 'הבקשה נדחתה.')),
    ])), 'אין בקשות חדשות.');
    fill(el('outgoing'), data.outgoing.map(r => row(r.username, 'ממתין לאישור', [
      smallButton('בטל', 'secondary', () => act(`/friends/requests/${r.id}`, 'DELETE', 'הבקשה בוטלה.')),
    ])), 'אין בקשות פתוחות.');
  }

  async function loadFriends() {
    const friends = await api(`/friends?date=${trackerDay()}`); // shared 02:00 Israel-time rule (day.js)
    el('friends-block').hidden = !friends.length;
    fill(el('friends'), friends.map(f => row(f.username, `🔥 ${f.total_calories} קל׳ · 💪 ${f.total_protein}g חלבון`, [
      smallButton('הסר', 'danger', () => {
        if (confirm(`להסיר את ${f.username} מהחברים?`)) act(`/friends/${f.user_id}`, 'DELETE', 'החבר הוסר.');
      }),
    ])), 'עוד אין לך חברים.');
  }

  async function loadAll() {
    try { await Promise.all([loadCode(), loadRequests(), loadFriends()]); } catch (error) { fail(error); }
  }

  async function act(path, method, successMessage) {
    notify();
    try { await api(path, { method }); notify(successMessage); await Promise.all([loadRequests(), loadFriends()]); } catch (error) { fail(error); }
  }

  async function sendRequest(payload, input) {
    notify();
    try {
      const result = await api('/friends/requests', { method: 'POST', body: JSON.stringify(payload) });
      input.value = '';
      notify(result.status === 'accepted' ? `את/ה ו-${result.username} חברים עכשיו.` : `בקשת חברות נשלחה ל-${result.username}.`);
      await Promise.all([loadRequests(), loadFriends()]);
    } catch (error) { fail(error); }
  }

  el('send-username').onclick = () => {
    const username = el('username').value.trim();
    if (!username) return notify('', 'יש להזין שם משתמש.');
    sendRequest({ username }, el('username'));
  };
  el('send-code').onclick = () => {
    const invite_code = el('invite-code').value.trim();
    if (!invite_code) return notify('', 'יש להזין קוד הזמנה.');
    sendRequest({ invite_code }, el('invite-code'));
  };
  el('copy').onclick = async () => {
    const code = el('code').textContent;
    try { await navigator.clipboard.writeText(code); notify('הקוד הועתק.'); } catch (_) { window.prompt('העתק את הקוד:', code); }
  };
  el('regenerate').onclick = async () => {
    if (!confirm('הקוד הנוכחי יפסיק לעבוד. ליצור קוד חדש?')) return;
    notify();
    try { el('code').textContent = (await api('/friends/invite-code/regenerate', { method: 'POST' })).code; notify('נוצר קוד חדש.'); } catch (error) { fail(error); }
  };

  // Load (again) every time the panel is opened; clear whenever the tracker screen is hidden (logout).
  document.addEventListener('pro-tracker:panel', event => { if (event.detail === 'friends') { notify(); loadAll(); } });
  new MutationObserver(() => {
    if (trackerView.classList.contains('hidden')) {
      notify();
      el('code').textContent = '…';
      el('friends-block').hidden = true;
      ['incoming', 'outgoing', 'friends'].forEach(name => el(name).replaceChildren());
    }
  }).observe(trackerView, { attributes: true, attributeFilter: ['class'] });
})();
