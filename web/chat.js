// Reusable conversation component. No bus, dashboard, or framework dependency.
// Host supplies request(path, options), conversation ID, and whether controls are owner-only.
const uuid = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');
const element = (tag, text, parent) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (parent) parent.append(el); return el; };
const HISTORY_ROWS = 300;
const HISTORY_BYTES = 1024 * 1024;
const errorText = error => typeof error === 'string' ? error : error?.message || (error ? JSON.stringify(error) : '');

export class ArkeChat extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<style>
      :host { display:block; min-width:0; font:15px/1.5 system-ui,sans-serif; }
      * { box-sizing:border-box; } [hidden] { display:none!important; }
      h2 { margin:0; font-size:20px; } small { opacity:.7; overflow-wrap:anywhere; }
      button,textarea,input { font:inherit; color:inherit; border:1px solid #80808055; border-radius:6px; padding:8px; background:Canvas; }
      button { cursor:pointer; } button:disabled { opacity:.5; cursor:default; }
      textarea { display:block; width:100%; resize:vertical; min-height:85px; }
      #history { max-height:58vh; min-height:160px; overflow:auto; padding:12px 0; }
      article { padding:10px 14px; margin:10px 0; border-left:3px solid #80808055; }
      article[data-kind=user] { border-color:#426cdb; } article p, pre { white-space:pre-wrap; overflow-wrap:anywhere; margin:4px 0; }
      pre { font-size:12px; max-height:250px; overflow:auto; }
      .actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:10px 0; }
      #error { color:light-dark(#aa2419,#ff9c93); white-space:pre-wrap; }
      #queue, #approvals { margin:12px 0; } .pending { padding:12px; border:1px solid #80808055; border-radius:6px; margin:8px 0; }
      label { display:block; margin:8px 0; } label input { display:block; width:100%; }
      #preview { white-space:pre-wrap; opacity:.8; } #status { font-weight:600; }
      dialog { max-width:min(900px,90vw); max-height:85vh; color:inherit; background:Canvas; border:1px solid #80808055; border-radius:6px; }
      dialog pre { max-height:65vh; }
    </style>
    <h2 id="name"></h2><small id="cwd"></small><p id="status" aria-live="polite"></p>
    <p id="error" role="alert"></p><button id="earlier" hidden>Earlier messages</button> <button id="latest" hidden>Latest messages</button>
    <div id="history" role="log" aria-label="Conversation"></div><div id="preview"></div>
    <div id="approvals"></div><div id="queue"></div>
    <form id="compose"><label>Message<textarea id="message" required maxlength="100000"></textarea></label>
      <div class="actions"><button id="send" type="submit">Send</button><label><input type="checkbox" id="interrupt"> Interrupt and redirect</label><button type="button" id="resume" hidden>Resume queue</button></div>
    </form><dialog id="detail"><button id="close-detail">Close</button><pre id="detail-text"></pre></dialog>`;
    this.generation = 0; this.historyGeneration = 0;
    this.$ = id => this.shadowRoot.getElementById(id);
    this.$('compose').onsubmit = e => { e.preventDefault(); this.send(); };
    this.$('resume').onclick = () => this.action('/resume', {});
    this.$('earlier').onclick = () => this.earlier();
    this.$('latest').onclick = () => this.latest();
    this.$('close-detail').onclick = () => this.$('detail').close();
    this.$('detail').onclose = () => { if (!this.$('detail').open) { this.detailGeneration++; this.$('detail-text').textContent = ''; } };
  }

  async open({ id, request, owner = false }) {
    this.disconnect();
    this.id = id; this.request = request; this.owner = owner; this.snapshot = null; this.snapshotHistorySeen = false; this.history = new Map();
    this.tail = new Map(); this.rows = new Map(); this.browsing = false; this.tailHasEarlier = false;
    this.detailGeneration = (this.detailGeneration || 0) + 1; this.$('detail').close(); this.$('detail-text').textContent = '';
    this.historyGeneration++; this.hasEarlier = false; this.loadingEarlier = false;
    for (const name of ['history', 'queue', 'approvals']) this.$(name).replaceChildren();
    for (const name of ['name', 'cwd', 'status', 'preview']) this.$(name).textContent = '';
    this.$('send').disabled = false; this.$('earlier').hidden = true; this.$('earlier').disabled = false; this.$('latest').hidden = true; this.$('resume').hidden = true;
    this.approvalsSignature = ''; this.$('error').textContent = ''; this.$('message').value = '';
    this.$('interrupt').checked = false; this.$('interrupt').disabled = true;
    try { this.pending = JSON.parse(sessionStorage.getItem('arke.pending.' + id) || 'null'); } catch { this.pending = null; }
    if (this.pending) { this.$('message').value = this.pending.text; this.$('send').textContent = 'Retry same message'; }
    else this.$('send').textContent = 'Send';
    this.controller = new AbortController();
    this.follow(this.controller);
  }

  disconnectedCallback() { this.disconnect(); }
  disconnect() { this.generation++; if (this.controller) this.controller.abort(); }
  path(suffix = '') { return '/api/conversations/' + encodeURIComponent(this.id) + suffix; }

  async action(suffix, body) {
    const generation = this.generation, request = this.request, path = this.path(suffix);
    try {
      await request(path, { method: 'POST', body: JSON.stringify(body) });
      if (generation === this.generation) this.$('error').textContent = '';
    } catch (error) { if (generation === this.generation) this.$('error').textContent = error.message; }
  }

  clearStoredPending(cid, pending) {
    try {
      const key = 'arke.pending.' + cid, stored = JSON.parse(sessionStorage.getItem(key) || 'null');
      if (stored?.request_id === pending.request_id) sessionStorage.removeItem(key);
    } catch { /* memory-only */ }
  }

  async send() {
    if (this.$('send').disabled) return;
    if (!this.pending) {
      const text = this.$('message').value;
      if (!text.trim()) return;
      this.pending = { request_id: uuid(), text, interrupt: this.$('interrupt').checked, target: this.snapshot?.active || null };
      try { sessionStorage.setItem('arke.pending.' + this.id, JSON.stringify(this.pending)); } catch { /* memory-only */ }
    }
    this.$('send').disabled = true;
    const cid = this.id, pending = this.pending, generation = this.generation, request = this.request, path = this.path('/messages');
    try {
      await request(path, { method: 'POST', body: JSON.stringify(pending) });
      this.clearStoredPending(cid, pending);
      if (generation !== this.generation) return;
      this.pending = null;
      if (this.$('message').value === pending.text) { this.$('message').value = ''; this.$('interrupt').checked = false; }
      this.$('send').textContent = 'Send'; this.$('error').textContent = '';
    } catch (error) {
      if (generation !== this.generation) return;
      if (error.status && error.status < 500) {
        this.pending = null;
        this.clearStoredPending(cid, pending);
        this.$('send').textContent = 'Send';
      } else this.$('send').textContent = 'Retry same message';
      this.$('error').textContent = error.message + (this.pending ? '\nDelivery is uncertain. Retry sends the same message with the same ID.' : '');
    } finally { if (generation === this.generation) this.$('send').disabled = false; }
  }

  async follow(controller) {
    const generation = this.generation, request = this.request, path = this.path('/events');
    while (!controller.signal.aborted && generation === this.generation) {
      try {
        const response = await request(path, { signal: controller.signal, stream: true });
        if (controller.signal.aborted || generation !== this.generation) { await response.body.cancel(); return; }
        const reader = response.body.getReader(), decoder = new TextDecoder();
        const stop = () => { reader.cancel().catch(() => {}); };
        controller.signal.addEventListener('abort', stop, { once: true });
        try {
          let buffer = '';
          while (!controller.signal.aborted) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let end;
            while ((end = buffer.indexOf('\n\n')) >= 0) {
              const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
              const data = frame.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n');
              if (data && !controller.signal.aborted && generation === this.generation) this.render(JSON.parse(data));
            }
          }
        } finally { controller.signal.removeEventListener('abort', stop); await reader.cancel().catch(() => {}); reader.releaseLock(); }
      } catch (error) {
        if (controller.signal.aborted || generation !== this.generation) return;
        this.$('status').textContent = 'Disconnected. Reconnecting does not restart work.';
        if (error.status === 401 || error.status === 403) { this.$('error').textContent = error.message; return; }
      }
      if (!controller.signal.aborted) await new Promise(resolve => {
        const finish = () => { clearTimeout(timer); controller.signal.removeEventListener('abort', finish); resolve(); };
        const timer = setTimeout(finish, 2000);
        controller.signal.addEventListener('abort', finish, { once: true });
      });
    }
  }

  render(snapshot) {
    this.snapshot = { ...snapshot, history: this.snapshot?.history || [] };
    this.$('name').textContent = snapshot.name;
    this.$('cwd').textContent = `${snapshot.harness} · ${snapshot.cwd} · shared files`;
    this.$('status').textContent = snapshot.state + (snapshot.stopping ? ' · stopping' : '') + (snapshot.paused ? ' · queued messages are paused' : '');
    if (snapshot.error) this.$('error').textContent = snapshot.error;
    this.$('interrupt').disabled = !snapshot.active || !!snapshot.stopping;
    const held = snapshot.queue.some(item => item.status === 'held');
    this.$('resume').hidden = !this.owner || !(snapshot.paused || held) || !!snapshot.active || !!snapshot.stopping;
    this.$('preview').textContent = snapshot.preview;
    if (snapshot.history) {
      this.updateHistory(snapshot);
      this.snapshot.history = [...this.tail.values()].sort((a,b) => a.seq - b.seq);
    }
    this.$('queue').replaceChildren();
    for (const item of snapshot.queue) {
      const box = element('div', undefined, this.$('queue')); box.className = 'pending';
      element('b', item.status + (item.origin === 'bus' ? ' · bus notification' : ''), box);
      element('p', item.text, box);
      if (this.owner && ['queued', 'held'].includes(item.status)) {
        element('button', 'Discard', box).onclick = () => this.action('/requests/' + encodeURIComponent(item.id) + '/discard', {});
      } else if (this.owner && item.status === 'uncertain') {
        element('p', 'Inspect the native conversation before resolving this request. It will not be replayed.', box);
        element('button', 'I checked its outcome', box).onclick = () => this.action('/requests/' + encodeURIComponent(item.id) + '/resolve', {});
      }
    }
    const signature = JSON.stringify(snapshot.approvals);
    if (signature !== this.approvalsSignature) {
      this.approvalsSignature = signature; this.$('approvals').replaceChildren();
      for (const approval of snapshot.approvals) this.renderApproval(approval);
    }
  }

  trimHistory(history, keepLatest = true) {
    const items = [...history.values()].sort((a,b) => keepLatest ? b.seq - a.seq : a.seq - b.seq);
    let bytes = 0, removed = false;
    for (const [index, item] of items.entries()) {
      bytes += new TextEncoder().encode(JSON.stringify(item)).length;
      if (index >= HISTORY_ROWS || bytes > HISTORY_BYTES) { history.delete(item.seq); removed = true; }
    }
    return removed;
  }

  updateHistory(snapshot) {
    // Missing history means a state/preview-only update. Append frames carry
    // new immutable records; reconnects and skipped windows replace the tail.
    const reset = !this.snapshotHistorySeen || snapshot.history_mode === 'replace' ||
      (!snapshot.history_mode && snapshot.history.length && !snapshot.history.some(item => this.tail.has(item.seq)));
    if (reset) { this.tail.clear(); this.tailHasEarlier = snapshot.has_earlier ?? snapshot.history.length >= 100; }
    for (const item of snapshot.history) this.tail.set(item.seq, item);
    if (this.trimHistory(this.tail)) this.tailHasEarlier = true;
    this.snapshotHistorySeen = true;
    if (this.browsing) return;
    if (reset) {
      this.history.clear(); this.historyGeneration++;
      this.hasEarlier = this.tailHasEarlier; this.loadingEarlier = false; this.$('earlier').disabled = false;
    }
    for (const item of snapshot.history) this.history.set(item.seq, item);
    if (this.trimHistory(this.history)) this.hasEarlier = true;
    this.renderHistory();
  }

  latest() {
    this.historyGeneration++; this.browsing = false; this.loadingEarlier = false;
    this.history = new Map(this.tail); this.hasEarlier = this.tailHasEarlier;
    this.$('earlier').disabled = false; this.$('latest').hidden = true;
    this.renderHistory(); this.$('history').scrollTop = this.$('history').scrollHeight;
  }

  async detail(item) {
    const generation = this.generation, detailGeneration = ++this.detailGeneration;
    this.$('detail-text').textContent = 'Loading…'; this.$('detail').showModal();
    try {
      const record = await this.request(this.path('/history/' + item.seq));
      if (generation !== this.generation || detailGeneration !== this.detailGeneration) return;
      this.$('detail-text').textContent = record.kind === 'tool' ? JSON.stringify(record.data.details, null, 2) :
        record.data.text || errorText(record.data.error) || record.data.status || JSON.stringify(record.data, null, 2);
    } catch (error) {
      if (generation === this.generation && detailGeneration === this.detailGeneration) this.$('detail-text').textContent = error.message;
    }
  }

  renderHistory() {
    const container = this.$('history'), bottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;
    for (const [seq, row] of this.rows) {
      if (!this.history.has(seq)) { row.remove(); this.rows.delete(seq); }
    }
    let previous = null;
    for (const item of [...this.history.values()].sort((a,b) => a.seq - b.seq)) {
      let row = this.rows.get(item.seq);
      if (!row) {
        row = element('article'); row.dataset.kind = item.kind; this.rows.set(item.seq, row);
        element('small', item.kind === 'user' ? (item.data.origin === 'bus' ? 'Bus notification' : 'You') : item.kind === 'message' ? 'Agent' : item.data.status || item.kind, row);
        if (item.deferred) {
          element('button', 'Load full output', row).onclick = () => this.detail(item);
        } else if (item.kind === 'tool') {
          const details = element('details', undefined, row); element('summary', 'Tool activity', details);
          element('pre', JSON.stringify(item.data.details, null, 2), details);
        } else element('p', item.data.text || errorText(item.data.error) || item.data.status || '', row);
      }
      const next = previous ? previous.nextSibling : container.firstChild;
      if (row !== next) container.insertBefore(row, next);
      previous = row;
    }
    this.$('earlier').hidden = !this.hasEarlier;
    if (bottom) container.scrollTop = container.scrollHeight;
  }

  async earlier() {
    if (this.loadingEarlier || !this.hasEarlier || !this.history.size) return;
    const generation = this.generation, historyGeneration = this.historyGeneration;
    const request = this.request, before = Math.min(...this.history.keys()), path = this.path('/history?before=' + before);
    const current = () => generation === this.generation && historyGeneration === this.historyGeneration;
    this.loadingEarlier = true; this.$('earlier').disabled = true;
    this.browsing = true; this.$('latest').hidden = false;
    try {
      const result = await request(path);
      if (!current()) return;
      for (const item of result.history) this.history.set(item.seq, item);
      this.trimHistory(this.history, false);
      this.hasEarlier = result.has_earlier ?? result.history.length >= 100;
      this.renderHistory();
    } catch (error) { if (current()) this.$('error').textContent = error.message; }
    finally { if (current()) { this.loadingEarlier = false; this.$('earlier').disabled = false; } }
  }

  renderApproval(approval) {
    const box = element('div', undefined, this.$('approvals')); box.className = 'pending';
    element('b', approval.kind === 'input' ? 'Agent has a question' : 'Permission requested', box);
    if (!this.owner) { element('p', 'Waiting for the owner.', box); return; }
    if (approval.kind === 'input') {
      const form = element('form', undefined, box), fields = [];
      for (const question of approval.details.questions || []) {
        const label = element('label', question.question || question.header, form);
        if (question.options?.length) element('small', question.options.map(o => o.label).join(' / '), label);
        const input = element('input', undefined, label); input.required = true;
        fields.push([question.id || question.question, input]);
      }
      element('button', 'Answer', form);
      form.onsubmit = e => { e.preventDefault(); this.action('/approvals/' + approval.id, { answers: Object.fromEntries(fields.map(([id,input]) => [id,input.value])) }); };
    } else {
      element('pre', JSON.stringify(approval.details, null, 2), box);
      const actions = element('div', undefined, box); actions.className = 'actions';
      for (const [label, allow] of [['Allow once', true], ['Deny', false]]) {
        element('button', label, actions).onclick = () => this.action('/approvals/' + approval.id, { allow });
      }
    }
  }
}
customElements.define('arke-chat', ArkeChat);
