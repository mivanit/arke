// Reusable conversation component. No bus, dashboard, or framework dependency.
// Host supplies request(path, options), conversation ID, and whether controls are owner-only.
const uuid = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), x => x.toString(16).padStart(2, '0')).join('');
const element = (tag, text, parent) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (parent) parent.append(el); return el; };

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
    </style>
    <h2 id="name"></h2><small id="cwd"></small><p id="status" aria-live="polite"></p>
    <p id="error" role="alert"></p><button id="earlier" hidden>Earlier messages</button>
    <div id="history" role="log" aria-label="Conversation"></div><div id="preview"></div>
    <div id="approvals"></div><div id="queue"></div>
    <form id="compose"><label>Message<textarea id="message" required maxlength="100000"></textarea></label>
      <div class="actions"><button id="send" type="submit">Send</button><label><input type="checkbox" id="interrupt"> Interrupt and redirect</label><button type="button" id="resume" hidden>Resume queue</button></div>
    </form>`;
    this.$ = id => this.shadowRoot.getElementById(id);
    this.$('compose').onsubmit = e => { e.preventDefault(); this.send(); };
    this.$('resume').onclick = () => this.action('/resume', {});
    this.$('earlier').onclick = () => this.earlier();
  }

  async open({ id, request, owner = false }) {
    this.disconnect();
    this.id = id; this.request = request; this.owner = owner; this.snapshot = null; this.history = new Map();
    this.approvalsSignature = ''; this.$('error').textContent = ''; this.$('message').value = '';
    this.$('interrupt').checked = false;
    try { this.pending = JSON.parse(sessionStorage.getItem('arke.pending.' + id) || 'null'); } catch { this.pending = null; }
    if (this.pending) { this.$('message').value = this.pending.text; this.$('send').textContent = 'Retry same message'; }
    else this.$('send').textContent = 'Send';
    this.controller = new AbortController();
    this.follow(this.controller);
  }

  disconnectedCallback() { this.disconnect(); }
  disconnect() { if (this.controller) this.controller.abort(); }
  path(suffix = '') { return '/api/conversations/' + encodeURIComponent(this.id) + suffix; }

  async action(suffix, body) {
    try {
      await this.request(this.path(suffix), { method: 'POST', body: JSON.stringify(body) });
      this.$('error').textContent = '';
    } catch (error) { this.$('error').textContent = error.message; }
  }

  async send() {
    if (!this.pending) {
      const text = this.$('message').value;
      if (!text.trim()) return;
      this.pending = { request_id: uuid(), text, interrupt: this.$('interrupt').checked, target: this.snapshot?.active || null };
      try { sessionStorage.setItem('arke.pending.' + this.id, JSON.stringify(this.pending)); } catch { /* memory-only */ }
    }
    this.$('send').disabled = true;
    const cid = this.id, pending = this.pending;
    try {
      await this.request(this.path('/messages'), { method: 'POST', body: JSON.stringify(pending) });
      try { sessionStorage.removeItem('arke.pending.' + cid); } catch { /* memory-only */ }
      if (cid !== this.id) return;
      this.pending = null; this.$('message').value = ''; this.$('interrupt').checked = false;
      this.$('send').textContent = 'Send'; this.$('error').textContent = '';
    } catch (error) {
      if (cid !== this.id) return;
      if (error.status && error.status < 500) {
        this.pending = null;
        try { sessionStorage.removeItem('arke.pending.' + cid); } catch { /* memory-only */ }
        this.$('send').textContent = 'Send';
      } else this.$('send').textContent = 'Retry same message';
      this.$('error').textContent = error.message + (this.pending ? '\nDelivery is uncertain. Retry sends the same message with the same ID.' : '');
    } finally { if (cid === this.id) this.$('send').disabled = false; }
  }

  async follow(controller) {
    while (!controller.signal.aborted) {
      try {
        const response = await this.request(this.path('/events'), { signal: controller.signal, stream: true });
        const reader = response.body.getReader(), decoder = new TextDecoder();
        let buffer = '';
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let end;
          while ((end = buffer.indexOf('\n\n')) >= 0) {
            const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2);
            const data = frame.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n');
            if (data && !controller.signal.aborted) this.render(JSON.parse(data));
          }
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        this.$('status').textContent = 'Disconnected. Reconnecting does not restart work.';
        if (error.status === 401 || error.status === 403) { this.$('error').textContent = error.message; return; }
      }
      if (!controller.signal.aborted) await new Promise(resolve => {
        const timer = setTimeout(resolve, 2000);
        controller.signal.addEventListener('abort', () => { clearTimeout(timer); resolve(); }, { once: true });
      });
    }
  }

  render(snapshot) {
    this.snapshot = snapshot;
    this.$('name').textContent = snapshot.name;
    this.$('cwd').textContent = `${snapshot.harness} · ${snapshot.cwd} · shared files`;
    this.$('status').textContent = snapshot.state + (snapshot.paused ? ' · queued messages are paused' : '');
    if (snapshot.error) this.$('error').textContent = snapshot.error;
    this.$('interrupt').disabled = !snapshot.active;
    this.$('resume').hidden = !this.owner || !snapshot.paused || !!snapshot.active;
    this.$('preview').textContent = snapshot.preview;
    for (const item of snapshot.history) this.history.set(item.seq, item);
    this.renderHistory();
    this.$('queue').replaceChildren();
    for (const item of snapshot.queue) {
      const box = element('div', undefined, this.$('queue')); box.className = 'pending';
      element('b', item.status + (item.origin === 'bus' ? ' · bus notification' : ''), box);
      element('p', item.text, box);
      if (this.owner && item.status === 'queued') {
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

  renderHistory() {
    const container = this.$('history'), bottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;
    container.replaceChildren();
    for (const item of [...this.history.values()].sort((a,b) => a.seq - b.seq)) {
      const row = element('article', undefined, container); row.dataset.kind = item.kind;
      element('small', item.kind === 'user' ? (item.data.origin === 'bus' ? 'Bus notification' : 'You') : item.kind === 'message' ? 'Agent' : item.kind, row);
      if (item.kind === 'tool') {
        const details = element('details', undefined, row); element('summary', 'Tool activity', details);
        element('pre', JSON.stringify(item.data.details, null, 2), details);
      } else element('p', item.data.text || item.data.error || item.data.status || '', row);
    }
    this.$('earlier').hidden = this.history.size < 100;
    if (bottom) container.scrollTop = container.scrollHeight;
  }

  async earlier() {
    try {
      const before = Math.min(...this.history.keys());
      const result = await this.request(this.path('/history?before=' + before));
      for (const item of result.history) this.history.set(item.seq, item);
      this.renderHistory(); if (!result.history.length) this.$('earlier').hidden = true;
    } catch (error) { this.$('error').textContent = error.message; }
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
