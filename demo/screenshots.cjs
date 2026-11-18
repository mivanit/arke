/* Retake the README's screenshots from the built demo: node demo/screenshots.cjs (after make demo-build).
 * Uses the test suite's Playwright (make setup-browser). Writes demo/screenshots/*.png. */
const fs = require('node:fs');
const path = require('node:path');
const root = path.dirname(__dirname), site = path.join(root, '_site'), out = path.join(__dirname, 'screenshots');
const { chromium } = require(process.env.ARKE_PLAYWRIGHT_MODULE || 'playwright');
const types = { html: 'text/html', js: 'text/javascript', css: 'text/css', md: 'text/markdown' };

(async () => {
  if (!fs.existsSync(path.join(site, 'index.html'))) throw new Error('no _site/: run make demo-build first');
  // served under the address a real bus would have, so the header reads like one
  const base = 'http://devbox:7777/';
  const serve = route => {
    const f = path.join(site, new URL(route.request().url()).pathname.replace(/^\/+/, '') || 'index.html');
    return fs.existsSync(f) ? route.fulfill({ contentType: types[f.split('.').pop()], body: fs.readFileSync(f) }) : route.fulfill({ status: 404, body: '' });
  };
  const browser = await chromium.launch({ executablePath: process.env.ARKE_CHROME || chromium.executablePath(), headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    // [file, page, when it has drawn, what to tidy before the picture]
    const shots = [
      ['console', '', () => document.querySelectorAll('#agents tr').length >= 6 && document.querySelectorAll('#feed .msg').length > 10, () => {}],
      ['analytics', 'analytics.html', () => /CPU avg/.test(document.body.textContent), () => {}],
      ['analytics3d', 'analytics3d.html', () => window.ab3d?.files().length > 20 && /events/.test(document.getElementById('status').textContent), () => document.getElementById('helpx').click()],
    ];
    // The demo follows the clock, and at some moments nobody is busy: step back in time until a few agents are.
    const open = async back => {
      const p = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
      await p.addInitScript(s => { const now = Date.now; Date.now = () => now() - s * 1000; }, back);
      await p.route('**/*', serve);
      return p;
    };
    let back = 0;
    for (; back < 7200; back += 300) {
      const p = await open(back);
      await p.goto(base);
      await p.waitForFunction(shots[0][2]);
      const busy = await p.evaluate(() => ArkeDemo.agents.filter(a => a.state === 'busy').length);
      await p.close();
      if (busy >= 3) break;
    }
    for (const [name, file, ready, tidy] of shots) {
      const p = await open(back);
      await p.goto(base + file);
      await p.waitForFunction(ready, null, { timeout: 30000 });
      await p.evaluate(tidy);
      await p.evaluate(() => { for (const el of document.querySelectorAll('body > div')) if (el.textContent.startsWith('Demo ·')) el.remove(); });
      await p.waitForTimeout(name === 'analytics3d' ? 6000 : 1500);  // the 3D scene settles over a few seconds
      await p.screenshot({ path: path.join(out, name + '.png') });
      console.log('wrote', path.join('demo/screenshots', name + '.png'));
      await p.close();
    }
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
