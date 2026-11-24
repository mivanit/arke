import './chat.js';

const $ = id => document.getElementById(id);
let key = '', info;
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
function select(id) {
  if (!id) return;
  $('select').value = id;
  history.replaceState(null, '', location.pathname + '#' + encodeURIComponent(id));
  document.querySelector('arke-chat').open({ id, request, owner: info.role === 'owner' });
}
async function load(preferred) {
  info = await request('/api/conversations');
  $('title').textContent = info.title;
  $('dashboard').hidden = !info.dashboard;
  $('create').hidden = info.role !== 'owner';
  $('agents').hidden = !!info.shared && !info.dashboard;
  $('workspace').style.gridTemplateColumns = $('agents').hidden ? '1fr' : '';
  $('select').replaceChildren(...info.conversations.map(c => new Option(c.name, c.id)));
  $('profiles').replaceChildren(...info.profiles.map(p => new Option(`${p.harness} · ${p.cwd}`, p.id)));
  $('login').hidden = true; $('workspace').hidden = false; $('lock').hidden = false; $('error').textContent = '';
  const candidate = preferred || decodeURIComponent(location.hash.slice(1));
  select(info.conversations.some(c => c.id === candidate) ? candidate : info.shared || info.conversations[0]?.id);
}
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
