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
      component.follow = () => { component.render({ id: component.id, name: 'Agent', harness: 'codex', cwd: '/shared', state: 'idle', active: null, paused: false, error: '', preview: '', queue: [], approvals: [], history: [] }); }; // Explicit snapshots; transport is tested on the server.
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
      const readingWindowPreserved = component.history.size === 200 && component.history.has(2) && !component.history.has(1000) && !component.$('earlier').disabled;
      component.latest();
      const newTailAvailable = component.history.size === 100 && component.history.has(1000) && !component.history.has(2);

      component.owner = true;
      component.render({ ...snapshot(records(401, 500)), held: true, queue: [{ id: 'held', text: 'Earlier task', status: 'held', origin: 'human' }] });
      const heldControls = !component.$('resume').hidden && component.$('queue').textContent.includes('Discard');
      component.render({ ...snapshot(records(401, 500)), stopping: true, active: 'old-turn', paused: true });
      const stoppingControls = component.$('resume').disabled && component.$('interrupt').disabled && component.$('status').textContent.includes('Cleaning up');
      return { disjointReset, allRecovered, exhaustionPersisted, staleHistoryIgnored, readingWindowPreserved, newTailAvailable, heldControls, stoppingControls, calls };
    });
    for (const [name, passed] of Object.entries(history)) if (name !== 'calls') assert.ok(passed, name);
    assert.ok(history.calls[0].endsWith('before=402'));
    const bounded = await page.evaluate(async () => {
      const component = document.querySelector('arke-chat');
      const record = seq => ({ seq, kind: 'tool', data: { details: { output: 'tool ' + seq } } });
      const state = history => ({ id: 'bounded', name: 'Agent', harness: 'codex', cwd: '/workspace', state: 'running', active: 'one', paused: false, preview: '', error: '', history, queue: [], approvals: [] });
      await component.open({ id: 'bounded', request: async () => ({ history: [record(1)], has_earlier: false }) });
      component.render({ ...state([record(1)]), history_mode: 'replace', has_earlier: false });
      const details = component.$('history').querySelector('details'); details.open = true;
      const preview = state([]); delete preview.history; preview.preview = 'another token';
      component.render(preview);
      const snapshotPreserved = component.snapshot.history[0].seq === 1;
      component.render({ ...state([record(2)]), history_mode: 'append' });
      const detailsPreserved = details.open && details === component.$('history').querySelector('details');
      for (let seq = 3; seq <= 700; seq++) component.render({ ...state([record(seq)]), history_mode: 'append' });
      const rowBound = component.history.size === 300 && component.rows.size === 300 && component.tail.size === 300 && component.hasEarlier;
      await component.earlier();
      const oldestReachable = component.history.has(1) && !component.hasEarlier && !component.$('latest').hidden;
      component.render({ ...state([record(701)]), history_mode: 'append' });
      const readingPreserved = component.history.has(1) && !component.history.has(701);
      component.latest();
      const latestReachable = component.history.has(701) && !component.history.has(1) && component.$('latest').hidden;

      const heavy = seq => ({ seq, kind: 'message', data: { text: '\u0000'.repeat(1200) } });
      component.render({ ...state([heavy(1000)]), history_mode: 'replace', has_earlier: true });
      for (let seq = 1001; seq <= 1200; seq++) component.render({ ...state([heavy(seq)]), history_mode: 'append' });
      const bytes = [...component.history.values()].reduce((n, r) => n + new TextEncoder().encode(JSON.stringify(r)).length, 0);
      const byteBound = bytes <= 1024 * 1024 && component.history.size < 200;
      component.render({ ...state([{ seq: 2000, kind: 'completed', data: { status: 'failed', error: { message: 'rate limit exceeded' } } }]), history_mode: 'replace' });
      const readableError = component.$('history').textContent.includes('rate limit exceeded') && !component.$('history').textContent.includes('[object Object]');

      const deferred = { seq: 2001, kind: 'tool', deferred: true, data: {} };
      component.render({ ...state([deferred]), history_mode: 'append' });
      let requested;
      component.request = async path => { requested = path; return { kind: 'tool', data: { details: '<script>full output</script>' } }; };
      await component.detail(deferred);
      const fullOutput = requested.endsWith('/history/2001') && component.$('detail').open && component.$('detail-text').textContent.includes('<script>full output</script>') && !component.$('detail').querySelector('script');
      component.$('detail').close();
      await new Promise(resolve => setTimeout(resolve, 30));
      const releasedOutput = component.$('detail-text').textContent === '';
      let finish;
      component.request = () => new Promise(resolve => { finish = resolve; });
      const loading = component.detail(deferred);
      await component.open({ id: 'different', request: async () => ({}) });
      finish({ kind: 'tool', data: { details: 'stale output' } }); await loading;
      const staleDetailIgnored = !component.$('detail').open && component.$('detail-text').textContent === '';
      return { snapshotPreserved, detailsPreserved, rowBound, oldestReachable, readingPreserved, latestReachable, byteBound, readableError, fullOutput, releasedOutput, staleDetailIgnored };
    });
    for (const [name, passed] of Object.entries(bounded)) assert.ok(passed, name);
    const lifecycle = await page.evaluate(async () => {
      const component = document.querySelector('arke-chat');
      await component.open({ id: 'lifecycle', request: async () => ({}), owner: true });
      const base = { ...component.snapshot, control_token: 'epoch:1', paused: true,
        controls: Object.fromEntries(['stop', 'stop_now', 'resume', 'archive', 'restore'].map(name => [name, { allowed: true, reason: '' }])) };
      component.render(base);
      const calls = [], controlling = [];
      const listener = () => controlling.push(component.controlling);
      component.addEventListener('lifecycle-change', listener);
      component.request = async (path, options) => {
        calls.push([path, options?.body && JSON.parse(options.body)]);
        if (options) { const error = new Error('controls changed'); error.status = 409; throw error; }
        return { ...base, control_token: 'epoch:2' };
      };
      await component.lifecycle('stop', 'now');
      component.removeEventListener('lifecycle-change', listener);
      const hostControlsReleased = controlling[0] === true && controlling.at(-1) === false;
      const staleControlRefreshed = calls.length === 2 && calls[0][1].expected_control === 'epoch:1' && calls[0][1].expected_active === null && component.snapshot.control_token === 'epoch:2';
      let finish;
      component.request = () => new Promise(resolve => { finish = resolve; });
      component.$('message').value = 'accepted before archive';
      const sending = component.send();
      component.render({ ...base, archived: true });
      finish({}); await sending;
      const archivedSendStaysDisabled = component.$('send').disabled && component.$('message').disabled;
      component.render(base);
      const sent = [];
      component.request = async (path, options) => { sent.push(JSON.parse(options.body)); if (sent.length === 1) throw new TypeError('response lost'); return {}; };
      component.$('message').value = 'check my receipt';
      await component.send();
      const original = JSON.stringify(component.pending);
      component.render({ ...base, archived: true });
      const deliveryAvailable = component.$('send').textContent === 'Check delivery' && !component.$('send').disabled;
      await component.send();
      const archivedReceiptRetried = JSON.stringify(sent[1]) === original && component.$('send').disabled;
      return { hostControlsReleased, staleControlRefreshed, archivedSendStaysDisabled, deliveryAvailable, archivedReceiptRetried };
    });
    for (const [name, passed] of Object.entries(lifecycle)) assert.ok(passed, name);
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
    // Exercise the shell with the real component, including archived deep links
    // and list refreshes that must preserve drafts, history, and selection.
    const shell = await browser.newPage();
    const state = (id, archived) => ({ id, name: id, harness: 'codex', cwd: '/shared', state: 'paused', active: null, paused: true, archived, error: '', preview: '', queue: [], approvals: [], history: [], control_token: id + ':1',
      controls: { archive: { allowed: !archived }, restore: { allowed: archived }, resume: { allowed: !archived }, stop: { allowed: !archived }, stop_now: { allowed: !archived } } });
    const conversations = { one: state('one', false), archived: state('archived', true) };
    await shell.route('http://shell.test/**', async route => {
      const url = new URL(route.request().url()), parts = url.pathname.split('/');
      let value;
      if (url.pathname === '/api/conversations') value = { conversations: Object.values(conversations).filter(c => !c.archived || url.searchParams.has('include_archived')), role: 'owner', shared: null, title: 'Agents', profiles: [], dashboard: true };
      else if (url.pathname.startsWith('/api/conversations/')) {
        const row = conversations[parts[3]];
        if (parts[4] === 'events') return route.fulfill({ contentType: 'text/event-stream', body: 'event: snapshot\ndata: ' + JSON.stringify(row) + '\n\n' });
        if (route.request().method() === 'POST') {
          conversations[row.id] = { ...state(row.id, parts[4] === 'archive'), control_token: row.id + ':2' };
          value = { conversation: conversations[row.id], synchronizing: false };
        } else value = row;
      } else {
        const file = url.pathname.endsWith('.js') ? path.basename(url.pathname) : url.pathname.endsWith('.css') ? 'chat.css' : 'chat.html';
        return route.fulfill({ contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(path.join(__dirname, '../web', file), 'utf8') });
      }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
    });
    await shell.goto('http://shell.test/chat#archived');
    await shell.locator('#key').fill('owner-key');
    await shell.locator('#login button').click();
    await shell.waitForFunction(() => document.querySelector('arke-chat').snapshot?.id === 'archived');
    assert.equal(await shell.locator('arke-chat #send').isDisabled(), true);
    await shell.locator('#select').selectOption('one');
    await shell.waitForFunction(() => document.querySelector('arke-chat').snapshot?.id === 'one');
    await shell.locator('arke-chat #message').fill('preserve this draft');
    const generation = await shell.evaluate(() => document.querySelector('arke-chat').generation);
    await shell.locator('#show-archived').check();
    await shell.waitForFunction(() => document.querySelector('#select').options.length === 2);
    assert.equal(await shell.locator('arke-chat #message').inputValue(), 'preserve this draft');
    assert.equal(await shell.evaluate(() => document.querySelector('arke-chat').generation), generation);
    await shell.locator('#archive').click();
    await shell.waitForFunction(() => document.querySelector('arke-chat').snapshot?.archived);
    assert.equal(await shell.locator('arke-chat #message').inputValue(), 'preserve this draft');
    await shell.locator('#show-archived').uncheck();
    await shell.waitForFunction(() => document.querySelector('#select').options.length === 1);
    assert.equal(await shell.locator('#select').inputValue(), 'one');
    assert.equal(await shell.locator('arke-chat').isVisible(), true);
    await shell.locator('#restore').click();
    await shell.waitForFunction(() => !document.querySelector('arke-chat').snapshot?.archived);
    assert.equal(await shell.evaluate(() => document.querySelector('arke-chat').snapshot.paused), true);
    assert.equal(await shell.locator('arke-chat #message').inputValue(), 'preserve this draft');
    await shell.close();
    console.log('ok: reusable chat, held queues, owner approvals, inert content, retry, navigation races, draft preservation, history recovery, stream cleanup, lifecycle controls, and archive navigation');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
