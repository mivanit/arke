/* Real HTTP/SSE and production chat component; native replies come from container fixtures. */
const assert = require('node:assert/strict');
const { chromium } = require(process.env.ARKE_PLAYWRIGHT_MODULE || 'playwright');
const [url, mode, phase, savedID] = process.argv.slice(2);
const owner = 'a'.repeat(43), guest = 'b'.repeat(43);

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.ARKE_CHROME || chromium.executablePath(), headless: true });
  let page;
  try {
    const context = await browser.newContext({ serviceWorkers: 'block' });
    // The browser only needs our loopback service. Never visit external sites.
    await context.route('**/*', route => new URL(route.request().url()).origin === url ? route.continue() : route.abort());
    page = await context.newPage();
    page.setDefaultTimeout(30000);
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto(url + '/chat' + (savedID ? '#' + savedID : ''));
    await page.locator('#key').fill(owner);
    await page.locator('#login button').click();
    await page.locator('#workspace').waitFor({ state: 'visible' });
    if (mode === 'dashboard' && phase === 'initial') {
      await page.locator('#profiles').selectOption('private');
      await page.locator('#create input[name=name]').fill('Created in the browser');
      await page.locator('#create button').click();
      await page.waitForFunction(() => document.querySelector('arke-chat').snapshot?.name === 'Created in the browser');
    } else {
      await page.waitForFunction(() => !!document.querySelector('arke-chat').snapshot);
    }
    const chat = page.locator('arke-chat');
    const snapshot = () => page.evaluate(() => document.querySelector('arke-chat').snapshot);
    const cid = (await snapshot()).id;
    const wait = predicate => page.waitForFunction(predicate, null, { timeout: 30000 });
    const approval = () => wait(() => document.querySelector('arke-chat').snapshot?.approvals.length > 0);
    const idle = () => wait(() => {
      const s = document.querySelector('arke-chat').snapshot;
      return s && !s.active && !s.stopping && !s.approvals.length;
    });
    const send = async (text, interrupt = false) => {
      await chat.locator('#message').fill(text);
      if (interrupt) await chat.locator('#interrupt').check();
      await chat.locator('#send').click();
      await page.waitForFunction(() => !document.querySelector('arke-chat').pending);
    };
    const allow = async () => {
      await approval();
      await chat.getByRole('button', { name: 'Allow once', exact: true }).click();
      await idle();
    };

    if (phase === 'initial') {
      // A second viewer shares the backend but has no approval or creation rights.
      const viewer = await context.newPage();
      await viewer.goto(url + '/chat');
      await viewer.locator('#key').fill(guest);
      await viewer.locator('#login button').click();
      await viewer.locator('#workspace').waitFor({ state: 'visible' });
      assert.equal(await viewer.locator('#create').isVisible(), false);
      const denied = await viewer.evaluate(async cid => {
        const response = await fetch(`/api/conversations/${cid}/approvals/fake`, {
          method: 'POST', headers: { Authorization: 'Bearer ' + 'b'.repeat(43), 'Content-Type': 'application/json' }, body: JSON.stringify({ allow: true }),
        });
        return response.status;
      }, cid);
      assert.equal(denied, 401);
      await send('first turn');
      await approval();
      if (mode === 'standalone') {
        await viewer.getByText('Waiting for the owner').waitFor();
        assert.equal(await viewer.getByRole('button', { name: 'Allow once' }).count(), 0);
      }
      await allow();
      assert.ok((await snapshot()).history.some(item => item.kind === 'message'));
      await send('interrupt this turn');
      await approval();
      const old = (await snapshot()).active;
      await send('held until explicitly resumed');
      await wait(() => document.querySelector('arke-chat').snapshot.queue.some(q => q.status === 'queued'));
      await send('redirected turn', true);
      await page.waitForFunction(old => {
        const s = document.querySelector('arke-chat').snapshot;
        return s.active && s.active !== old && s.approvals.length && s.queue.some(q => q.status === 'held');
      }, old);
      await allow();
      assert.ok((await snapshot()).queue.some(q => q.status === 'held'));
      await chat.getByRole('button', { name: 'Resume queue', exact: true }).click();
      await allow();
      await wait(() => document.querySelector('arke-chat').snapshot.queue.length === 0);
      // Stop the supervisor with an active request and a later queued request.
      await send('stopped during restart');
      await approval();
      await send('queued across restart');
      await wait(() => document.querySelector('arke-chat').snapshot.queue.some(q => q.text === 'queued across restart'));
      await viewer.close();
    } else {
      assert.equal(cid, savedID);
      const recovered = await snapshot();
      assert.equal(recovered.paused, true);
      assert.equal(recovered.active, null);
      assert.equal(recovered.approvals.length, 0);
      assert.ok(recovered.queue.some(q => q.text === 'queued across restart' && q.status === 'queued'));
      // A graceful shutdown can confirm the interrupted turn's outcome. If it
      // could not, the owner must resolve uncertainty before releasing the queue.
      if (recovered.queue.some(q => q.status === 'uncertain')) {
        await chat.getByRole('button', { name: 'I checked its outcome', exact: true }).click();
        await wait(() => !document.querySelector('arke-chat').snapshot.queue.some(q => q.status === 'uncertain'));
      }
      await chat.getByRole('button', { name: 'Resume queue', exact: true }).click();
      await allow();
      await wait(() => document.querySelector('arke-chat').snapshot.queue.length === 0);
    }
    assert.deepEqual(errors, []);
    assert.equal(await chat.locator('#error').textContent(), '');
    console.log(JSON.stringify({ cid }));
  } catch (error) {
    if (page) console.error('Browser state:', JSON.stringify(await page.evaluate(() => {
      const chat = document.querySelector('arke-chat');
      return { snapshot: chat?.snapshot, error: chat?.$('error').textContent };
    }).catch(() => null)));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
