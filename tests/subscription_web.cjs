/* Explicitly invoked by subscription_web.py; this uses real subscription capacity. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.ARKE_PLAYWRIGHT_MODULE || 'playwright');
const [url, phase, marker, savedID] = process.argv.slice(2);
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.ARKE_CHROME || chromium.executablePath(), headless: true });
  let page;
  try {
    const context = await browser.newContext({ serviceWorkers: 'block' });
    await context.route('**/*', r => new URL(r.request().url()).origin === url ? r.continue() : r.abort());
    page = await context.newPage(); page.setDefaultTimeout(180000);
    const errors = []; page.on('pageerror', error => errors.push(String(error)));
    await page.goto(url + '/chat' + (savedID ? '#' + savedID : ''));
    await page.locator('#key').fill(process.env.ARKE_TEST_OWNER_KEY);
    await page.locator('#login button').click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    if (phase === 'initial') {
      await page.locator('#profiles').selectOption('codex');
      await page.locator('#create input[name=name]').fill('Subscription lifecycle acceptance');
      await page.locator('#create button').click();
    }
    const chat = page.locator('arke-chat');
    const state = () => page.evaluate(() => document.querySelector('arke-chat').snapshot);
    const wait = (predicate, arg) => page.waitForFunction(({ source, arg }) => {
      const snapshot = document.querySelector('arke-chat').snapshot;
      if (snapshot?.error) throw new Error(snapshot.error);
      return (0, eval)('(' + source + ')')(arg);
    }, { source: predicate.toString(), arg }, { timeout: 180000 });
    await wait(() => !!document.querySelector('arke-chat').snapshot);
    const cid = (await state()).id;
    const recall = 'Reply with only the exact marker from my first message. Do not use tools.';
    const send = async text => {
      await chat.locator('#message').fill(text);
      await chat.locator('#send').click();
      await wait(() => !document.querySelector('arke-chat').pending);
    };
    const approval = async (value, turn = 1) => {
      await wait(turn => {
        const s = document.querySelector('arke-chat').snapshot;
        if (s.history.filter(h => h.kind === 'completed').length >= turn && s.approvals.length === 0)
          throw new Error('Native turn ended without requesting tool approval');
        return s.approvals.length > 0;
      }, turn);
      const approvals = (await state()).approvals;
      assert.equal(approvals.length, 1);
      assert.equal(approvals[0].details.serverName, 'acceptance');
      assert.equal(approvals[0].details._meta.tool_name, 'record_marker');
      assert.equal(approvals[0].details._meta.tool_params.value, value);
    };
    const stopped = () => wait(() => {
      const s = document.querySelector('arke-chat').snapshot;
      return s.paused && !s.active && !s.stopping && !s.retiring;
    });
    const messages = count => wait(({ marker, count }) => {
      const s = document.querySelector('arke-chat').snapshot;
      const completed = s.history.filter(h => h.kind === 'completed');
      const messages = s.history.filter(h => h.kind === 'message');
      return !s.active && completed.length === count && messages.at(-1)?.data.text.trim() === marker;
    }, { marker, count });
    if (phase === 'initial') {
      await send(`Remember this marker: ${marker}. Invoke acceptance record_marker now with value approved exactly once. The harness will request permission before executing it. Invoke the tool directly; do not ask for permission in a text reply. Use no other tools.`);
      await approval('approved');
      await send(recall);
      await wait(() => document.querySelector('arke-chat').snapshot.queue.some(q => q.status === 'queued'));
      await chat.getByRole('button', { name: 'Stop after this turn', exact: true }).click();
      await wait(() => document.querySelector('arke-chat').snapshot.retiring);
      assert.equal(fs.existsSync(process.env.ARKE_TEST_MARKER_FILE), false);
      await page.reload();
      await wait(() => !!document.querySelector('arke-chat').snapshot);
      await approval('approved');
      await chat.getByRole('button', { name: 'Allow once', exact: true }).click();
      await stopped();
      assert.equal((await state()).history.filter(h => h.kind === 'completed' && h.data.status === 'completed').length, 1);
      assert.equal((await state()).queue.filter(q => q.status === 'queued').length, 1);
      assert.equal(fs.readFileSync(process.env.ARKE_TEST_MARKER_FILE, 'utf8'), 'approved');
      console.log('Passed graceful stop, approval, browser reload, and paused queue.');
      await chat.getByRole('button', { name: 'Resume queue', exact: true }).click();
      await messages(2);
      await chat.getByRole('button', { name: 'Stop now', exact: true }).click();
      await stopped();
      await page.locator('#archive').click();
      await wait(() => document.querySelector('arke-chat').snapshot.archived);
      assert.equal(await chat.locator('#send').isDisabled(), true);
      await page.locator('#restore').click();
      await wait(() => !document.querySelector('arke-chat').snapshot.archived);
      assert.equal((await state()).paused, true);
      await send(recall);
      await wait(() => document.querySelector('arke-chat').snapshot.queue.some(q => q.status === 'queued'));
      assert.equal((await state()).active, null);
      console.log('Passed fresh-worker recall, archive/restore, and queuing without starting a worker.');
    } else {
      assert.equal(cid, savedID);
      assert.equal((await state()).paused, true);
      assert.equal((await state()).active, null);
      assert.equal((await state()).queue.length, 1);
      await chat.getByRole('button', { name: 'Resume queue', exact: true }).click();
      await messages(3);
      console.log('Passed service restart with paused work and native conversation recall.');
      await send('Invoke acceptance record_marker now with value must-not-run exactly once. The harness will request permission before executing it. Invoke the tool directly; do not ask for permission in a text reply. Use no other tools.');
      await approval('must-not-run', 4);
      await send(recall);
      await wait(() => document.querySelector('arke-chat').snapshot.queue.some(q => q.status === 'queued'));
      await chat.getByRole('button', { name: 'Stop now', exact: true }).click();
      await stopped();
      const pending = (await state()).queue;
      assert.equal(pending.length, 1);
      assert.equal(pending[0].status, 'queued');
      assert.equal(fs.readFileSync(process.env.ARKE_TEST_MARKER_FILE, 'utf8'), 'approved');
      await chat.getByRole('button', { name: 'Discard', exact: true }).click();
      await wait(() => document.querySelector('arke-chat').snapshot.queue.length === 0);
      console.log('Passed active Stop now: unapproved tool never ran and queued work stayed paused.');
    }
    assert.deepEqual(errors, []);
    assert.equal(await chat.locator('#error').textContent(), '');
    console.log(JSON.stringify({ cid, phase, passed: true }));
  } catch (error) {
    if (page) console.error('Browser state:', JSON.stringify(await page.evaluate(() => {
      const c = document.querySelector('arke-chat'); return { snapshot: c?.snapshot, error: c?.$('error').textContent };
    }).catch(() => null)));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
