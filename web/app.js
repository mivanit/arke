import './chat.js';

const $ = id => document.getElementById(id);
let key = '', info, selected, loading = 0;
const chat = document.querySelector('arke-chat');
try { key = sessionStorage.getItem('arke.key') || ''; } catch { /* memory-only */ }
async function request(path, options = {}) {
  const { stream, ...init } = options;
  const response = await fetch(path, { ...init, headers: { Authorization: 'Bearer ' + key, ...(init.body ? { 'Content-Type': 'application/json' } : {}), ...init.headers } });
  if (!response.ok) {
    let data = {}; try { data = await response.json(); } catch { /* status is enough */ }
    const error = new Error(data.error || data.detail || `Request failed (${response.status})`); error.status = response.status; throw error;
  }
  return stream ? response : response.json();
}
function controls(snapshot = chat.snapshot) {
  const owner = info?.role === 'owner';
  $('lifecycle').hidden = !owner;
  for (const action of ['archive', 'restore']) {
    const control = snapshot?.controls?.[action];
    $(action).hidden = !owner || (action === 'archive' ? !!snapshot?.archived : !snapshot?.archived);
    $(action).disabled = !control?.allowed || chat.controlling;
    $(action).title = control?.reason || '';
  }
}
function select(id) {
  $('select').value = id || '';
  if (id === selected) { controls(); return; }
  selected = id;
  history.replaceState(null, '', location.pathname + (id ? '#' + encodeURIComponent(id) : ''));
  chat.hidden = !id;
  if (id) chat.open({ id, request, owner: info.role === 'owner' });
  else chat.disconnect();
  controls(id ? chat.snapshot : null);
}
async function load(preferred) {
  const generation = ++loading;
  const next = await request('/api/conversations' + ($('show-archived').checked ? '?include_archived=true' : ''));
  if (generation !== loading) return;
  const candidate = preferred || selected || decodeURIComponent(location.hash.slice(1));
  if (candidate && !next.conversations.some(c => c.id === candidate) && next.role === 'owner') {
    try {
      // An archived deep link or open conversation stays available even when
      // the normal list hides archives. Refreshing never reopens the component.
      const row = await request('/api/conversations/' + encodeURIComponent(candidate));
      if (generation !== loading) return;
      next.conversations.push(row);
    } catch (error) { if (error.status !== 404) throw error; }
  }
  if (generation !== loading) return;
  info = next;
  $('title').textContent = info.title;
  $('dashboard').hidden = !info.dashboard;
  $('create').hidden = info.role !== 'owner';
  $('agents').hidden = !!info.shared && !info.dashboard;
  $('workspace').style.gridTemplateColumns = $('agents').hidden ? '1fr' : '';
  $('select').replaceChildren(...info.conversations.map(c => new Option(c.name + (c.archived ? ' (archived)' : ''), c.id)));
  const previousProfile = $('profiles').value;
  $('profiles').replaceChildren(...info.profiles.map(p => new Option(`${p.harness} · ${p.cwd}`, p.id)));
  if (info.profiles.some(p => p.id === previousProfile)) $('profiles').value = previousProfile;
  $('login').hidden = true; $('workspace').hidden = false; $('lock').hidden = false; $('error').textContent = '';
  select(info.conversations.some(c => c.id === candidate) ? candidate : info.shared || info.conversations[0]?.id);
  if (!selected) chat.hidden = true;
}
chat.addEventListener('lifecycle-change', event => {
  if (event.detail.id !== selected) return;
  const snapshot = event.detail.snapshot;
  controls(snapshot);
  const old = info?.conversations.find(c => c.id === selected);
  if (old && old.archived !== snapshot.archived) {
    Object.assign(old, snapshot);
    load().catch(error => { $('error').textContent = error.message; });
  }
});
$('show-archived').onchange = () => load().catch(error => { $('error').textContent = error.message; });
for (const action of ['archive', 'restore']) $(action).onclick = async () => {
  $(action).disabled = true;
  await chat.lifecycle(action);
  controls();
};
$('login').onsubmit = async e => {
  e.preventDefault(); key = $('key').value;
  try { await load(); sessionStorage.setItem('arke.key', key); $('key').value = ''; }
  catch (error) { $('error').textContent = error.message; }
};
$('lock').onclick = () => {
  document.querySelector('arke-chat').disconnect(); key = '';
  try { sessionStorage.removeItem('arke.key'); } catch { /* memory-only */ }
  location.reload();
};
$('select').onchange = () => select($('select').value);
$('create').onsubmit = async e => {
  e.preventDefault(); const form = new FormData(e.target), button = e.target.querySelector('button'); button.disabled = true;
  try { const result = await request('/api/conversations', { method: 'POST', body: JSON.stringify(Object.fromEntries(form)) }); await load(result.id); }
  catch (error) { $('error').textContent = error.message; }
  finally { button.disabled = false; }
};
if (key) load().catch(error => { $('error').textContent = error.message; });
