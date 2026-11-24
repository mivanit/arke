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
    console.log('ok: reusable chat, shared queue controls, owner approvals, inert content, and uncertain-send retry');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
