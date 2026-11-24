/* Browser tests for the reusable component; fake native backend, no model calls. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.ARKE_PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.ARKE_CHROME || chromium.executablePath(), headless: true });
  try {
    const page = await browser.newPage();
    await page.route('http://arke.test/**', route => {
      const source = route.request().url().endsWith('chat.js')
        ? fs.readFileSync(path.join(__dirname, '../web/chat.js'), 'utf8')
        : '<!doctype html><arke-chat></arke-chat><script type="module" src="/chat.js"></script>';
      route.fulfill({ contentType: route.request().url().endsWith('chat.js') ? 'text/javascript' : 'text/html', body: source });
    });
    await page.goto('http://arke.test/');
    await page.evaluate(() => customElements.whenDefined('arke-chat'));
    const result = await page.evaluate(async () => {
      const component = document.querySelector('arke-chat'), calls = [];
      component.follow = () => {}; // Explicit snapshots below; stream transport is tested on the server.
      const request = async (path, options) => { calls.push([path, JSON.parse(options.body)]); return {}; };
      await component.open({ id: 'one', request, owner: true });
      const snapshot = { id: 'one', name: '<script>bad()</script>', harness: 'codex', cwd: '/shared', state: 'running', active: 'turn-1', paused: false, error: '', preview: '<img src=x onerror=bad()>', history: [{ seq: 1, kind: 'message', data: { text: '<script>bad()</script>' } }], queue: [], approvals: [] };
      component.render(snapshot);
      const inert = !component.shadowRoot.querySelector('script,img') && component.$('history').textContent.includes('<script>');
      component.$('message').value = 'redirect'; component.$('interrupt').checked = true;
      await component.send();
      component.render({ ...snapshot, active: null, paused: true, state: 'paused', queue: [{ id: 'queued', text: 'old task', status: 'queued', origin: 'human' }] });
      const paused = !component.$('resume').hidden && component.$('queue').textContent.includes('Discard');
      component.owner = false;
      component.render({ ...snapshot, approvals: [{ id: 'approval', kind: 'permission', details: { command: 'danger' } }] });
      const guest = component.$('approvals').textContent.includes('Waiting for the owner') && !component.$('approvals').querySelector('button');
      return { calls, inert, paused, guest };
    });
    assert.ok(result.inert); assert.ok(result.paused); assert.ok(result.guest);
    assert.equal(result.calls.length, 1);
    assert.equal(result.calls[0][1].interrupt, true); assert.equal(result.calls[0][1].target, 'turn-1');
    assert.ok(result.calls[0][1].request_id);
    // A lost HTTP response must retain the same ID and message on retry.
    const retry = await page.evaluate(async () => {
      const component = document.querySelector('arke-chat'), sent = [];
      component.request = async (path, options) => { sent.push(JSON.parse(options.body)); if (sent.length === 1) throw new TypeError('network lost'); return {}; };
      component.$('message').value = 'original'; await component.send();
      component.$('message').value = 'edited'; await component.send();
      return sent;
    });
    assert.equal(retry[0].request_id, retry[1].request_id); assert.equal(retry[1].text, 'original');
    const races = await page.evaluate(async () => {
      const component = document.querySelector('arke-chat');
      const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
      const pending = deferred();
      await component.open({ id: 'race-a', request: () => pending.promise, owner: true });
      component.$('message').value = 'first'; const sending = component.send();
      await component.open({ id: 'race-b', request: async () => ({}), owner: true });
      const newViewEnabled = !component.$('send').disabled;
      pending.resolve({}); await sending;
      const completedOldViewEnabled = !component.$('send').disabled;

      const original = deferred(), retry = deferred();
      await component.open({ id: 'round-trip', request: () => original.promise });
      component.$('message').value = 'original'; const oldSend = component.send();
      await component.open({ id: 'another', request: async () => ({}) });
      await component.open({ id: 'round-trip', request: () => retry.promise });
      component.$('message').value = 'new draft'; const retrySend = component.send();
      const requestID = component.pending.request_id;
      original.resolve({}); await oldSend;
      const roundTripSafe = component.$('send').disabled && component.pending.request_id === requestID && component.$('message').value === 'new draft';
      retry.resolve({}); await retrySend;
      const draftPreserved = component.$('message').value === 'new draft' && !component.$('send').disabled;

      const stale = deferred();
      await component.open({ id: 'stored', request: () => stale.promise });
      component.$('message').value = 'old'; const staleSend = component.send();
      await component.open({ id: 'stored-other', request: async () => ({}) });
      sessionStorage.setItem('arke.pending.stored', JSON.stringify({ request_id: 'newer', text: 'new' }));
      stale.resolve({}); await staleSend;
      const newReceiptPreserved = JSON.parse(sessionStorage.getItem('arke.pending.stored')).request_id === 'newer';

      const action = deferred();
      await component.open({ id: 'action-old', request: () => action.promise });
      const acting = component.action('/resume', {});
      await component.open({ id: 'action-new', request: async () => ({}) });
      component.$('error').textContent = 'current error';
      action.reject(new Error('old error')); await acting;
      const staleActionIgnored = component.$('error').textContent === 'current error';
      return { newViewEnabled, completedOldViewEnabled, roundTripSafe, draftPreserved, newReceiptPreserved, staleActionIgnored };
    });
    for (const [name, passed] of Object.entries(races)) assert.ok(passed, name);

    const history = await page.evaluate(async () => {
      const component = document.querySelector('arke-chat');
      const record = i => ({ seq: i * 2, kind: 'message', data: { text: 'message ' + i } }); // Another conversation occupies the odd IDs.
      const records = (start, end) => Array.from({ length: end - start + 1 }, (_, i) => record(start + i));
      const snapshot = items => ({ id: 'history', name: 'History', harness: 'codex', cwd: '/workspace', state: 'idle', active: null, paused: false, error: '', preview: '', history: items, queue: [], approvals: [] });
      const calls = [];
      await component.open({ id: 'history', owner: true, request: async path => {
        calls.push(path); const before = Number(new URL(path, 'http://arke.test').searchParams.get('before'));
        return { history: records(1, 300).filter(item => item.seq < before).slice(-100) };
      } });
      component.render(snapshot(records(1, 100)));
      component.render(snapshot(records(201, 300)));
      const disjointReset = component.history.size === 100 && !component.history.has(2);
      await component.earlier(); await component.earlier(); await component.earlier();
      const allRecovered = component.history.size === 300 && records(1, 300).every(item => component.history.has(item.seq));
      component.render(snapshot(records(201, 300)));
      const exhaustionPersisted = component.$('earlier').hidden;

      let finish;
      await component.open({ id: 'history-old', request: () => new Promise(resolve => { finish = resolve; }) });
      component.render(snapshot(records(101, 200))); const loading = component.earlier();
      await component.open({ id: 'history-new', request: async () => ({ history: [] }) });
      component.render(snapshot([record(1000)]));
      finish({ history: records(1, 100) }); await loading;
      const staleHistoryIgnored = component.history.size === 1 && component.history.has(2000);

      let finishGap;
      await component.open({ id: 'history-gap', request: () => new Promise(resolve => { finishGap = resolve; }) });
      component.render(snapshot(records(101, 200))); const gapLoading = component.earlier();
      component.render(snapshot(records(401, 500)));
      finishGap({ history: records(1, 100) }); await gapLoading;
      const staleWindowIgnored = component.history.size === 100 && component.history.has(1000) && !component.history.has(2) && !component.$('earlier').disabled;

      component.owner = true;
      component.render({ ...snapshot(records(401, 500)), queue: [{ id: 'held', text: 'Earlier task', status: 'held', origin: 'human' }] });
      const heldControls = !component.$('resume').hidden && component.$('queue').textContent.includes('Discard');
      component.render({ ...snapshot(records(401, 500)), stopping: true, active: 'old-turn', paused: true });
      const stoppingControls = component.$('resume').hidden && component.$('interrupt').disabled && component.$('status').textContent.includes('stopping');
      return { disjointReset, allRecovered, exhaustionPersisted, staleHistoryIgnored, staleWindowIgnored, heldControls, stoppingControls, calls };
    });
    for (const [name, passed] of Object.entries(history)) if (name !== 'calls') assert.ok(passed, name);
    assert.ok(history.calls[0].endsWith('before=402'));
    const streams = await page.evaluate(async () => {
      const component = document.querySelector('arke-chat');
      let complete, cancelled = false;
      await component.open({ id: 'stream-old', request: () => new Promise(resolve => { complete = resolve; }) });
      const following = Object.getPrototypeOf(component).follow.call(component, component.controller);
      await component.open({ id: 'stream-new', request: async () => ({}) });
      component.$('status').textContent = 'new view';
      complete(new Response(new ReadableStream({ cancel() { cancelled = true; } })));
      await following;
      const lateStreamIgnored = cancelled && component.$('status').textContent === 'new view';

      let activeCancelled = false;
      await component.open({ id: 'stream-active', request: async () => new Response(new ReadableStream({ cancel() { activeCancelled = true; } })) });
      const active = Object.getPrototypeOf(component).follow.call(component, component.controller);
      await Promise.resolve(); await Promise.resolve();
      component.disconnect(); await active;
      return { lateStreamIgnored, activeCancelled };
    });
    for (const [name, passed] of Object.entries(streams)) assert.ok(passed, name);
    console.log('ok: reusable chat, held queues, owner approvals, inert content, retry, navigation races, draft preservation, history recovery, and stream cleanup');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
