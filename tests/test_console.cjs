/* UI regressions. node tests/test_console.cjs
 * Uses an installed Playwright and Chromium; origin checks use a temporary loopback bus.
 * Optional: ARKE_PLAYWRIGHT_MODULE=/path/to/playwright, ARKE_CHROME=/path/to/chrome.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { spawn, spawnSync } = require('node:child_process');
const read = name => fs.readFileSync(path.join(path.dirname(__dirname), 'web', name), 'utf8');  // the pages live in ../web
// the event-log reader both analytics pages' load() calls
const readEventsSrc = (s => s.slice(s.indexOf('async function readEvents('), s.indexOf('// ---------- view state')))(read('analytics-page.js'));
const readAsset = name => fs.readFileSync(path.join(path.dirname(__dirname), '.local/web', name), 'utf8');
function playwright() {
  try { return require(process.env.ARKE_PLAYWRIGHT_MODULE || 'playwright'); }
  catch { throw new Error('Run make setup-browser, or set ARKE_PLAYWRIGHT_MODULE to an existing installation.'); }
}
const chrome = chromium => process.env.ARKE_CHROME || chromium.executablePath();

async function markdownIsInert() {
  // md() runs on escaped text; hostile markdown must never produce a tag, an event handler or a non-http(s) link.
  const html = read('console.html'), base = read('base.js');
  const src = base.slice(base.indexOf('const esc = '), base.indexOf('\n', base.indexOf('const esc = ')))
    + '\n' + html.slice(html.indexOf('const BARE_URL'), html.indexOf('// optional syntax highlighting'));
  const inputs = [
    '[a](http://x![b](http://q/onmouseover=alert(1)//))',  // nested link: the stashed inner link was spliced into the href
    '![a](http://x[b](http://q/"onerror=alert(1)//))',
    'http://x`code"onmouseover=alert(1)`y',
    '[`c`](http://x`d`) and http://y\\*z',
    '<img src=x onerror=alert(1)> <script>alert(1)</script>',
    '[js](javascript:alert(1)) [data](data:text/html,<b>x</b>)',
    '\u00000\u0000 [x](http://a\u00000\u0000)',
    '| a | b |\n|---|---|\n| [x](http://q/"a) | `y` |',
    '> [q](http://x![i](http://y/onload=1))\n- [l](http://z`w`)',
  ];
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const page = await browser.newPage();
    await page.setContent('<div id="out"></div>');
    const bad = await page.evaluate(([src, inputs]) => {
      const md = new Function(src + '; return s => md(esc(s));')();
      const out = document.getElementById('out'), problems = [];
      const allowed = new Set(['href', 'target', 'rel', 'class', 'style', 'start', 'type', 'disabled', 'checked']);
      for (const input of inputs) {
        out.innerHTML = md(input);
        for (const el of out.querySelectorAll('*')) {
          if (!['A', 'B', 'I', 'S', 'P', 'BR', 'CODE', 'PRE', 'DIV', 'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD', 'UL', 'OL', 'LI', 'INPUT', 'BLOCKQUOTE'].includes(el.tagName))
            problems.push(`${input}: <${el.tagName}>`);
          for (const a of el.attributes) if (!allowed.has(a.name)) problems.push(`${input}: attribute ${a.name}`);
          if (el.tagName === 'A' && !/^https?:\/\//.test(el.getAttribute('href') || '')) problems.push(`${input}: href ${el.getAttribute('href')}`);
        }
      }
      return problems;
    }, [src, inputs]);
    assert.deepEqual(bad, []);
    // and ordinary markdown still renders
    const ok = await page.evaluate(src => new Function(src + '; return md(esc("[l](https://a.com/x) **b** `c` https://b.com/(w)"));')(), src);
    assert.equal(ok, '<p><a href="https://a.com/x" target="_blank" rel="noopener noreferrer">l</a> <b>b</b> <code>c</code> <a href="https://b.com/(w)" target="_blank" rel="noopener noreferrer">https://b.com/(w)</a></p>');
  } finally { await browser.close(); }
  console.log('ok: hostile markdown renders no tags, event handlers or non-http(s) links');
}

function analyticsIdentity() {
  const shared = read('analytics-shared.js'), { eventNames: resolve, buildModel } = vm.runInNewContext(shared + '; ({ eventNames, buildModel });');
  // Exercise the displayed unanswered count, including ownership changes and name reuse.
  const html = read('analytics.html');
  const chartSeries = vm.runInNewContext(shared + html.slice(html.indexOf('function tsBuckets('), html.indexOf('function tsTotal(')) + `
    const NB = 10, inProj = () => true, nowS = () => 10;
    let M;
    events => { M = buildModel(events, [], 10); return tsBuckets(0, 10); };
  `);
  const renderWaiting = vm.runInNewContext(shared + html.slice(html.indexOf('function renderTalk('), html.indexOf('function renderNow(')) + `
    const elements = {}, $ = id => elements[id] ||= { innerHTML: '' };
    const inProj = () => true, colorOf = () => '#123456', esc = String, agentLink = String;
    const clock = String, dur = String, pair = null;
    let M;
    events => { M = buildModel(events, [], 100); renderTalk(0, 100, []); return elements.flow.innerHTML; };
  `);
  for (const legacy of [false, true]) {
    const direct = name => legacy ? name : [{ name, kind: 'agent' }];
    const events = [
      { ts: 1, event: 'join', agent: 'alice', sid: 'one' },
      { ts: 2, event: 'message', id: 1, from: 'human', to: direct('alice'), text: 'Please respond' },
    ];
    assert.equal((renderWaiting(events).match(/1 waiting/g) || []).length, 1);
    events.push({ ts: 3, event: 'rename', old: 'alice', agent: 'bob', sid: 'one' });
    assert.equal((renderWaiting(events).match(/1 waiting/g) || []).length, 1, 'renaming keeps the unanswered count');
    events.push(
      { ts: 4, event: 'join', agent: 'alice', sid: 'two' },
      { ts: 5, event: 'message', id: 2, from: 'human', to: direct('alice'), reach: ['alice'], text: 'A separate request' },
      { ts: 6, event: 'takeover', agent: 'bob', new_name: 'carol', old_sid: 'one', new_sid: 'three' },
      { ts: 7, event: 'join', agent: 'carol', sid: 'three' },
    );
    const original = JSON.stringify(events), model = buildModel(events, [], 100);
    assert.equal(model.msgs[0].to[0].name, 'carol', 'the inherited mailbox owns the original request');
    assert.equal(model.msgs[0].reach[0], 'carol');
    assert.equal(model.msgs[1].to[0].name, 'alice', 'a reused name keeps its own request');
    assert.equal((renderWaiting(events).match(/1 waiting/g) || []).length, 2);
    assert.equal(JSON.stringify(events), original, 'historical events stay unchanged');
    events.push({ ts: 8, event: 'message', from: 'carol', to: 'human', text: 'Done' });
    assert.equal((renderWaiting(events).match(/1 waiting/g) || []).length, 1, 'only the answered request clears');
  }
  for (const file of ['analytics-shared.js']) {
    for (const withSid of [false, true]) {
      const sid = n => withSid ? { sid: n } : {};
      const events = [
        { event: 'tool', agent: 'alpha', ...sid('one') },
        { event: 'message', from: 'alpha', reach: ['helper'], to: [{ name: 'helper', kind: 'agent' }] },
        { event: 'procs', agents: { alpha: { cpu: 42 } } },
        { event: 'rename', old: 'alpha', agent: 'beta', ...sid('one') },
        { event: 'join', agent: 'alpha', ...sid('two') },
        { event: 'tool', agent: 'alpha', ...sid('two') },
        { event: 'message', from: 'helper', reach: ['alpha'], to: 'alpha' },
        { event: 'procs', agents: { alpha: { cpu: 1 }, beta: { cpu: 42 } } },
        { event: 'rename', old: 'beta', agent: 'gamma', ...sid('one') },
      ];
      const name = resolve(events);
      for (const i of [0, 1, 2]) assert.equal(name(events[i], 'alpha'), 'gamma', file + ': old history follows its owner');
      for (const i of [4, 5, 6, 7]) assert.equal(name(events[i], 'alpha'), 'alpha', file + ': reused name keeps its own history');
      assert.equal(name(events[7], 'beta'), 'gamma');
      assert.equal(name(events[6], 'helper'), 'helper');
      const modelEvents = events.map((e, i) => ({ ...e, ts: i + 1, tool: 'Read', target: 'file-' + i }));
      const model = buildModel(modelEvents, [], 100);
      assert.equal(model.lanes.get('gamma').tools[0].target, 'file-0');
      assert.equal(model.lanes.get('alpha').tools[0].target, 'file-5');
      assert.equal(model.msgs[0].from, 'gamma');
      assert.equal(model.msgs[1].reach[0], 'alpha');
      assert.equal(model.procs[0].agents.gamma.cpu, 42);
      assert.equal(model.procs[1].agents.alpha.cpu, 1);
    }
    const takeover = [{ event: 'tool', agent: 'alpha', sid: 'one' },
      { event: 'takeover', agent: 'alpha', new_name: 'beta', old_sid: 'one', new_sid: 'two' },
      { event: 'join', agent: 'beta', sid: 'two' }, { event: 'rename', old: 'beta', agent: 'gamma', sid: 'two' },
      { event: 'join', agent: 'alpha', sid: 'three' }];
    const name = resolve(takeover);
    assert.equal(name(takeover[0], 'alpha'), 'gamma');
    assert.equal(name(takeover[4], 'alpha'), 'alpha');
    {
      const html = read('analytics3d.html'), pstart = html.indexOf('function namedProcs('), pend = html.indexOf('// ---------- side-plane data:', pstart);
      const procs = vm.runInNewContext(shared + html.slice(pstart, pend) + '; namedProcs;');
      const samples = [{ ts: 1, event: 'procs', agents: { alpha: { cpu: 42 } }, started: [{ agent: 'alpha', pid: 10 }] },
        { ts: 4, event: 'procs', agents: { alpha: { cpu: 1 }, beta: { cpu: 42 } }, ended: [{ agent: 'beta', pid: 10 }] }];
      const resolved = procs([{ ts: 2, event: 'rename', old: 'alpha', agent: 'beta' }, { ts: 3, event: 'join', agent: 'alpha' }], samples);
      assert.equal(resolved[0].agents.beta.cpu, 42);
      assert.equal(resolved[0].started[0].agent, 'beta');
      assert.equal(resolved[1].agents.alpha.cpu, 1);
      assert.equal(resolved[1].ended[0].agent, 'beta');
      // Both agents existed at the same sample time before one inherited the other's mailbox.
      const overlap = { ts: 3, event: 'procs', agents: {
        alpha: { cpu: 30, rss_mb: 100, n: 1, top: [{ pid: 10, cpu: 30 }] },
        beta: { cpu: 40, rss_mb: 200, n: 2, top: [{ pid: 20, cpu: 40 }] },
      } };
      const history = [{ ts: 1, event: 'join', agent: 'alpha', sid: 'one' },
        { ts: 2, event: 'join', agent: 'beta', sid: 'two' },
        { ts: 4, event: 'takeover', agent: 'alpha', old_sid: 'one', new_sid: 'two', new_name: 'beta' },
        { ts: 5, event: 'rename', old: 'beta', agent: 'alpha', sid: 'two' },
        { ts: 6, event: 'join', agent: 'beta', sid: 'three' }];
      const reused = { ts: 7, event: 'procs', agents: { beta: { cpu: 5, rss_mb: 50 } } };
      const original = JSON.stringify(overlap);
      const totals = buildModel([...history, overlap, reused].sort((a, b) => a.ts - b.ts), [], 10).procs;
      assert.equal(JSON.stringify(totals.map(e => e.agents)), JSON.stringify([
        { alpha: { cpu: 70, rss: 300 } }, { beta: { cpu: 5, rss: 50 } },
      ]));
      const merged = procs(history, [overlap, reused]);
      assert.equal(merged[0].agents.alpha.cpu, 70);
      assert.equal(merged[0].agents.alpha.rss_mb, 300);
      assert.equal(merged[0].agents.alpha.n, 3);
      assert.equal(JSON.stringify(merged[0].agents.alpha.top.map(p => p.pid)), '[10,20]');
      assert.equal(merged[1].agents.beta.cpu, 5);
      assert.equal(JSON.stringify(overlap), original, 'building either graph leaves the raw sample intact');
      for (const name of ['alpha', 'constructor', '__proto__', 'toString']) {
        const sample = { ts: 8, event: 'procs', agents: { [name]: { cpu: 2, rss_mb: 10 } } };
        const charts = chartSeries([sample]);
        assert.deepEqual([...charts.cpu.keys()], [name], 'CPU chart includes ' + name);
        assert.deepEqual([...charts.rss.keys()], [name], 'memory chart includes ' + name);
        assert.equal(charts.cpu.get(name)[8], 2);
        assert.equal(charts.rss.get(name)[8], 10);
        assert.deepEqual(Object.keys(procs([], [sample])[0].agents), [name]);
        assert.equal(procs([], [sample])[0].agents[name].cpu, 2);
      }
    }
  }
  console.log('ok: both analytics pages preserve renamed owners, reused names, legacy events, and takeovers');
  // Preferences and bulk colors must preserve these names too, through JSON and back.
  const names = ['alpha', 'constructor', '__proto__', 'toString'];
  const page = read('analytics-page.js'), base = read('base.js'), consoleHtml = read('console.html');
  let saved = '';
  const ctx = vm.createContext({ GKEY: 'graph', console,
    localStorage: { setItem: (_, value) => { saved = value; }, getItem: () => saved },
    agents: names.map(name => ({ name, projects: {} })), projects: [],
  });
  vm.runInContext(page.slice(page.indexOf('function savedPositions('), page.indexOf('// the who-talks-to-whom'))
    + base.slice(base.indexOf('const hsl ='), base.indexOf('const fnv ='))
    + consoleHtml.slice(consoleHtml.indexOf('function rerollColors('), consoleHtml.indexOf('// random:')), ctx);
  const positions = new Map(names.map((name, i) => [name, { x: i / 10, y: 0.5, pinned: true }]));
  ctx.writePositions(positions);
  assert.deepEqual(JSON.parse(JSON.stringify([...ctx.savedPositions()])), [...positions], 'every name keeps its saved position and pin');
  const colors = JSON.parse(JSON.stringify(ctx.rerollColors()));
  assert.deepEqual(Object.keys(colors).sort(), [...names].sort(), 'bulk colors include every agent');
  for (const color of Object.values(colors)) assert.match(color, /^#[0-9a-f]{6}$/);
}

function analyticsFileTargets() {
  const filesOf = vm.runInNewContext(read('analytics-shared.js') + '; filesOf;');
  for (const tool of ['Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'read_file', 'write_file', 'edit_file', 'str_replace']) {
    for (const target of ['/repo/My Report.md', 'My Report.md', './sub/../My Report.md', 'notes; final.md']) {
      const files = filesOf({ tool, target, cwd: '/repo', host: 'local' });
      const expected = '/repo/' + (target === 'notes; final.md' ? target : 'My Report.md');
      assert.equal(files.length, 1, tool + ': one explicit file target');
      assert.equal(files[0].path, expected, tool + ': preserve the complete path');
      assert.equal(files[0].key, JSON.stringify(['local', expected]));
      assert.equal(files[0].write, !['Read', 'read_file'].includes(tool));
    }
  }
  assert.equal(filesOf({ tool: 'Grep', target: 'some pattern' }).length, 0, 'search text is not a filename');
  for (const [tool, target] of [['Grep', 'TODO'], ['Glob', 'src/**/*.py'], ['WebFetch', 'https://example.com/a.html'], ['LS', '/repo/sub'], ['search', 'needle']])
    assert.equal(filesOf({ tool, target, cwd: '/repo' }).length, 0, tool + ': a pattern, URL or directory is not a file');
  console.log('ok: explicit file tools preserve paths with spaces and shell punctuation');
}

async function analyticsFiles() {
  const raw = [
    { event: 'tool', ts: 1, agent: 'one', host: 'local', cwd: '/repo', tool: 'Edit', target: 'README.md' },
    { event: 'tool', ts: 2, agent: 'two', host: 'remote', cwd: '/repo', tool: 'Read', target: 'README.md' },
    { event: 'tool', ts: 3, agent: 'one', host: 'local', cwd: '/repo/sub', tool: 'Bash', target: 'cat ../README.md; echo x >> ../README.md' },
  ];
  const extract = (html, start, end) => {
    const a = html.indexOf(start), b = html.indexOf(end, a + start.length);
    assert(a >= 0 && b > a, 'missing source: ' + start); return html.slice(a, b);
  };
  let ctx;
  for (const page of ['analytics.html', 'analytics3d.html']) {
    const html = read(page);
    ctx = vm.createContext({ raw, M: null, nowS: () => 10, agentsNow: [{ host: 'local', local: true }], console });
    vm.runInContext(read('analytics-shared.js')
      + extract(read('analytics-page.js'), 'const fileAt =', 'let paneResized')
      + extract(read('analytics-page.js'), 'function build()', '\n') + '; build();', ctx);
    const [local, remote, normalized] = ctx.M.touches;
    assert.equal(ctx.M.touches.length, 3);
    assert.equal(local.path, '/repo/README.md');
    assert.equal(local.key, normalized.key, page + ': relative shell paths share the normalized file');
    assert(normalized.write, page + ': a later write in the command is retained');
    assert.notEqual(local.key, remote.key, page + ': the host is part of the identity');
  }
  const shown = [];  // the side pane's file view counts only that file's touches
  Object.assign(ctx, { pane: { seq: 0 }, paneShow: (title, html) => shown.push(html), paneTimeline: () => '', paneAgentLink: String,
    esc: String, dayOf: String, clock: String, dur: String, $: () => null, api: async () => ({ available: false }) });
  vm.runInContext(extract(read('analytics-page.js'), 'async function paneFile(', 'const paneAgentLink'), ctx);
  for (const [t, counts] of [[ctx.M.touches[0], '2 edits, 0 reads'], [ctx.M.touches[1], '0 edits, 1 reads']]) {
    shown.length = 0; await ctx.paneFile(t.key);
    assert(shown[0].includes(counts), `the ${t.host} file pane says ${counts}`);
  }
  const html = read('analytics3d.html'), [local, remote] = ctx.M.touches, requests = [];
  const junkPath = vm.runInNewContext(extract(html, 'const junkPath', '\n\n') + '; junkPath;', { agentsNow: [{ cwd: '/repo' }], under: (p, q) => p.startsWith(q + '/') });
  assert.equal(junkPath('/repo/scratchpad/notes.md'), false, 'a project with a scratchpad/ dir keeps its files');
  assert.equal(junkPath('/tmp/claude-1/scratchpad/x.md'), true, 'temp files outside every agent stay out');
  Object.assign(ctx, { w0: 0, w1: 10, cut: 0, rebuildGen: 0, junkPath: () => false, cityRoot: '', inProj: () => true,
    api: async url => { requests.push(url); return { '/repo/README.md': 123 }; }, URLSearchParams, CW: 200, CD: 200 });
  const files = vm.runInContext(extract(html, '  // ----- files touched in the window', '  // everything that waits') + '; files;', ctx);
  assert.equal(files.size, 2, 'same path on two hosts makes two buildings');
  assert.equal(files.get(local.key).edits, 2);
  assert.equal(files.get(remote.key).reads, 1);
  vm.runInContext(extract(html, 'const sizes = new Map()', '// shell-parsed junk'), ctx);
  await ctx.fetchSizes([...files.values()]);
  assert.equal(requests.length, 1);
  assert.deepEqual(new URL(requests[0], 'http://test').searchParams.getAll('path'), ['/repo/README.md']);
  const sizes = vm.runInContext('sizes', ctx);
  assert.equal(sizes.get(local.key).size, 123); assert(!sizes.has(remote.key), 'remote file never borrows the local size');
  await ctx.fetchSizes([remote]); assert.equal(requests.length, 1, 'remote-only batches never request sizes');

  vm.runInContext(extract(html, 'function squarify(', '// ---------- scene'), ctx);
  const top = { path: '/repo', dirs: new Map(), files: [{ ...local, value: 10 }], value: 10 };
  ctx.planCity(top, 'test');
  const rects = vm.runInContext('plan.rects', ctx), first = rects.get(local.key);
  top.files.push({ ...remote, value: 0.1 }); top.value += 0.1;
  assert.equal(ctx.planCity(top, 'test'), 'kept');
  assert.equal(rects.get(local.key), first, 'adding another host preserves the original plot');
  assert(rects.has(remote.key)); assert.notDeepEqual(rects.get(remote.key), first, 'the new host gets its own plot');

  const localFile = key => vm.runInContext(`isLocalFile(fileAt(${JSON.stringify(key)}), agentsNow)`, ctx);
  assert.equal(localFile(local.key), true);
  assert.equal(localFile(remote.key), false);
  const painted = [];
  Object.assign(ctx, {
    document: { documentElement: {}, createElement: () => ({ getContext: () => ({ fillRect() {}, fillText: text => painted.push(text) }) }) },
    THREE: { CanvasTexture: class {} }, requestRender() {}, getComputedStyle: () => ({ getPropertyValue: () => '#fff' }),
    api: async url => { requests.push(url); return { text: 'LOCAL CONTENTS' }; },
  });
  vm.runInContext(extract(html, 'const near = new Map()', 'function sideTexture('), ctx);
  requests.length = 0;
  ctx.roofTexture(local); await new Promise(setImmediate); ctx.roofTexture(local);
  assert.equal(requests.length, 1); assert(painted.includes('LOCAL CONTENTS'));
  painted.length = 0; ctx.roofTexture(remote); await new Promise(setImmediate);
  assert.equal(requests.length, 1, 'the remote roof never fetches local file contents');
  assert(!painted.includes('LOCAL CONTENTS'), 'the remote roof never reuses local cached contents');
  assert(painted.some(text => text.includes('only for absolute paths on this server')));

  const blds = [
    { f: { host: 'local', path: '/repo-a/src/lib/parser.py' } },
    { f: { host: 'local', path: '/repo-b/src/lib/parser.py' } },
    { f: { host: 'remote', path: '/repo-a/src/lib/parser.py' } },
    { f: { host: 'remote', path: '/repo-a/remote.py' } },
  ];
  const buildingAt = vm.runInNewContext(read('analytics-shared.js')
    + extract(html, '    const byPath = new Map()', '    drawPanel(w0, w1, Y, P, buildingAt);') + '; buildingAt;',
    { blds, agentsNow: ctx.agentsNow });
  assert.equal(buildingAt('/repo-a', 'src/lib/parser.py'), blds[0]);
  assert.equal(buildingAt('/repo-b', 'src/lib/parser.py'), blds[1]);
  assert.equal(buildingAt('/repo-c', 'src/lib/parser.py'), undefined, 'a matching suffix in another repo is not this file');
  assert.equal(buildingAt('/repo-a', 'remote.py'), undefined, 'local commits never link to remote-only buildings');

  const gitCtx = vm.createContext({ agentsNow: [{ cwd: '/repo-a/sub', local: true }, { cwd: '/repo-a/nested', local: true }],
    M: { lanes: new Map() }, X3: {}, nowS: () => 100, projSel: () => '', under: (p, q) => p === q || p.startsWith(q + '/'),
    api: async url => ({ repo: url.includes('nested') ? '/repo-a/nested' : '/repo-a', commits: [] }) });
  vm.runInContext(extract(html, 'async function loadGit(', '// the bus machine'), gitCtx);
  await gitCtx.loadGit(0, 100);
  assert.deepEqual(Array.from(gitCtx.X3.git.keys()), ['/repo-a', '/repo-a/nested'], 'nested repositories are resolved independently');
  console.log('ok: both file models, 3D buildings, sizes, growing plots, details, and roof caches keep hosts separate');
  console.log('ok: Git links match exact local paths, and nested repositories keep their own history');
}

async function analyticsRoofRefresh() {
  const html = read('analytics3d.html'), requests = [];
  const file = { key: 'local-file', host: 'local', path: '/repo/report.py', last: 1 };
  const ctx = vm.createContext({ agentsNow: [{ host: 'local', local: true }], fileLabel: f => f.path, console,
    document: { createElement() {
      const canvas = { painted: [] };
      canvas.getContext = () => ({ fillRect() { canvas.painted.length = 0; }, fillText(text) { canvas.painted.push(text); } });
      return canvas;
    } },
    THREE: { CanvasTexture: class { constructor(image) { this.image = image; } } }, requestRender() {}, getComputedStyle: () => ({ getPropertyValue: () => '#fff' }),
    api: url => new Promise(resolve => requests.push({ url, resolve })),
  });
  vm.runInContext(read('analytics-shared.js')
    + html.slice(html.indexOf('const near = new Map()'), html.indexOf('function sideTexture(')), ctx);
  const near = vm.runInContext('near', ctx);
  const show = f => {
    const texture = ctx.roofTexture(f);
    near.set(f.key, { mats: [{ map: texture }] });
    return texture;
  };
  show(file);
  requests[0].resolve({ text: 'version one' }); await new Promise(setImmediate);
  assert(show(file).image.painted.includes('version one'));
  assert.equal(requests.length, 1, 'unchanged files reuse their cached contents');

  const updated = show({ ...file, last: 2 });
  assert.equal(requests.length, 2, 'new file activity refreshes roof text');
  requests[1].resolve({ text: 'version two' }); await new Promise(setImmediate);
  assert(updated.image.painted.includes('version two'), 'the visible roof repaints when refreshed text arrives');

  const obsolete = show({ ...file, last: 3 });
  show({ ...file, last: 4 });
  const recreated = show({ ...file, last: 4 }); // scene rebuild while the current fetch is still pending
  assert.equal(requests.length, 4, 'recreating a roof shares the pending request for its version');
  requests[3].resolve({ text: 'version four' }); await new Promise(setImmediate);
  assert(recreated.image.painted.includes('version four'), 'the replacement texture receives the pending result');
  requests[2].resolve({ text: 'version three' }); await new Promise(setImmediate);
  assert(!obsolete.image.painted.includes('version three'), 'an obsolete request does not repaint a removed texture');
  assert(show({ ...file, last: 4 }).image.painted.includes('version four'), 'a delayed older reply cannot replace newer cached text');
  assert.equal(requests.length, 4);

  ctx.agentsNow = [{ host: 'local', local: false }];
  const unavailable = show({ ...file, last: 4 });
  assert(!unavailable.image.painted.includes('version four'), 'a file no longer local cannot reuse readable contents');
  assert.equal(requests.length, 4);
  ctx.agentsNow = [{ host: 'local', local: true }];
  const localAgain = show({ ...file, last: 4 });
  assert.equal(requests.length, 5, 'a file becoming available on this server can be fetched again');
  requests[4].resolve({ text: 'version five' }); await new Promise(setImmediate);
  assert(localAgain.image.painted.includes('version five'));
  console.log('ok: roof previews refresh after file activity, share pending requests, and ignore obsolete replies');
}

async function analyticsSizeRefresh() {
  const html = read('analytics3d.html'), requests = [];
  let now = 10, bytes = null;
  const file = { key: 'local-file', host: 'local', path: '/repo/report,a.py', last: 1 };
  const ctx = vm.createContext({ URLSearchParams, nowS: () => now, rebuildGen: 0,
    agentsNow: [{ host: 'local', local: true }], console,
    api: async url => {
      requests.push(url);
      assert.deepEqual(new URL(url, 'http://test').searchParams.getAll('path'), [file.path]);
      return { [file.path]: bytes };
    } });
  vm.runInContext(read('analytics-shared.js')
    + html.slice(html.indexOf('const sizes = new Map()'), html.indexOf('// shell-parsed junk')), ctx);
  const sizes = vm.runInContext('sizes', ctx);
  await ctx.fetchSizes([file]);
  assert.equal(sizes.get(file.key).size, null);
  bytes = 8; file.last = 2;
  await ctx.fetchSizes([file]);
  assert.equal(sizes.get(file.key).size, 8, 'a newly created file replaces cached unknown size');
  bytes = 16; file.last = 3;
  await ctx.fetchSizes([file]);
  assert.equal(sizes.get(file.key).size, 16, 'new activity refreshes an existing size');
  await ctx.fetchSizes([file]);
  assert.equal(requests.length, 3, 'unchanged files reuse their fresh sizes');
  now += 31; bytes = 24;
  await ctx.fetchSizes([file]);
  assert.equal(sizes.get(file.key).size, 24, 'expiry also catches writes outside agent hooks');

  file.last = 6; ctx.api = async () => { throw new Error('temporary failure'); };
  await assert.rejects(ctx.fetchSizes([file]), /temporary failure/);
  ctx.api = async () => ({ [file.path]: 48 });
  await ctx.fetchSizes([file]);
  assert.equal(sizes.get(file.key).size, 48, 'failed requests can be retried immediately');
  console.log('ok: file sizes preserve commas, refresh after activity or expiry, and retry after failures');
}

async function analyticsSizeBatching() {
  const html = read('analytics3d.html'), requests = [];
  const directory = '/repo/' + ['測'.repeat(40), '量'.repeat(40), '資'.repeat(40)].join('/');
  const files = Array.from({ length: 80 }, (_, i) => ({
    key: 'long-' + i, host: 'local', path: `${directory}/file ${i},&+.py`, last: 1,
  }));
  const ctx = vm.createContext({ URLSearchParams, nowS: () => 10, rebuildGen: 0,
    agentsNow: [{ host: 'local', local: true }], console,
    api: async url => {
      assert(Buffer.byteLength(`GET ${url} HTTP/1.1\r\n`) <= 65536, 'encoded paths must fit the HTTP request-line limit');
      const paths = new URL(url, 'http://test').searchParams.getAll('path');
      assert(paths.length <= 80);
      requests.push(paths);
      return Object.fromEntries(paths.map(p => [p, 123]));
    } });
  vm.runInContext(read('analytics-shared.js')
    + html.slice(html.indexOf('const sizes = new Map()'), html.indexOf('// shell-parsed junk')), ctx);
  await ctx.fetchSizes(files);
  assert.equal(requests.length, 2, 'long paths split efficiently into two requests');
  assert.deepEqual(requests.flat(), files.map(f => f.path), 'every path survives encoding exactly once');
  const sizes = vm.runInContext('sizes', ctx);
  for (const f of files) assert.equal(sizes.get(f.key).size, 123);
  ctx.rebuildGen++;
  await ctx.fetchSizes(files);
  assert.equal(requests.length, 2, 'long paths reuse the same fresh cache');
  console.log('ok: Unicode-heavy file paths stay below the HTTP request limit without extra cache requests');
}

async function analyticsHistoryRaces() {
  const html = read('analytics3d.html');
  const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start)));
  const record = ts => ({ timestamp: new Date(ts * 1000).toISOString(), cpu: { avg: 1 } });
  for (const staleFails of [false, true]) {
    const requests = [];
    let now = 18000;
    const ctx = vm.createContext({ X3: {}, agentsNow: [{ cwd: '/repo', local: true }], M: { lanes: new Map() },
      projSel: () => '', nowS: () => now, under: () => true,
      api: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject })) });
    vm.runInContext(extract('async function loadGit(', '// the bus machine')
      + extract('async function loadHoratio(', '// ---------- the two side planes'), ctx);
    const load = since => Promise.all([ctx.loadGit(since, 18000), ctx.loadHoratio(since, 18000)]);
    const reply = (at, stamps) => {
      requests[at].resolve({ repo: '/repo', commits: stamps });
      requests[at + 1].resolve({ records: stamps.map(record) });
    };
    const state = () => ({ git: Array.from(ctx.X3.git.get('/repo')), hor: Array.from(ctx.X3.hor, r => r.ts),
      gitNote: ctx.X3.gitNote, horNote: ctx.X3.horNote });
    const narrow = load(17100), wide = load(14400);
    let joinedDone = false;
    const joined = load(14400).then(() => { joinedDone = true; });
    await new Promise(setImmediate);
    assert.equal(requests.length, 4, 'same range shares pending requests');
    assert.equal(joinedDone, false, 'a rebuild waits for its pending history');
    reply(2, [15000, 17500]); await Promise.all([wide, joined]);
    const expected = { git: [15000, 17500], hor: [15000, 17500], gitNote: '', horNote: '' };
    assert.deepEqual(state(), expected);
    if (staleFails) {
      for (const r of requests.slice(0, 2)) r.reject(Object.assign(new Error('late failure'), { status: 404 }));
    } else reply(0, [17500]);
    await narrow; await load(14400);
    assert.deepEqual(state(), expected, 'late success or failure cannot replace current history or notes');
    assert.equal(requests.length, 4, 'completed current history remains cached');

    // Leave the wide range and return while both new requests are still pending.
    const away = load(17100), back = load(14400);
    reply(6, [15000, 17500]); await back;
    reply(4, [17500]); await away;
    assert.deepEqual(state(), expected, 'returning to a range still rejects superseded replies');

    now += 301;
    const refreshed = load(14400);
    assert.equal(requests.length, 10, 'expired histories fetch again');
    reply(8, [15000, 17500, 17900]); await refreshed;
    assert.deepEqual(state().git, [15000, 17500, 17900]);
    assert.deepEqual(state().hor, [15000, 17500, 17900]);
  }
  console.log('ok: 3D history waits for shared requests, rejects stale replies and errors, and refreshes expired caches');
}

function analyticsStressLayout() {
  const file = 'graph-layout.js', layout = vm.runInNewContext(read(file) + '; layoutTargets;');
  assert.equal(layout('stress', [], [], 600, 480).size, 0);
  const solo = { name: 'solo', px: 75, py: 110, r: 5, pinned: false };
  assert.deepEqual(Array.from(layout('stress', [solo], [], 600, 480).get('solo')), [300, 240], file + ': a lone agent is centered');
  assert.equal(layout('stress', [{ ...solo, pinned: true }], [], 600, 480).size, 0, file + ': a lone pinned agent stays fixed');
  const pair = [solo, { ...solo, name: 'other', px: 200, py: 300 }];
  const isolated = layout('stress', pair, [], 600, 480);
  assert.equal(isolated.size, 2);
  for (const target of isolated.values()) assert(target.every(Number.isFinite), file + ': disconnected agents stay finite');
  // Keep one node free: all 400 nodes contribute distances without 400 nodes' relaxation work.
  const nodes = Array.from({ length: 400 }, (_, i) => ({ name: 'agent-' + i, px: i % 20 * 20, py: Math.floor(i / 20) * 20, r: 5, pinned: i !== 0 }));
  const edges = nodes.slice(1).map((node, i) => ({ na: nodes[i], nb: node, w: 1 }));
  for (const connected of [true, false]) {
    const targets = layout('stress', nodes, connected ? edges : [], 600, 480);
    assert.equal(targets.size, 1, file + ': pinned nodes stay fixed');
    assert(targets.get('agent-0').every(Number.isFinite), file + ': large Stress layout returns finite positions');
  }
  console.log('ok: shared Stress layout handles empty, single, pinned, disconnected, and 400-agent graphs');
}

async function consoleUI() {
  const help = read('console.html');
  for (const id of ['cols-auto', 'cols-reset']) {
    const label = help.match(new RegExp(`id="${id}"[^>]*>([^<]+)<`))[1];  // the button's text
    assert(help.includes(`"${label}"`), `the help names the "${label}" button`);
  }
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const agent = name => ({ name, kind: 'codex', state: 'idle', host: 'test', projects: { '/test': true }, files: [], started_at: 1, last_active: 1, unread: 0, reachable: 'yes', cwd: '/tmp/test' });
    const message = id => ({ id, ts: 1700000000 + id, from: 'alpha', to: [{ kind: 'agent', name: 'beta' }], reach: ['beta'], text: 'message ' + id, host: 'test', projects: ['/test'] });
    let messages = Array.from({ length: 200 }, (_, i) => message(i + 1));
    let failBefore = 0;
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
      if (['/auth.js', '/base.js', '/analytics-shared.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      if (u.hostname !== 'arke-review.test') return route.abort();
      let data;
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: read('console.html') });
      if (u.pathname === '/agents') data = [agent('alpha'), agent('beta')];
      else if (u.pathname === '/projects') data = [{ path: '/test', n: 2 }];
      else if (u.pathname.endsWith('/transcript')) data = { available: false, reason: 'mock transcript' };
      else if (u.pathname === '/log') {
        if (failBefore && Number(u.searchParams.get('before')) === failBefore)
          return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"test outage"}' });
        const name = u.searchParams.get('agent');
        data = messages.filter(m => (!name || m.from === name || m.reach.includes(name)) && m.id > Number(u.searchParams.get('since') || 0)
          && m.id < Number(u.searchParams.get('before') || Infinity)).slice(-Number(u.searchParams.get('limit') || 200));
      } else if (u.pathname === '/resolve') data = { has_project: false, unknown: [], unsubscribed: [], reach: ['alpha'], unreachable: [] };
      else data = {};
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await page.addInitScript(() => localStorage.setItem('ab.pins', JSON.stringify(['alpha', 'beta'])));
    await page.goto('http://arke-review.test/#agent/alpha');
    await page.click('#strip a[data-pin="beta"]');
    await page.waitForFunction(() => location.hash === '#agent/beta');
    assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('ab.pins'))), ['alpha', 'beta']);
    await page.click('#agent-head button.pin');
    assert.deepEqual(await page.evaluate(() => JSON.parse(localStorage.getItem('ab.pins'))), ['alpha']);
    await page.goto('http://arke-review.test/');
    await page.waitForFunction(() => document.querySelectorAll('#feed .msg').length === 200);
    const poll = () => page.evaluate(async () => { await poll(true); clearTimeout(timer); });
    const ids = () => page.evaluate(() => [...document.querySelectorAll('#feed .msg')].map(li => Number(li.dataset.id)).sort((a, b) => a - b));
    messages = Array.from({ length: 1450 }, (_, i) => message(i + 1));
    failBefore = 1251;  // fail halfway through the missing interval: don't commit a partial cursor
    await poll();
    assert.equal(await page.evaluate(() => lastId), 200);
    assert.equal((await ids()).length, 200);
    failBefore = 0;
    await poll();
    assert.deepEqual(await ids(), messages.map(m => m.id));
    await poll();
    assert.equal((await ids()).length, 1450, 'repeated polls must not duplicate messages');
    await page.click('#feed-older');
    await page.waitForFunction(() => document.getElementById('feed-older').hidden);
    assert.equal((await ids()).length, 1450);
    // A hash-only navigation can reuse a stale pin cache and then catch up beyond the initial page.
    await page.evaluate(log => {
      setPins(['alpha', 'beta']);
      cache.set('beta', { tr: { available: false, reason: 'cached mock transcript' }, log, at: now() });
    }, messages.slice(0, 200));
    await page.goto('http://arke-review.test/#agent/beta/messages');
    await page.waitForFunction(() => view.name === 'beta' && lastId === 1450 && document.querySelectorAll('#feed .msg').length === 1450);
    assert.deepEqual(await ids(), messages.map(m => m.id));
    // Force a new document for the fresh-visit case. A fragment-only goto would retain the pin cache,
    // where a transient 200 rows could satisfy the wait while its catch-up request was still running.
    await page.goto('http://arke-review.test/?case=older#agent/beta/messages');
    await page.waitForFunction(() => document.querySelectorAll('#feed .msg').length === 200);
    await page.click('#feed-older');
    await page.waitForFunction(() => document.querySelectorAll('#feed .msg').length === 400);
    assert.deepEqual(await ids(), messages.slice(-400).map(m => m.id));
    assert.deepEqual(errors, []);
    console.log('ok: pin navigation, star toggles, burst pagination, failed-page retries, deduplication, and older pages');

    await page.goto('http://arke-review.test/#agent/beta/messages');
    await page.waitForFunction(() => document.querySelector('#feed a.name[data-c="beta"]'));
    assert.equal(await page.locator('#feed a.name[data-c="beta"]').first().evaluate(el => getComputedStyle(el).fontWeight), '700');
    const leaks = [];
    page.on('request', r => { if (new URL(r.url()).hostname === 'collector.test') leaks.push(r.url()); });
    const payload = '\n]{}body{--review-injected:yes}#tree [data-pick*=test]{background-image:url(http://collector.test/project)}/*';
    await page.goto('http://arke-review.test/#agent/' + encodeURIComponent(payload));
    await page.waitForFunction(name => view.name === name, payload);
    assert.equal(await page.locator('#tree [data-pick="/test"]').count(), 1);
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).getPropertyValue('--review-injected')), '');
    await page.waitForTimeout(100); // allow a stylesheet-triggered request to reach the intercepted network
    assert.deepEqual(leaks, [], 'an agent URL cannot probe rendered project names through CSS');
    assert.deepEqual(errors, []);
    console.log('ok: agent highlighting survives while crafted URL hashes cannot inject CSS or leak project names');
  } finally { await browser.close(); }
}

// Findings 14-16: run the real build()/load() in a vm against a scripted /events, for both pages.
async function analyticsLoad() {
  for (const file of ['analytics.html', 'analytics3d.html']) {
    const html = read(file), ts = 1700000000;
    const page = read('analytics-page.js'), model = vm.runInNewContext(read('analytics-shared.js') + page.slice(page.indexOf('function build()'), page.indexOf('\n', page.indexOf('function build()'))) + '; build(); M;', {
      raw: [{ event: 'join', agent: 'human-reviewer', sid: 'a', ts }, { event: 'message', from: 'human-reviewer', reach: ['human-ops'], to: [{ name: 'human-ops', kind: 'agent' }], ts: ts + 1 },
        { event: 'message', from: 'human', reach: ['alpha'], to: 'alpha', ts: ts + 2 }],
      M: null, agentsNow: [], nowS: () => ts + 20,
    });
    assert.equal(model.msgs[0].from, 'human-reviewer', file + ': an agent named human-* is not the person');
    assert.deepEqual([...model.msgs[0].reach], ['human-ops'], file + ': nor is a human-* recipient');
    assert.equal(model.msgs[1].from, 'human');
    const ls = html.indexOf('async function load()'), le = html.indexOf('// ---------- view state', ls);
    let clock = 100, size = 100, requests = [];
    const els = new Map(), ctx = {
      eventGeneration: "", raw: [], next: 0, loading: false, M: null, TYPES: 'tool', clock: String, console: { warn() {}, error: e => { throw e; } },
      nowS: () => clock, $: id => { if (!els.has(id)) els.set(id, {}); return els.get(id); },
      buildDefaults() {}, fillProjects() {}, render() {}, rebuild: async () => {}, showErr: m => { throw new Error(m); }, errSeen: new Map(),
      build() { ctx.M = { t1: Math.max(...ctx.raw.map(e => e.ts), clock) }; },
      api: async url => {
        if (!url.startsWith('/events')) return [];
        requests.push(url);
        const since = Number(new URL(url, 'http://x').searchParams.get('since'));
        if (missing) return { events: [], next: 0 };  // the server's answer while the log file doesn't exist
        if (since > size) throw Object.assign(new Error(`since=${since} is past the end of the log`), { status: 400 });
        const type = new URL(url, 'http://x').searchParams.get('type');
        return { events: since ? [] : [{ event: type === 'procs' ? 'procs' : 'tool', ts: 90, gen: size }], next: size };
      },
    };
    let missing = false;
    if (file === 'analytics3d.html') ctx.X3 = { procs: [], procsNext: 0, procsFrom: Infinity, procsGen: 0, procsNote: '' };
    vm.runInNewContext(html.slice(ls, le) + readEventsSrc + (file === 'analytics3d.html' ? html.slice(html.indexOf('async function offsetAt('), html.indexOf('async function loadGit(')) : ''), ctx);
    await ctx.load();
    clock = 3700; await ctx.load();
    assert.equal(ctx.M.t1, 3700, file + ': an idle reload still advances now');
    size = 50; await ctx.load();
    assert.equal(ctx.next, 50, file + ': a rotated log restarts from since=0');
    assert.equal(ctx.raw.length, 1, file + ': rereading after rotation does not double events');
    assert.match(els.get('conn').textContent, /reloaded from the start/, file + ': rotation is surfaced');
    await ctx.load();
    assert.equal(requests.at(-1).includes('since=50'), true, file + ': the new cursor sticks');
    // mid-rotation the log is missing and /events answers 200 with next 0; then a new log appears
    missing = true; await ctx.load(); missing = false; size = 80; await ctx.load();
    assert.equal(JSON.stringify(ctx.raw.map(e => e.gen)), '[80]', file + ': a missing log resets, so generations never mix');
    if (file === 'analytics3d.html') {  // the process samples follow a rotation too
      size = 100; await ctx.load(); await ctx.loadProcs(0);
      assert.equal(JSON.stringify(ctx.X3.procs.map(e => e.gen)), '[100]');
      size = 50; await ctx.load(); await ctx.loadProcs(0); await ctx.loadProcs(0);
      assert.equal(JSON.stringify([ctx.X3.procsNext, ctx.X3.procs.map(e => e.gen)]), '[50,[50]]', 'the process cursor restarts after a rotation');
    }
  }
  console.log('ok: both analytics pages keep human-* agents, restart after log rotation, and advance now on idle reloads');
}

async function analyticsRecovery() {
  const extract = (html, a, b) => html.slice(html.indexOf(a), html.indexOf(b, html.indexOf(a)));
  function context() {
    const elements = new Map(), ctx = {
      eventGeneration: "", raw: [], next: 0, loading: false, M: null, TYPES: 'tool', nowS: () => 200, clock: String,
      X3: { procs: [], procsNext: 0, procsFrom: Infinity, procsGen: 0, procsNote: '' },
      console: { warn() {}, error() {} }, $: id => { if (!elements.has(id)) elements.set(id, {}); return elements.get(id); },
      buildDefaults() {}, fillProjects() {}, render() {}, showErr() {}, rebuild: async () => {}, errSeen: new Map(),
      build() { ctx.M = { t0: 180, t1: 200, events: [...ctx.raw] }; },
    };
    vm.createContext(ctx); return ctx;
  }
  for (const file of ['analytics.html', 'analytics3d.html']) {
    const ctx = context(); let stage = 'before';
    ctx.api = async url => {
      if (!url.startsWith('/events')) {
        if (stage === 'fail-reset' && url === '/agents') throw new Error('temporary outage');
        return [];
      }
      if (stage === 'before') return { events: [{ event: 'tool', agent: 'old', ts: 100 }], next: 100 };
      if (Number(new URL(url, 'http://x').searchParams.get('since'))) throw Object.assign(new Error('rotated'), { status: 400 });
      return { events: [], next: 0 };
    };
    vm.runInContext(extract(read(file), 'async function load()', '// ---------- view state') + readEventsSrc, ctx);
    await ctx.load(); stage = 'fail-reset'; await ctx.load(); stage = 'after'; await ctx.load(); await ctx.load();
    assert.equal(ctx.M.events.length, 0, file + ': failed empty-log reset cannot preserve the old model');
    ctx.next = 0; ctx.eventGeneration = 'old-empty-file';
    ctx.api = async url => {
      if (!url.startsWith('/events')) return [];
      if (new URL(url, 'http://x').searchParams.get('generation') === 'old-empty-file')
        throw Object.assign(new Error('generation changed'), { status: 400 });
      return { events: [{ event: 'tool', agent: 'new', ts: 190 }], next: 100, generation: 'new-file' };
    };
    await ctx.load();
    assert.equal(ctx.eventGeneration, 'new-file', file + ': an empty or missing log can rotate at offset zero');
    assert.equal(ctx.raw.length, 1);
  }
  const ctx = context(), html = read('analytics3d.html');
  ctx.raw = [{ event: 'tool', gen: 'old', ts: 100 }]; ctx.next = 100; ctx.build();
  Object.assign(ctx.X3, { procs: [{ event: 'procs', gen: 'old', ts: 100 }], procsNext: 100, procsFrom: 0 });
  let release, hold = true;
  ctx.api = async url => {
    if (!url.startsWith('/events')) return [];
    const u = new URL(url, 'http://x'), since = Number(u.searchParams.get('since')), proc = u.searchParams.get('type') === 'procs';
    if (proc && hold) { hold = false; return new Promise(resolve => { release = () => resolve({ events: [{ event: 'procs', gen: 'OLD DELAYED', ts: 110 }], next: 120 }); }); }
    if (since > 50) throw Object.assign(new Error('rotated'), { status: 400 });
    return { events: since ? [] : [{ event: proc ? 'procs' : 'tool', gen: 'new', ts: 195 }], next: 50 };
  };
  vm.runInContext(extract(html, 'async function load()', '// ---------- view state') + readEventsSrc + extract(html, 'async function offsetAt(', 'async function loadGit('), ctx);
  ctx.rebuild = async () => ctx.loadProcs(180);
  const pending = ctx.loadProcs(0);
  await ctx.load(); release(); await pending; await ctx.loadProcs(180);
  assert.equal(ctx.X3.procsNext, 50);
  assert.equal(JSON.stringify(ctx.X3.procs.map(e => e.gen)), '["new"]');
  assert.equal(ctx.X3.procsNote, '');
  console.log('ok: failed empty-log resets and delayed process responses cannot restore old analytics data');
}

// Finding 13: a matrix click must select the clicked pair (a NUL separator in the attribute became U+FFFD).
async function analyticsMatrix() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const ts = Date.now() / 1000;
    const events = [{ event: 'join', agent: 'alpha', sid: 'a', ts: ts - 10 }, { event: 'join', agent: 'beta', sid: 'b', ts: ts - 9 },
      { event: 'message', from: 'alpha', to: [{ name: 'beta', kind: 'agent' }], reach: ['beta'], ts: ts - 6, id: 1, text: 'hello' }];
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
      if (['/auth.js', '/base.js', '/analytics-shared.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      if (u.hostname !== 'arke-review.test') return route.abort();
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: read('analytics.html') });
      if (['/graph-layout.js', '/analytics-shared.js', '/analytics-page.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      let data = [];
      if (u.pathname === '/events') data = { events: Number(u.searchParams.get('since')) ? [] : events, next: 100 };
      else if (u.pathname === '/horatio/history') data = { records: [] };
      else if (u.pathname === '/fileinfo') data = { available: false, reason: 'fixture' };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await page.goto('http://arke-review.test/');
    await page.waitForFunction(() => M?.msgs.length === 1);
    await page.locator('[data-pair]').first().click();
    assert.deepEqual(await page.evaluate(() => pair), ['alpha', 'beta']);
    console.log('ok: the who-talks-to-whom matrix selects the clicked pair');
  } finally { await browser.close(); }
}

async function analyticsSingleAgentLayout() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const events = [{ event: 'tool', agent: 'solo', ts: Date.now() / 1000 - 10, tool: 'Read', target: '/tmp/example.md' }];
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
      if (['/auth.js', '/base.js', '/analytics-shared.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      if (u.hostname !== 'arke-review.test') return route.abort();
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: read('analytics.html') });
      if (['/graph-layout.js', '/analytics-shared.js', '/analytics-page.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      let data = [];
      if (u.pathname === '/events') data = { events: Number(u.searchParams.get('since')) ? [] : events, next: 100 };
      else if (u.pathname === '/horatio/history') data = { records: [] };
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await page.goto('http://arke-review.test/');
    await page.waitForSelector('.gn[data-node="solo"]');
    for (const kind of ['fr', 'force', 'stress', 'circle', 'layers', 'stress']) {
      await page.selectOption('#glayout', kind);
      await page.waitForFunction(() => G.raf === 0);
      assert(await page.evaluate(() => G.nodes.length === 1 && G.nodes.every(a => Number.isFinite(a.px) && Number.isFinite(a.py))),
        kind + ': switching layouts must keep the agent visible');
      assert(!/NaN|Infinity/.test(await page.getAttribute('.gn[data-node="solo"]', 'transform')));
      const saved = await page.evaluate(() => JSON.parse(localStorage.getItem(GKEY)).solo);
      assert(saved.slice(0, 2).every(Number.isFinite), kind + ': saved positions must remain usable');
    }
    assert.deepEqual(errors, []);
    console.log('ok: a single agent stays visible and saves valid positions across all five layouts');
  } finally { await browser.close(); }
}


// Replies that arrive late or out of order must not overwrite the current view.
const raceHtml = read('console.html');
const agent = name => ({name,kind:'codex',state:'idle',host:'test',projects:{'/test':true},files:[],started_at:1,last_active:1,unread:0,reachable:'yes',cwd:'/tmp/test'});
const msg = (id,from,to,text='message '+id) => ({id,ts:1700000000+id,from,to:[{kind:to==='human'?'human':'agent',name:to}],reach:[to],text,host:'test',projects:['/test']});
const deferred = () => {let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}};
async function consoleRaces() {
 const { chromium } = playwright(); const b = await chromium.launch({ executablePath: chrome(chromium), headless: true });
 try {
  async function fixture(state,hash='') {
   const p=await b.newPage({viewport:{width:1440,height:1000}});
   await p.route('**/*',async r=>{
    const u=new URL(r.request().url());
    if(u.pathname==='/arke.css')return r.fulfill({contentType:'text/css',body:read('arke.css')});
    if(['/auth.js','/base.js','/analytics-shared.js'].includes(u.pathname))return r.fulfill({contentType:'text/javascript',body:read(u.pathname.slice(1))});
    if(u.hostname!=='review.test')return r.abort();
    if(u.pathname==='/')return r.fulfill({contentType:'text/html',body:raceHtml});
    let data;
    if(u.pathname==='/agents')data=(state.agentNames||['alpha','beta','helper']).map(agent);
    else if(u.pathname==='/projects')data=[{path:'/test',n:3}];
    else if(u.pathname==='/resolve'){
     data=state.resolve?await state.resolve(u):{has_project:false,unknown:[],unsubscribed:[],reach:u.searchParams.get('to').split(','),unreachable:[]};
     if(data.error)return r.fulfill({status:503,contentType:'application/json',body:JSON.stringify(data)});
    }
    else if(u.pathname==='/send') {state.sent=JSON.parse(r.request().postData());(state.sentBodies ||= []).push(state.sent);state.sendStarted?.resolve();await state.sendRelease?.promise;
     if (state.failSendOnce) { state.failSendOnce = false; return r.abort(); }
     if (state.refuseSend) return r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: 'oldn is an old name of several agents' }) });
     data={id:999,reach:state.sent.to,mode:'notify'};}
    else if(u.pathname==='/name') {
     const body=JSON.parse(r.request().postData());state.renameBody=body;state.renameStarted.resolve();await state.renameRelease.promise;
     if(state.failRename)return r.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:'name already taken'})});
     state.agentNames=(state.agentNames||['alpha','beta','helper']).map(n=>n===body.agent?body.name:n);data={name:body.name};
    }
    else if(u.pathname.endsWith('/transcript'))data=state.transcript?await state.transcript(u):{available:false,reason:'mock'};
    else if(u.pathname==='/log'){
     const name=u.searchParams.get('agent'),before=Number(u.searchParams.get('before')||Infinity),since=Number(u.searchParams.get('since')||0);
     data=(state.messages||[]).filter(m=>(!name||m.from===name||m.reach.includes(name))&&m.id<before&&m.id>since).slice(-Number(u.searchParams.get('limit')||200));
     if(name==='alpha'&&Number.isFinite(before)&&state.olderRelease){state.olderStarted.resolve();await state.olderRelease.promise;}
    }else data={};
    await r.fulfill({contentType:'application/json',body:JSON.stringify(data)});
   });
   await p.goto('http://review.test/'+hash);
   await p.waitForFunction(()=>agents.length===3);
   await p.evaluate(()=>clearTimeout(timer));
   return p;
  }
  for (const fail of [false, true]) for (const change of ['stay', 'navigate', 'cancel', 'reopen']) {
   const state = { renameStarted: deferred(), renameRelease: deferred(), failRename: fail };
   const p = await fixture(state, '#agent/alpha/messages'), errors = [];
   p.on('pageerror', e => errors.push(e.message));
   await p.click('#rename'); await p.fill('#rename-in', 'charlie'); await p.click('#rename-ok'); await state.renameStarted.promise;
   if (change === 'navigate') {
    await p.evaluate(() => { location.hash = '#agent/beta/messages'; }); await p.waitForFunction(() => view.name === 'beta');
   } else if (change === 'cancel' || change === 'reopen') {
    await p.click('#rename-no'); await p.waitForSelector('#rename-in', { state: 'detached' });
    if (change === 'reopen') { await p.click('#rename'); await p.fill('#rename-in', 'different'); }
   }
   await p.fill('#text', 'draft for ' + (change === 'navigate' ? 'beta' : 'alpha'));
   const response = p.waitForResponse(r => new URL(r.url()).pathname === '/name');
   state.renameRelease.resolve(); await (await response).finished(); await p.waitForTimeout(100);
   const expected = change === 'navigate' ? 'beta' : change === 'stay' && !fail ? 'charlie' : 'alpha';
   assert.equal(await p.evaluate(() => view.name), expected, change + ': delayed rename must respect the current view');
   assert.equal(await p.inputValue('#to'), expected);
   assert.equal(await p.inputValue('#text'), 'draft for ' + (change === 'navigate' ? 'beta' : 'alpha'));
   assert.deepEqual(state.renameBody, { agent: 'alpha', name: 'charlie' });
   if (change === 'stay' && fail) assert.equal(await p.locator('#rename-err').textContent(), 'name already taken');
   if (change === 'reopen') {
    assert.equal(await p.inputValue('#rename-in'), 'different');
    assert.equal(await p.locator('#rename-err').textContent(), '');
   }
   assert.deepEqual(errors, []); await p.close();
  }
  console.log('ok: rename replies update only their original active form, preserving navigation, recipients and drafts');
  {
   const p = await fixture({}, '#agent/alpha/transcript');
   for (const outputFirst of [false, true]) {
    await p.evaluate(outputFirst => {
     clearTimeout(timer); tReset(false);
     addEntries([{id:'300:0',ts:1700000003,kind:'tool',tool:'Bash',call:'newer',command:'echo newer',edits:[]}]);
     const older = {id:'100:0',ts:1700000001,kind:'tool',tool:'shell',command:'echo older',edits:[]};
     const output = {id:'200:0',ts:1700000002,kind:'result',for:'shell',text:'OLDER SHELL OUTPUT'};
     if (outputFirst) { addEntries([output]); addEntries([older]); } else addEntries([older, output]);
     addEntries([{id:'400:0',ts:1700000004,kind:'output',text:'standalone slash-command output'}]);
    }, outputFirst);
    assert.equal(await p.locator('#transcript li[data-id="100:0"] pre.out').textContent(), 'OLDER SHELL OUTPUT');
    assert.equal(await p.locator('#transcript li[data-id="300:0"] pre.out').count(), 0);
    assert.equal(await p.locator('#transcript li.output[data-id="400:0"]').count(), 1);
   }
   console.log('ok: historical shell results follow their own call, including when the result arrives first'); await p.close();
  }
  {
   const state={sendStarted:deferred(),sendRelease:deferred()};const p=await fixture(state,'#agent/alpha/messages');
   await p.fill('#text','first message');await p.evaluate(()=>document.getElementById('compose').requestSubmit());await state.sendStarted.promise;
   assert.equal(await p.locator('#compose button[type="submit"]').isDisabled(), true);
   await p.evaluate(() => { for (let i = 0; i < 3; i++) document.getElementById('compose').requestSubmit(); });
   await p.waitForTimeout(100);
   assert.equal(state.sentBodies.length, 1, 'overlapping submissions are ignored inside the handler');
   await p.fill('#text','second message draft');state.sendRelease.resolve();await p.waitForFunction(()=>document.getElementById('status').textContent.startsWith('Sent'));
   assert.equal(await p.locator('#compose button[type="submit"]').isDisabled(), false);
   const result=await p.inputValue('#text');assert.equal(state.sent.text,'first message');assert.equal(result,'second message draft');console.log('ok: a draft typed while the previous message was sending is kept');await p.close();
  }
  for (const change of ['retype', 'recipient', 'navigate']) {
   const state = { sendStarted: deferred(), sendRelease: deferred() }, p = await fixture(state, '#agent/alpha/messages');
   await p.fill('#text', 'continue'); await p.evaluate(() => document.getElementById('compose').requestSubmit()); await state.sendStarted.promise;
   if (change === 'retype') { await p.fill('#text', ''); await p.fill('#text', 'continue'); }
   if (change === 'recipient') await p.fill('#to', 'beta');
   if (change === 'navigate') { await p.evaluate(() => { location.hash = '#agent/beta/messages'; }); await p.waitForFunction(() => view.name === 'beta'); }
   state.sendRelease.resolve(); await p.waitForFunction(() => !sending);
   // navigating keeps the draft's To, so the sent draft is still the draft: it clears, and a second Send sends nothing
   const kept = change === 'navigate' ? '' : 'continue';
   assert.equal(await p.inputValue('#text'), kept, change + ': only a newer draft survives the send');
   assert.deepEqual(state.sent.to, ['alpha']);
   if (change === 'navigate') {
    await p.evaluate(() => document.getElementById('compose').requestSubmit()); await p.waitForTimeout(200);
    assert.equal(state.sentBodies.length, 1, 'navigate: the sent text is not sent twice');
    assert.equal(await p.inputValue('#to'), 'beta', 'navigate: the next message is for the page now open');
   }
   await p.close();
  }
  console.log('ok: retyping identical text and changing recipients preserve a newer draft; navigating mid-send never sends twice');
  for (const fail of [false, true]) for (const target of ['/test', '']) {
   const started = deferred(), release = deferred(); let hold = false;
   const state = { resolve: async u => {
    const to = u.searchParams.get('to');
    if (hold && to === 'alpha') { hold = false; started.resolve(); await release.promise; if (fail) return { error: 'old lookup failed' }; }
    return { has_project: to === '/test', unknown: [], unsubscribed: [], reach: to === '/test' ? ['alpha', 'beta'] : [to], unreachable: [] };
   } };
   const p = await fixture(state);
   hold = true; await p.fill('#to', 'alpha'); await started.promise;
   await p.fill('#to', target);
   if (target) {
    await p.waitForFunction(() => document.getElementById('reach').textContent.includes('2 agents'));
    await p.locator('#mode label').filter({ has: p.locator('#mode-board') }).click();
    await p.waitForFunction(() => document.getElementById('reach').textContent.includes('posts to'));
   } else await p.waitForFunction(() => document.getElementById('mode').hidden && document.getElementById('reach').textContent === '');
   const expected = await p.locator('#reach').textContent();
   const response = p.waitForResponse(r => new URL(r.url()).pathname === '/resolve' && new URL(r.url()).searchParams.get('to') === 'alpha');
   release.resolve(); await (await response).finished(); await p.waitForTimeout(100);
   assert.equal(await p.locator('#reach').textContent(), expected, 'stale ' + (fail ? 'failure' : 'success') + ' leaves current preview intact');
   assert.equal(await p.locator('#mode').isHidden(), !target);
   if (target) {
    await p.fill('#text', 'board message'); await p.evaluate(() => document.getElementById('compose').requestSubmit()); await p.waitForFunction(() => !sending);
    assert.deepEqual(state.sent.to, ['/test']); assert.equal(state.sent.mode, 'board');
   }
   await p.close();
  }
  console.log('ok: stale recipient lookup successes and failures cannot replace a current or empty preview');
  for (const target of ['old-name', 'old-name, alpha', 'old-name, /test']) {
   const state = { resolve: async u => {
    const to = u.searchParams.get('to').split(',');
    return { has_project: to.includes('/test'), unknown: [], unsubscribed: [], reach: to.length > 1 ? ['alpha'] : [],
     ambiguous: [{ name: 'old-name', delivered_to: '', also_old_name_of: ['beta', 'helper'] }] };
   } };
   const p = await fixture(state);
   await p.fill('#to', target);
   await p.waitForFunction(() => document.getElementById('reach').textContent.includes('old-name'));
   assert.equal(await p.locator('#reach').textContent(), 'old-name is an old name of several agents (beta, helper); use a current name');
   assert.equal(await p.locator('#reach').evaluate(el => el.classList.contains('err')), true);
   if (target.includes('/test')) {
    await p.locator('#mode label').filter({ has: p.locator('#mode-board') }).click();
    await p.waitForTimeout(300);
    assert.ok(!(await p.locator('#reach').textContent()).includes('posts to'));
   }
   state.resolve = async () => ({ has_project: false, unknown: [], unsubscribed: [], reach: ['alpha'],
    ambiguous: [{ name: 'alpha', delivered_to: 'alpha', also_old_name_of: ['beta'] }] });
   await p.fill('#to', 'alpha');
   await p.waitForFunction(() => document.getElementById('reach').textContent === '→ notifies alpha');
   assert.equal(await p.locator('#reach').evaluate(el => el.classList.contains('err')), false);
   await p.close();
  }
  console.log('ok: unresolved aliases flag the whole recipient preview; a current name remains deliverable');
  {
   const state = { failSendOnce: true }, p = await fixture(state, '#agent/alpha/messages');
   await p.fill('#text', 'send once');
   await p.evaluate(() => document.getElementById('compose').requestSubmit());
   await p.waitForFunction(() => document.getElementById('status').textContent.startsWith('Send not confirmed'));
   await p.evaluate(() => document.getElementById('compose').requestSubmit());
   await p.waitForFunction(() => document.getElementById('status').textContent.startsWith('Sent'));
   assert.equal(state.sentBodies[0].request_id, state.sentBodies[1].request_id);
   await p.fill('#text', 'send once');
   await p.evaluate(() => document.getElementById('compose').requestSubmit());
   await p.waitForFunction(() => document.getElementById('text').value === '');
   assert.notEqual(state.sentBodies[1].request_id, state.sentBodies[2].request_id);
   console.log('ok: an uncertain send reuses its request ID, while a new confirmed send gets a new ID'); await p.close();
  }
  {
   const state = { refuseSend: true }, p = await fixture(state, '#agent/alpha/messages');
   await p.fill('#text', 'refused');
   await p.evaluate(() => document.getElementById('compose').requestSubmit());
   await p.waitForFunction(() => document.getElementById('status').textContent.startsWith('Not sent: oldn'));
   assert.equal(await p.inputValue('#text'), 'refused');
   console.log('ok: a send the bus refuses says "Not sent" and keeps the draft'); await p.close();
  }
  {
   const onlyAlpha=Array.from({length:200},(_,i)=>msg(i+1,'helper','alpha','ALPHA ONLY '+(i+1)));
   const shared=Array.from({length:200},(_,i)=>({...msg(i+201,'helper','alpha'),reach:['alpha','beta']}));
   const state={messages:[...onlyAlpha,...shared],olderStarted:deferred(),olderRelease:deferred()};const p=await fixture(state,'#agent/alpha/messages');
   await p.waitForFunction(()=>lastId===400);await p.click('#feed-older');await state.olderStarted.promise;
   await p.evaluate(()=>location.hash='#agent/beta/messages');await p.waitForFunction(()=>view.name==='beta'&&lastId===400);
   state.olderRelease.resolve();await p.waitForTimeout(500);
   assert.equal(await p.evaluate(()=>view.name),'beta');assert.equal(await p.locator('#feed li[data-id="1"]').count(),0);console.log('ok: an older page that arrives after switching agents is dropped');await p.close();
  }
  {
   const firstHeld=deferred(),release=deferred();let requests=0;
   const stale={available:true,next:'c200',entries:[{id:'200:0',ts:1700000002,kind:'assistant',text:'ROLLED BACK RESPONSE'}]};
   const rollback={available:true,next:'c300',entries:[{id:'300:0',ts:1700000003,kind:'rollback',undo_from:200,undo_to:300,turns:1}]};
   const state={transcript:async u=>{
    if(!u.searchParams.get('cursor'))return {available:true,next:'c100',prev:null,entries:[{id:'100:0',ts:1700000001,kind:'prompt',text:'initial'}]};
    requests++;
    if(requests===1){firstHeld.resolve();await release.promise;return stale;}
    return rollback;
   }};
   const p=await fixture(state,'#agent/alpha/transcript');await p.waitForFunction(()=>tNext==='c100');
   await p.evaluate(()=>{window.oldPoll=poll(true)});await firstHeld.promise;
   await p.evaluate(async()=>{await poll(true);clearTimeout(timer)});
   release.resolve();await p.evaluate(()=>window.oldPoll);
   await p.evaluate(async()=>{await poll(true);clearTimeout(timer)});
   assert.equal(await p.locator('#transcript li[data-id="200:0"]').count(),0);assert.equal(await p.evaluate(()=>tNext),'c300');console.log('ok: a superseded transcript reply cannot bring back rolled-back rows');await p.close();
  }
  {
   // a "load earlier" page fetched before a rollback must not put the rows it removed back
   const started=deferred(),release=deferred();
   const state={transcript:async u=>{
    if(u.searchParams.has('before')){started.resolve();await release.promise;
     return {available:true,prev:null,entries:[{id:'200:0',ts:1700000001,kind:'assistant',text:'UNDONE OLDER PAGE'}]};}
    if(!u.searchParams.has('cursor'))return {available:true,next:'c1000',prev:'p900',entries:[{id:'900:0',ts:1700000002,kind:'assistant',text:'old tail'}]};
    if(u.searchParams.get('cursor')==='c1000')return {available:true,next:'c1100',entries:[{id:'1000:0',ts:1700000003,kind:'rollback',undo_from:200,undo_to:1000,turns:1}]};
    return {available:true,next:'c1100',entries:[]};
   }};
   const p=await fixture(state,'#agent/alpha/transcript');await p.waitForFunction(()=>tNext==='c1000');
   await p.evaluate(()=>{clearTimeout(timer);window.pendingEarlier=loadEarlier();});await started.promise;
   await p.evaluate(async()=>{await poll(true);clearTimeout(timer)});
   release.resolve();await p.evaluate(()=>window.pendingEarlier);
   await p.evaluate(async()=>{await poll(true);clearTimeout(timer)});
   assert.equal(await p.locator('#transcript li[data-id="200:0"]').count(),0);
   console.log('ok: an older page fetched before a rollback cannot bring back the rows it removed');await p.close();
  }
 }finally{await b.close()}
}


async function humanHistoryAndFileIdentity() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const p = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = []; p.on('pageerror', e => errors.push(e.message));
    let messages = [{ ...msg(1, 'alpha', 'human', 'OLDER QUESTION STILL WAITING'),
      from_owner: 'old-alpha-owner', reach_owners: { human: 'human' } },
      ...Array.from({ length: 649 }, (_, i) => msg(i + 2, 'human', 'beta'))];
    const held = deferred(), release = deferred(); let holdOnce = false;
    const requested = [];
    await p.addInitScript(() => { if (!localStorage.getItem('arke')) localStorage.setItem('agentbus', 'http://chosen-bus.test'); });
    await p.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
      if (['/auth.js', '/base.js', '/analytics-shared.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      if (!['arke-review.test', 'chosen-bus.test'].includes(u.hostname)) return route.abort();
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: read('console.html') });
      requested.push(u.hostname); let data = {};
      if (u.pathname === '/agents') data = [agent('alpha'), agent('beta')];
      else if (u.pathname === '/projects') data = [];
      else if (u.pathname.endsWith('/transcript')) data = { available: false, reason: 'mock transcript' };
      else if (u.pathname === '/log') {
        const who = u.searchParams.get('agent'), since = Number(u.searchParams.get('since') || 0), before = Number(u.searchParams.get('before') || Infinity);
        if (holdOnce && who === 'human' && since === 650 && before === 1051) {
          holdOnce = false; held.resolve(); await release.promise;
        }
        data = messages.filter(m => (!who || m.from === who || m.reach.includes(who)) && m.id > since && m.id < before)
          .slice(-Number(u.searchParams.get('limit') || 200));
      }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await p.goto('http://arke-review.test/?bus=http://chosen-bus.test');
    await p.waitForFunction(() => humanLastId === 650);
    await p.evaluate(() => clearTimeout(timer));
    assert.equal(await p.locator('#waiting').textContent(), '1 unanswered');
    await p.click('#waiting');
    assert.match(await p.locator('#inbox-open').textContent(), /OLDER QUESTION STILL WAITING/);
    assert(requested.every(host => host === 'arke-review.test'), 'query parameters and saved bus addresses cannot send credentials to another server');
    await p.click('#inbox-close');
    messages.push(...Array.from({ length: 600 }, (_, i) => msg(i + 651, 'human', 'beta')));
    holdOnce = true;
    await p.evaluate(() => { window.oldHumanPoll = poll(true); }); await held.promise;
    await p.evaluate(() => { location.hash = '#agent/beta/messages'; });
    await p.waitForFunction(() => view.name === 'beta' && humanLastId === 1250);
    release.resolve(); await p.evaluate(() => window.oldHumanPoll);
    await p.evaluate(() => clearTimeout(timer));
    assert.equal(await p.locator('#waiting').textContent(), '1 unanswered');
    assert.equal(await p.evaluate(() => humanHistory.size), 1250, 'old navigation reply cannot replace complete history');
    await p.reload(); await p.waitForFunction(() => humanLastId === 1250);
    assert.equal(await p.locator('#waiting').textContent(), '1 unanswered', 'full reload recovers old pending questions');
    await p.evaluate(() => { clearTimeout(timer); doneIds = new Set(Array.from({ length: 601 }, (_, i) => i + 1)); saveDone(); renderWaiting(mineLast); });
    await p.reload(); await p.waitForFunction(() => humanLastId === 1250);
    assert.equal(await p.locator('#waiting').textContent(), '0 unanswered', 'older done IDs survive beyond 500 dismissals');
    await p.evaluate(() => { clearTimeout(timer); doneIds.clear(); saveDone(); renderWaiting(mineLast); });
    messages.push(msg(1251, 'human', 'alpha'));
    await p.evaluate(async () => { await poll(true); clearTimeout(timer); });
    assert.equal(await p.locator('#waiting').textContent(), '0 unanswered', 'a later reply clears the older question');
    const names = await p.evaluate(() => {
      agents = [{ name: 'carol', history_id: 'successor', history_ids: ['old-owner', 'successor'], aka: ['alice', 'bravo', 'alice'] }]; buildAlias();
      const question = { id: 1, ts: 1, from: 'alice', from_owner: 'old-owner', to: [{ name: 'human', kind: 'human' }], reach: ['human'], reach_owners: { human: 'human' }, text: 'old owner' };
      const reply = { id: 2, ts: 2, from: 'human', to: [{ name: 'carol', kind: 'agent' }], reach: ['carol'], text: 'reply after rename' };
      renderWaiting([question, reply].map(canonMsg));
      const afterRename = document.getElementById('waiting').textContent;
      const renamed = canonMsg(question);
      const transcriptTo = canonEntry({ kind: 'bus', from: 'human', to: ['alice'], reach_owners: { alice: '' } }).to;
      agents.push({ name: 'dave', aka: ['alice'] }); buildAlias();
      const ambiguousFrom = canonMsg(question).from;
      agents.pop();
      // A current name wins over someone else's old alias, even when saved ownership metadata disagrees.
      agents.push({ name: 'alice', history_id: 'new-owner', history_ids: ['new-owner'] }); buildAlias();
      renderWaiting([question, reply].map(canonMsg));
      const afterUnrelated = document.getElementById('waiting').textContent;
      const reusedReply = { ...reply, to: [{ name: 'alice', kind: 'agent' }], reach: ['alice'], reach_owners: { alice: '' } };
      renderWaiting([question, reusedReply].map(canonMsg));
      const afterReused = document.getElementById('waiting').textContent;
      const board = { id: 3, ts: 3, from: 'human', to: [{ name: '/demo', kind: 'project' }], reach: ['alice', 'carol'], mode: 'board', text: 'board post' };
      const everyone = { id: 4, ts: 4, from: 'human', to: [{ name: '*', kind: 'broadcast' }], reach: ['alice', 'carol'], text: 'broadcast' };
      renderWaiting([question, board, everyone].map(canonMsg));
      const afterBoardAndBroadcast = document.getElementById('waiting').textContent;
      const currentQuestion = { id: 1, from: 'alice', to: [{ name: 'human', kind: 'human' }], reach: ['human'] };
      renderWaiting([currentQuestion, { ...reusedReply, reach_owners: { alice: 'old-owner' } }].map(canonMsg));
      return { afterBoardAndBroadcast, afterRename, renamedFrom: renamed.from, fromAs: renamed.fromAs, transcriptTo, ambiguousFrom, afterUnrelated, afterReused,
        currentQuestionOldReply: document.getElementById('waiting').textContent, reusedFrom: canonMsg(question).from,
        unknown: canonMsg({ ...question, from: 'retired-name', from_owner: '' }).from };
    });
    assert.deepEqual(names, { afterBoardAndBroadcast: '1 unanswered', afterRename: '0 unanswered', renamedFrom: 'carol', fromAs: 'alice', transcriptTo: ['carol'], ambiguousFrom: 'alice',
      afterUnrelated: '1 unanswered', afterReused: '0 unanswered', currentQuestionOldReply: '0 unanswered',
      reusedFrom: 'alice', unknown: 'retired-name' });
    assert.deepEqual(errors, []); await p.close();
    console.log('ok: human questions survive paging, navigation races, reloads, dismissals, mixed message formats, and renamed or reused names');

    const a = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const fileErrors = []; a.on('pageerror', e => fileErrors.push(e.message));
    const now = Date.now() / 1000, calls = [];
    const events = [['one', 'local', '/repo-a', 'My Report.md'], ['two', 'local', '/repo-b', 'My Report.md'],
      ['remote', 'elsewhere', '/repo-a', 'My Report.md'], ['same', 'local', '/repo-a', './sub/../My Report.md']]
      .map(([name, host, cwd, target], i) => ({ ts: now - 20 + i, event: 'tool', agent: name, host, cwd, target, tool: 'Edit' }));
    await a.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
      if (['/auth.js', '/base.js', '/analytics-shared.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      if (u.hostname !== 'arke-review.test') return route.abort();
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: read('analytics.html') });
      if (['/graph-layout.js', '/analytics-shared.js', '/analytics-page.js'].includes(u.pathname)) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      let data = [];
      if (u.pathname === '/events') data = { events: Number(u.searchParams.get('since')) ? [] : events, next: 100 };
      else if (u.pathname === '/agents') data = events.map(e => ({ ...agent(e.agent), host: e.host, local: e.host === 'local', cwd: e.cwd }));
      else if (u.pathname === '/horatio/history') data = { records: [] };
      else if (u.pathname === '/fileinfo') { calls.push(u.searchParams.get('path')); data = { available: false, reason: 'fixture' }; }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await a.goto('http://arke-review.test/');
    await a.waitForFunction(() => M?.touches.length === 4);
    assert.equal(await a.locator('#filelabs [data-file]').count(), 3, 'different repos and hosts have separate rows');
    assert.equal(await a.locator('#conflicts li').count(), 1, 'only normalized same-file edits overlap');
    const overlap = await a.locator('#conflicts').textContent();
    assert.match(overlap, /one/); assert.match(overlap, /same/); assert(!/two|remote/.test(overlap));
    const clickFile = key => a.evaluate(key => [...document.querySelectorAll('#filelabs [data-file]')].find(el => el.dataset.file === key).click(), key);
    await clickFile(JSON.stringify(['local', '/repo-b/My Report.md']));
    await a.waitForFunction(() => document.getElementById('pfinfo')?.textContent.includes('fixture'));
    assert.deepEqual(calls, ['/repo-b/My Report.md']);
    assert.match(await a.locator('#logsub').textContent(), /^1 entries/);
    await clickFile(JSON.stringify(['elsewhere', '/repo-a/My Report.md']));
    await a.waitForFunction(() => document.getElementById('pfinfo')?.textContent.includes('only for files on this server'));
    assert.deepEqual(calls, ['/repo-b/My Report.md'], 'remote row never reads the same path on the local machine');
    assert.match(await a.locator('#pbody').textContent(), /elsewhere:\/repo-a\/My Report.md/);
    assert.match(await a.locator('#logsub').textContent(), /^1 entries/);
    assert.deepEqual(fileErrors, []); await a.close();
    console.log('ok: file rows, overlap detection, activity filters, and details use host plus normalized path');
  } finally { await browser.close(); }
}

async function experimentalKeyAccess() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true,
    args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const key = 'A'.repeat(43);
  try {
    for (const [file, pathname] of [['console.html', '/'], ['analytics.html', '/analytics'], ['analytics3d.html', '/analytics3d']]) {
      const p = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
      const errors = [], requests = [], sends = [], held = new Map();
      let rejectSend = true;
      p.on('pageerror', e => errors.push(e.message));
      await p.addInitScript(() => localStorage.setItem('arke', 'http://elsewhere.test:7777'));
      await p.route('**/*', async route => {
        const u = new URL(route.request().url());
        if (u.hostname !== 'auth-review.test') return route.abort();
        if (u.pathname === pathname) return route.fulfill({ contentType: 'text/html', body: read(file) });
        if (u.pathname.startsWith('/vendor/')) return route.fulfill({ contentType: 'text/javascript', body: readAsset(u.pathname.slice('/vendor/'.length)) });
        if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
        if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
      if (u.pathname.endsWith('.js')) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
        requests.push(route.request());
        if (held.has(u.pathname)) {
          const h = held.get(u.pathname); h.started.resolve(); await h.release.promise;
          return route.fulfill({ status: h.status, contentType: 'application/json', body: '{}' });
        }
        if (u.pathname === '/send') sends.push(JSON.parse(route.request().postData()));
        if (route.request().headers().authorization !== 'Bearer ' + key || u.pathname === '/lock' || (u.pathname === '/send' && rejectSend)) {
          if (u.pathname === '/send') rejectSend = false;
          return route.fulfill({ status: 401, contentType: 'application/json', body: '{"error":"key required"}' });
        }
        const data = u.pathname === '/agents' ? [agent('alpha')] : u.pathname === '/events' ? { events: [], next: 0, generation: 'fixture' }
          : u.pathname === '/horatio/history' ? { records: [] } : u.pathname === '/machine' ? { host: 'test', cores: 4, mem_total_mb: 8192 }
          : u.pathname === '/send' ? { id: 1, reach: ['alpha'], mode: 'notify' }
          : u.pathname === '/resolve' ? { unknown: [], unsubscribed: [], reach: ['alpha'], unreachable: [] } : [];
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
      });
      const unlock = async value => {
        await p.locator('dialog input').fill(value);
        await p.locator('dialog button').click();
      };
      await p.goto('http://auth-review.test' + pathname + '?bus=http://elsewhere.test:7777');
      await p.locator('dialog[open]').waitFor();
      await p.waitForFunction(() => document.getElementById('conn').textContent);
      if (file !== 'console.html') assert.doesNotMatch(await p.textContent('#conn'), /no answer/, file + ': waiting for the key is not a dead server');
      else assert.equal(await p.textContent('#conn'), 'Waiting for the Arke key.', 'the locked console waits for the key, not for the network');
      const before = requests.length;
      await p.evaluate(async () => { for (let i = 0; i < 5; i++) await ArkeAuth.fetch('/agents').catch(() => {}); });
      assert.equal(requests.length, before, 'locked polling makes no new network requests');
      await unlock('B'.repeat(43));
      await p.waitForFunction(() => document.querySelector('dialog [role=alert]').textContent === 'Incorrect key.');
      assert.equal(await p.evaluate(() => sessionStorage.getItem('arke.key')), null);
      await unlock(key);
      await p.locator('dialog[open]').waitFor({ state: 'hidden' });
      await p.waitForFunction(() => sessionStorage.getItem('arke.key'));
      if (file !== 'console.html') await p.waitForFunction(() => !document.getElementById('conn').textContent, null, { timeout: 5000 });  // the key error is gone
      assert.equal(await p.evaluate(async () => (await ArkeAuth.fetch('/agents')).status), 200);
      assert(requests.every(r => new URL(r.url()).hostname === 'auth-review.test'));
      assert(await p.evaluate(async () => { try { await ArkeAuth.fetch('http://elsewhere.test/agents'); return false; } catch { return true; } }));
      await p.reload();
      assert.equal(await p.evaluate(async () => (await ArkeAuth.fetch('/agents')).status), 200);
      assert.equal(await p.locator('dialog[open]').count(), 0, 'tab storage survives reload and page navigation');
      if (file === 'console.html') {
        await p.waitForFunction(() => agents.length === 1);
        await p.evaluate(() => clearTimeout(timer));
        await p.fill('#to', 'alpha'); await p.fill('#text', 'preserve this draft');
        await p.click('#compose button[type=submit]');
        await p.locator('dialog[open]').waitFor();
        assert.equal(await p.locator('#text').inputValue(), 'preserve this draft');
        await unlock(key); await p.locator('dialog[open]').waitFor({ state: 'hidden' });
        assert.equal(sends.length, 1, 'unlock never replays a mutation');
        await p.click('#compose button[type=submit]');
        await p.waitForFunction(() => document.querySelector('#status').textContent.includes('Sent to'));
        assert.equal(sends.length, 2);
        assert.equal(sends[0].request_id, sends[1].request_id);
        for (const status of [401, 200]) {
          const h = { status, started: deferred(), release: deferred() }; held.set('/old', h);
          await p.evaluate(() => { window.oldReply = ArkeAuth.fetch('/old').then(() => 'accepted', e => e.message); });
          await h.started.promise;
          await p.evaluate(() => ArkeAuth.fetch('/lock').catch(() => {}));
          await unlock(key); await p.locator('dialog[open]').waitFor({ state: 'hidden' });
          h.release.resolve();
          assert.match(await p.evaluate(() => window.oldReply), /discard this response/);
          assert.equal(await p.locator('dialog[open]').count(), 0, 'an old 401 cannot clear a newer key');
          assert.equal(await p.evaluate(() => sessionStorage.getItem('arke.key')), key);
          held.delete('/old');
        }
        // Privacy modes without sessionStorage still work for the lifetime of this page.
        await p.addInitScript(() => Object.defineProperty(window, 'sessionStorage', { get() { throw Error('disabled'); } }));
        await p.reload(); await p.locator('dialog[open]').waitFor();
        await unlock(key); await p.locator('dialog[open]').waitFor({ state: 'hidden' });
        assert.equal(await p.evaluate(async () => (await ArkeAuth.fetch('/agents')).status), 200);
      }
      assert.deepEqual(errors, [], file);
      await p.close();
      console.log('ok: experimental key prompt, tab memory, and same-origin requests in ' + file);
    }
  } finally { await browser.close(); }
}

async function browserOriginIsolation() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  const otherSite = http.createServer((request, response) => {
    response.writeHead(200, { 'Content-Type': 'text/html' });
    response.end('<!doctype html><title>other website</title>');
  });
  await new Promise(resolve => otherSite.listen(0, '127.0.0.1', resolve));
  const key = 'A'.repeat(43);
  try {
    for (const requireKey of [false, true]) {
      const root = fs.mkdtempSync(path.join(os.tmpdir(), 'arke-browser-origin-'));
      fs.writeFileSync(path.join(root, 'key'), key, { mode: 0o600 });
      const config = path.join(root, 'config.toml');
      fs.writeFileSync(config, `[server]\nstate_dir=${JSON.stringify(root)}\nlog_file=${JSON.stringify(path.join(root, 'events.jsonl'))}\nexperimental_require_key=${requireKey}\nkey_file=${JSON.stringify(path.join(root, 'key'))}\n`);
      const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !/^(ARKE_|AGENTBUS_)/.test(name)));
      const script = `import settings,server
server.Handler.key=settings.server_key(server.SETTINGS)
server.Handler.bus=server.Bus(server.STATE_DIR/'state.sqlite3')
server.Handler.bus.hook({'event':'SessionStart','session_id':'a','names':['alice'],'host':'remote'})
server.Handler.bus.send('human',{'to':['alice'],'text':'private fixture mail'})
with server.BusHTTPServer(('127.0.0.1',0),server.Handler) as http:
 print('http://127.0.0.1:'+str(http.server_port),flush=True)
 http.serve_forever()
`;
      const child = spawn('python3', ['-u', '-c', script], { cwd: path.join(path.dirname(__dirname), 'arke'), env: { ...env, ARKE_CONFIG: config } });
      let output = '';
      child.stdout.on('data', chunk => { output += chunk; });
      child.stderr.on('data', chunk => { output += chunk; });
      try {
        const base = await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(Error('test bus startup timed out: ' + output)), 10000);
          child.stdout.on('data', () => { const match = output.match(/^http:\/\/127\.0\.0\.1:\d+/m); if (match) { clearTimeout(timer); resolve(match[0]); } });
          child.once('exit', () => { clearTimeout(timer); reject(Error('test bus exited: ' + output)); });
          child.once('error', e => { clearTimeout(timer); reject(e); });
        });
        const consolePage = await browser.newPage();
        await consolePage.route('https://**/*', route => route.abort());
        await consolePage.goto(base);
        if (requireKey) {
          await consolePage.locator('dialog input').fill(key);
          await consolePage.locator('dialog button').click();
          await consolePage.locator('dialog[open]').waitFor({ state: 'hidden' });
        }
        await consolePage.waitForFunction(() => agents.some(a => a.name === 'alice'));
        // Actual same-origin browser writes still work, including its generated Origin/Fetch Metadata headers.
        assert.equal(await consolePage.evaluate(async () => (await ArkeAuth.fetch('/send', {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Agent-As': 'human' },
          body: JSON.stringify({ to: ['alice'], text: 'same-origin browser send' }),
        })).status), 200);
        const port = otherSite.address().port;
        for (const host of ['127.0.0.1', 'localhost']) {
          const attacker = await browser.newPage();
          await attacker.goto(`http://${host}:${port}/`);
          const results = await attacker.evaluate(async ({ base, key }) => {
            const payload = JSON.stringify({ event: 'SessionStart', session_id: 'attack', names: ['unwanted'], host: 'remote' });
            const result = [];
            // Read, credentialed/preflighted write, and blind non-preflighted write.
            for (const [route, options] of [
              ['/log?attack=read', {}],
              ['/hook?attack=json', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + key }, body: payload }],
              ['/hook?attack=plain', { method: 'POST', mode: 'no-cors', referrerPolicy: 'no-referrer', headers: { 'Content-Type': 'text/plain' }, body: payload }],
            ]) {
              try { const r = await fetch(base + route, options); result.push(r.type === 'opaque' ? 'opaque' : await r.text()); }
              catch { result.push('blocked'); }
            }
            return result;
          }, { base, key });
          assert.deepEqual(results.slice(0, 2), ['blocked', 'blocked']);
          assert(['blocked', 'opaque'].includes(results[2]));
          const formPath = '/hook?attack=form-' + host;
          // CORP can hide the iframe response from Playwright; verify the rejection at the real server.
          const submitted = new Promise((resolve, reject) => {
            const check = () => { if (output.includes('POST ' + formPath + ' 403')) { clearTimeout(timer); child.stdout.off('data', check); resolve(); } };
            const timer = setTimeout(() => { child.stdout.off('data', check); reject(Error('form was not rejected: ' + output)); }, 5000);
            child.stdout.on('data', check);
          });
          await attacker.evaluate(({ base, formPath }) => {
            const frame = document.createElement('iframe'); frame.name = 'sink'; document.body.append(frame);
            const form = document.createElement('form'); form.method = 'POST'; form.enctype = 'text/plain';
            form.target = 'sink'; form.action = base + formPath;
            const input = document.createElement('input');
            input.name = '{"event":"SessionStart","session_id":"form-attack","names":["unwanted-form"],"host":"remote","padding":"';
            input.value = '"}'; // name=value\r\n becomes valid JSON, despite being a plain HTML form.
            form.append(input); document.body.append(form); form.submit();
          }, { base, formPath });
          await submitted;
          await attacker.close();
        }
        assert.match(output, /GET \/log\?attack=read 403/);
        assert.match(output, /OPTIONS \/hook\?attack=json 403/);
        assert.match(output, /POST \/hook\?attack=plain 403/);
        assert.match(output, /POST \/hook\?attack=form-.* 403/);
        const remaining = await consolePage.evaluate(async () => (await ArkeAuth.fetch('/agents')).json());
        assert.deepEqual(remaining.map(a => a.name), ['alice'], 'other websites cannot register agents');
        assert.equal(remaining[0].unread, 2, 'unread mail survives every rejected request');
        await consolePage.close();
        console.log('ok: real browser reads, preflights, blind POSTs and forms are blocked with key ' + (requireKey ? 'on' : 'off'));
      } finally {
        if (child.exitCode === null && child.signalCode === null) {
          const stopped = new Promise(resolve => child.once('exit', resolve)); child.kill(); await stopped;
        }
        fs.rmSync(root, { recursive: true, force: true });
      }
    }
  } finally { await browser.close(); await new Promise(resolve => otherSite.close(resolve)); }
}

// 3D page against a bus whose /filesizes fails: the city still draws; keys typed into fields never fly the camera;
// a rebuild with nothing new leaves the saved agent positions alone (a 2D tab re-renders on every change).
async function analytics3dResilience() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const p = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = []; p.on('pageerror', e => errors.push(e.message));
    const ts = Date.now() / 1000 - 600;
    const events = ['alpha', 'beta', 'gamma'].flatMap((a, i) => [
      { event: 'join', agent: a, ts: ts + i, projects: ['/repo'] },
      { event: 'tool', agent: a, host: 'local', cwd: '/repo', tool: 'Edit', target: `/repo/f${i}.py`, ts: ts + 10 + i }]);
    events.push({ event: 'message', from: 'alpha', to: [{ name: 'beta', kind: 'agent' }], reach: ['beta'], text: 'hi', ts: ts + 20 },
      { event: 'tool', agent: 'beta', host: 'local', cwd: '/repo', tool: 'Read', target: '/repo/f0.py', ts: ts + 30 });
    let sizeAsks = 0;  // the sizes keep failing, so every rebuild asks again
    await p.route('**/*', route => {
      const u = new URL(route.request().url());
      if (u.pathname === '/analytics3d') return route.fulfill({ contentType: 'text/html', body: read('analytics3d.html') });
      if (u.pathname.startsWith('/vendor/')) return route.fulfill({ contentType: 'text/javascript', body: readAsset(u.pathname.slice('/vendor/'.length)) });
      if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
      if (u.pathname.endsWith('.js')) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
      if (u.pathname === '/filesizes') { sizeAsks++; return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"fixture outage"}' }); }
      const data = u.pathname === '/events' ? { events: Number(u.searchParams.get('since')) ? [] : events, next: 100, generation: 'g' }
        : u.pathname === '/agents' ? ['alpha', 'beta', 'gamma'].map(n => ({ name: n, state: 'idle', host: 'local', local: true, cwd: '/repo', projects: { '/repo': true } }))
        : u.pathname === '/horatio/history' ? { records: [] } : u.pathname === '/machine' ? { host: 'local', cores: 4, mem_total_mb: 8192 }
        : u.pathname === '/git' ? { repo: '/repo', commits: [] } : [];
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await p.goto('http://arke.test/analytics3d');
    assert.equal(await p.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()), '#2c5fd4', 'the shared stylesheet loaded');
    await p.waitForFunction(() => window.ab3d?.files().length === 3, null, { timeout: 10000 });
    const saved = () => p.evaluate(() => localStorage.getItem('ab.an.graph.arke.test'));
    const before = await saved();
    for (let i = 0; i < 3; i++) {  // rebuilds without new data
      await p.evaluate(() => document.getElementById('proj').dispatchEvent(new Event('change')));
      await p.waitForTimeout(300);
    }
    assert.equal(await saved(), before, 'rebuilds with nothing new keep the saved positions byte for byte');
    assert.match(await p.textContent('#status'), /events/, 'a rebuild leaves the status line alone (it said "measuring files…" for good)');
    // the reads checkbox only shows / hides the read lines: no rebuild, and no "pull" slider that moved nobody
    assert.equal(await p.$('#mix'), null);
    const asks = sizeAsks, reads = () => p.evaluate(() => ab3d.state().lines.find(l => l[0] === 'reads')[2]);
    assert.equal(await reads(), false);
    await p.click('#showmenu > summary'); await p.click('#tread');
    assert.equal(await reads(), true, 'checking reads shows the read lines');
    await p.waitForTimeout(300);
    assert.equal(sizeAsks, asks, 'toggling reads does not rebuild the scene');
    await p.evaluate(() => document.getElementById('proj').dispatchEvent(new Event('change')));
    await p.waitForTimeout(300);
    assert(sizeAsks > asks, 'a rebuild does ask for the sizes again (so the check above can fail)');
    await p.click('#arrmenu > summary');
    const cam = await p.evaluate(() => JSON.stringify(ab3d.cam())), roots = await p.isChecked('#roots');
    await p.focus('#cores'); await p.keyboard.type('12');
    await p.focus('#acity'); await p.keyboard.press('r');
    await p.waitForTimeout(900);
    assert.equal(await p.evaluate(() => JSON.stringify(ab3d.cam())), cam, 'digits typed into a number field never fly the camera');
    assert.equal(await p.isChecked('#roots'), roots, 'R on a slider does not toggle roots');
    await p.evaluate(() => paneOpen('agent', 'alpha'));
    await p.waitForFunction(() => document.querySelector('#pmsgs h4 .sub'));
    assert.match(await p.textContent('#pbody'), /state\s*idle/, 'the shared pane sees the agents the 3D page loaded');
    // pressing an agent's base block claims the press: a name label under the pointer can't start a link drag instead
    await p.keyboard.press('Escape');
    const prevented = await p.evaluate(() => {
      const at = ab3d.screenOf('marker', 'alpha'), ev = new PointerEvent('pointerdown', { bubbles: true, cancelable: true, clientX: at.x, clientY: at.y, button: 0, pointerId: 1 });
      document.querySelector('canvas').dispatchEvent(ev);
      dispatchEvent(new PointerEvent('pointerup', { clientX: at.x, clientY: at.y, pointerId: 1 }));
      return ev.defaultPrevented;
    });
    assert(prevented, 'a press on an agent block prevents the browser default (a native link drag)');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
  console.log('ok: 3D city survives a failed size batch, field keys stay in fields, idle rebuilds keep saved positions and the status line');
  console.log('ok: 3D reads checkbox shows read lines without a rebuild');
}

// Serve the pages and their scripts from web/; `data(u)` answers the API: an object is JSON, a number a bare status.
async function mockPage(browser, data) {
  const p = await browser.newPage({ viewport: { width: 1440, height: 1000 } }), seen = [], logs = [];
  p.on('console', m => logs.push(m.type() + ' ' + m.text()));
  await p.route('**/*', route => {
    const u = new URL(route.request().url()), page = { '/': 'console.html', '/analytics': 'analytics.html', '/analytics3d': 'analytics3d.html' }[u.pathname];
    if (u.hostname !== 'arke-review.test') return route.abort();
    if (page) return route.fulfill({ contentType: 'text/html', body: read(page) });
    if (u.pathname === '/arke.css') return route.fulfill({ contentType: 'text/css', body: read('arke.css') });
    if (u.pathname.startsWith('/vendor/')) return route.fulfill({ contentType: 'text/javascript', body: readAsset(u.pathname.slice('/vendor/'.length)) });
    if (u.pathname.endsWith('.js')) return route.fulfill({ contentType: 'text/javascript', body: read(u.pathname.slice(1)) });
    seen.push(u.pathname);
    const d = data(u);
    if (d === 'abort') return route.abort();
    if (typeof d === 'number') return route.fulfill({ status: d, contentType: 'application/json', body: '{"error":"fixture"}' });
    return route.fulfill({ status: d.status || 200, contentType: 'application/json', body: JSON.stringify(d.body ?? d) });
  });
  return { p, seen, logs };
}

// Code blocks with a language load the bus's own highlight.js (/vendor/, from install-service.py) once; without it, plain monospace.
async function consoleHighlighting() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    for (const vendored of [true, false]) {
      const { p } = await mockPage(browser, () => []), errors = [], asked = [];
      p.on('pageerror', e => errors.push(e.message));
      p.on('request', r => { if (/highlight/.test(r.url())) asked.push(r.url()); });
      if (!vendored) await p.route('**/vendor/highlight.min.js', r => r.fulfill({ status: 404, contentType: 'application/json', body: '{"error":"3D dependencies missing"}' }));
      await p.goto('http://arke-review.test/');
      const show = text => p.evaluate(text => { const d = document.createElement('div'); d.className = 'md'; d.innerHTML = md(esc(text)); document.body.append(d); }, text);
      await show('```\nplain = 1\n```');
      await p.waitForTimeout(200);
      assert.deepEqual(asked, [], 'a block without a language loads no highlighter');
      await show('```py\ndef f():\n    return "x"\n```');
      if (vendored) await p.waitForSelector('.md code.language-py .hljs-keyword');
      else { await p.waitForFunction(() => hlState === 'failed'); assert.equal(await p.$('.md code span'), null, 'without the file, code stays plain'); }
      await show('```js\nconst x = 1;\n```');
      if (vendored) await p.waitForSelector('.md code.language-js .hljs-keyword');
      assert.deepEqual(asked, ['http://arke-review.test/vendor/highlight.min.js'], 'loaded from the bus, once');
      assert.deepEqual(errors, []);
      await p.close();
    }
  } finally { await browser.close(); }
  console.log('ok: code blocks are highlighted by the vendored highlight.js, and stay plain when it is missing');
}

// A link can set the theme, but no other preference (hidden agents, dismissed questions, analytics settings).
async function prefsFromLinks() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    for (const path of ['/', '/analytics', '/analytics3d']) {
      const { p } = await mockPage(browser, () => 503);
      await p.goto(`http://arke-review.test${path}?theme=dark&hidden=${encodeURIComponent('["alpha"]')}&done=%5B1%5D&an.range=60&md=plain`, { waitUntil: 'domcontentloaded' });
      const saved = await p.evaluate(() => ({ ...localStorage }));
      for (const k of ['hidden', 'done', 'an.range', 'md']) assert.equal(saved['ab.' + k], undefined, `${path}: ?${k}= is not remembered`);
      assert.equal(saved['ab.theme'], 'dark', path + ': ?theme= is');
      assert.equal(await p.evaluate(() => document.documentElement.dataset.theme), 'dark');
      await p.close();
    }
  } finally { await browser.close(); }
  console.log('ok: a link sets the theme and no other preference');
}

// The light says "data none" before any answer, and a page waiting for its key is not disconnected.
async function connectionLight() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const down = await mockPage(browser, () => 'abort');
    await down.p.goto('http://arke-review.test/');
    await down.p.waitForFunction(() => /reconnecting|disconnected/.test(document.getElementById('netstat').textContent));
    assert.match(await down.p.textContent('#netstat'), /data none$/, 'no answer yet: no age');
    await down.p.close();
    for (const path of ['/', '/analytics']) {
      const locked = await mockPage(browser, () => 401);
      await locked.p.goto('http://arke-review.test' + path);
      await locked.p.locator('dialog[open]').waitFor();
      await locked.p.waitForTimeout(1500);  // a few more polls and light updates while locked
      assert.equal(await locked.p.getAttribute('#netstat', 'class'), 'netstat wait', path + ': waiting for the key is not a network failure');
      await locked.p.close();
    }
  } finally { await browser.close(); }
  console.log('ok: the connection light says "data none" before any answer and stays neutral while locked');
}

// Lanes hidden by an empty window must not leave the next render measuring a hidden element (W = -300).
async function analyticsWidthAfterEmpty() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    let events = [];
    const { p } = await mockPage(browser, u => u.pathname === '/events' ? { events: Number(u.searchParams.get('since')) ? [] : events, next: events.length ? 100 : 0 }
      : u.pathname === '/horatio/history' ? { records: [] } : []);
    await p.goto('http://arke-review.test/analytics');
    await p.waitForFunction(() => M && document.getElementById('lanes').hidden);
    events = [{ event: 'tool', agent: 'solo', ts: Date.now() / 1000 - 10, tool: 'Read', target: '/tmp/example.md' }];
    await p.evaluate(() => load());
    await p.waitForFunction(() => !document.getElementById('lanes').hidden);
    assert(await p.evaluate(() => W > 0), 'lanes are measured while shown');
  } finally { await browser.close(); }
  console.log('ok: the timeline measures its width after an empty window hid it');
}

// A message the markdown renderer chokes on shows as plain text without losing the batch around it,
// and a missing agent's page doesn't keep the previous agent's activity.
async function consoleBadMarkdownAndMissingAgent() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const bomb = '> '.repeat(6000) + 'deep';
    const msgs = [1, 2, 3].map(id => ({ id, ts: 1700000000 + id, from: 'alpha', to: [{ kind: 'human', name: 'human' }], reach: ['human'],
      text: id === 2 ? bomb : 'message ' + id, host: 'local', projects: [] }));
    const agents = [{ name: 'alpha', state: 'idle', host: 'local', local: true, cwd: '/repo', projects: {}, files: [{ ts: 1700000000, tool: 'Edit', target: '/repo/alpha-only.py' }] }];
    const { p } = await mockPage(browser, u => u.pathname === '/agents' ? agents : u.pathname === '/projects' ? []
      : u.pathname === '/log' ? msgs.filter(m => m.id > Number(u.searchParams.get('since') || 0))
      : u.pathname.endsWith('/transcript') ? { available: false, reason: 'mock' } : []);
    const errors = []; p.on('pageerror', e => errors.push(e.message));
    await p.goto('http://arke-review.test/');
    await p.waitForFunction(() => document.querySelectorAll('#feed .msg').length === 3, null, { timeout: 10000 });
    assert.match(await p.textContent('#feed'), /deep/, 'the message that broke markdown shows as text');
    await p.click('#waiting');
    assert.equal(await p.locator('#inbox-open li.msg').count(), 3, 'the inbox lists all three questions');
    await p.goto('http://arke-review.test/#agent/alpha');
    await p.waitForFunction(() => /alpha-only/.test(document.getElementById('files').textContent));
    await p.goto('http://arke-review.test/#agent/nobody');
    await p.waitForFunction(() => /No agent named nobody/.test(document.getElementById('agent-head').textContent));
    assert.doesNotMatch(await p.textContent('#files'), /alpha-only/, "a missing agent's page drops the previous agent's activity");
    await p.goto('http://arke-review.test/#agent/alpha');
    await p.waitForFunction(() => /alpha-only/.test(document.getElementById('files').textContent), null, { timeout: 10000 });
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
  console.log("ok: a message markdown can't render shows as text; a missing agent's page drops the previous activity");
}

// Enter in To picks a suggestion and never sends; the pin keys don't retarget a draft in the message box.
async function composeKeys() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const agents = ['alpha', 'beta'].map(name => ({ name, state: 'idle', host: 'local', local: true, cwd: '/repo', projects: {} }));
    const { p, seen } = await mockPage(browser, u => u.pathname === '/agents' ? agents : u.pathname === '/resolve' ? { has_project: false, reach: ['beta'], unknown: [], unsubscribed: [], broadcast: false }
      : u.pathname.endsWith('/transcript') ? { available: false, reason: 'mock' } : u.pathname === '/send' ? { id: 1, reach: ['beta'] } : []);
    await p.addInitScript(() => localStorage.setItem('ab.pins', JSON.stringify(['alpha', 'beta'])));
    await p.goto('http://arke-review.test/#agent/alpha');
    await p.waitForFunction(() => document.getElementById('to').value === 'alpha');
    await p.fill('#text', 'for alpha');
    await p.focus('#text');
    await p.keyboard.press('PageDown');
    await p.waitForTimeout(300);
    assert.equal(await p.evaluate(() => location.hash), '#agent/alpha', 'a draft keeps the carousel on its agent');
    assert.equal(await p.inputValue('#to'), 'alpha', "the draft's To is unchanged");
    assert.equal(await p.inputValue('#text'), 'for alpha');
    await p.fill('#to', '');
    await p.type('#to', 'bet');
    await p.keyboard.press('Enter');
    await p.keyboard.press('Enter');
    await p.waitForTimeout(300);
    assert.equal(await p.inputValue('#to'), 'beta', 'the first Enter picks the suggestion');
    assert.equal(await p.inputValue('#text'), 'for alpha', 'Enter in To keeps the draft');
    assert(!seen.includes('/send'), 'Enter in To sends nothing');
  } finally { await browser.close(); }
  console.log('ok: Enter in To never sends; the pin keys leave a draft and its To alone');
}

// At phone width the project page's compose row wraps and the 3D arrange menu stays on screen.
async function phoneWidth() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const agents = [{ name: 'alpha', state: 'idle', host: 'local', local: true, cwd: '/repo', projects: { '/demo': true } }];
    const { p } = await mockPage(browser, u => u.pathname === '/agents' ? agents : u.pathname === '/projects' ? [{ path: '/demo', members: ['alpha'] }]
      : u.pathname === '/resolve' ? { has_project: true, reach: ['alpha'], unknown: [], unsubscribed: [], broadcast: false }
      : u.pathname === '/events' ? { events: [], next: 0, generation: 'g' } : u.pathname === '/horatio/history' ? 404
      : u.pathname === '/machine' ? { host: 'local', cores: 4, mem_total_mb: 8192 } : []);
    await p.setViewportSize({ width: 390, height: 800 });
    await p.goto('http://arke-review.test/#project/%2Fdemo');
    await p.waitForSelector('#mode', { state: 'visible' });  // project recipient: Notify / Board beside Any member / Within project
    assert(await p.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'the project page fits a phone');
    const thead = await p.evaluate(() => {
      const d = document.createElement('div'); d.innerHTML = md('| a | b |\n|---|---|\n| 1 | 2 |'); document.body.append(d);
      return getComputedStyle(d.querySelector('thead')).display;
    });
    assert.equal(thead, 'table-header-group', 'a markdown table keeps its header on a phone (the card layout is for the agents table)');
    await p.goto('http://arke-review.test/');
    await p.waitForSelector('#agents td.wide', { state: 'attached' });
    assert.deepEqual(await p.evaluate(() => [...new Set([...document.querySelectorAll('#agents td.wide')].map(td => getComputedStyle(td).display))]), ['none'],
      'the wide columns (Tokens, Session, Times, Dir, Where, Projects) are hidden in the phone cards');
    await p.goto('http://arke-review.test/analytics3d');
    await p.click('#arrmenu > summary');
    const box = await p.evaluate(() => document.querySelector('#arrmenu[open] > div').getBoundingClientRect().toJSON());
    assert(box.left >= 0 && box.right <= 390, `the arrange menu is on screen: ${box.left}..${box.right}`);
  } finally { await browser.close(); }
  console.log('ok: the project compose row, markdown tables and the 3D arrange menu fit a phone');
}

// A bus without horatio answers 404: the 2D page says so once, without warnings, retries or a red light.
async function horatioOff() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const { p, seen, logs } = await mockPage(browser, u => u.pathname === '/horatio/history' ? { status: 404, body: { error: 'horatio is not configured on this bus' } }
      : u.pathname === '/events' ? { events: [], next: 0 } : []);
    await p.goto('http://arke-review.test/analytics');
    await p.waitForFunction(() => /off on this bus/.test(document.getElementById('tsgrid').textContent));
    await p.evaluate(() => { TS.horAt = 0; render(); });  // as if the retry interval had passed
    await p.waitForTimeout(300);
    assert.equal(seen.filter(s => s === '/horatio/history').length, 1, 'a 404 is not asked again');
    assert.deepEqual(logs.filter(l => /^warning|machine history/.test(l)), [], 'no warnings');
    assert.equal(await p.getAttribute('#netstat', 'class'), 'netstat up');
  } finally { await browser.close(); }
  console.log('ok: analytics treats a 404 from /horatio/history as horatio being off');
}

// 3D: notes for missing process samples and a bus without horatio; a 404 is not asked again.
async function analytics3dNotes() {
  const html = read('analytics3d.html'), extract = (a, b) => html.slice(html.indexOf(a), html.indexOf(b, html.indexOf(a)));
  let now = 1000, horatio = 0;
  const ctx = vm.createContext({ X3: { procs: [], procsNext: 0, procsFrom: Infinity, procsGen: 0, procsNote: '' }, next: 0, eventGeneration: '', nowS: () => now, console,
    api: async url => {
      if (url.startsWith('/events')) return { events: [], next: 0 };
      horatio++; throw Object.assign(new Error('horatio is not configured on this bus'), { status: 404 });
    } });
  vm.runInContext(extract('async function offsetAt(', 'async function loadGit(') + extract('async function loadHoratio(', '// ---------- the two side planes'), ctx);
  await ctx.loadProcs(0);
  assert.equal(ctx.X3.procsNote, 'no process samples in this window');
  await ctx.loadHoratio(0, 3600);
  assert.match(ctx.X3.horNote, /off on this bus: horatio is not configured/);
  now += 301; await ctx.loadHoratio(0, 3600);
  assert.equal(horatio, 1, 'a 404 is not asked again');
  assert.match(ctx.X3.horNote, /off on this bus/);
  // GET /machine: one failure must not stick for the page's life; the next rebuild asks again, a success is kept
  let machine = 0;
  Object.assign(ctx, { $: () => ({}), console: { ...console, warn() {} },
    api: async () => { if (machine++ === 0) throw new Error('bus restarting'); return { cores: 12, mem_total_mb: 4096 }; } });
  vm.runInContext(extract('async function loadMachine(', 'async function loadHoratio('), ctx);
  await ctx.loadMachine();
  await ctx.loadMachine();
  assert.equal(ctx.X3.machine?.cores, 12, 'a failed /machine is retried by the next rebuild');
  await ctx.loadMachine();
  assert.equal(machine, 2, 'a loaded machine is not asked again');
  console.log('ok: 3D notes empty process windows and treats a 404 from /horatio/history as horatio being off');
  console.log('ok: 3D retries GET /machine after a failure');
}

// 3D: agents with a saved place stay put when a new agent arrives; only the new one is placed and saved.
function analytics3dStablePlaces() {
  const page = read('analytics-page.js'), html = read('analytics3d.html');
  // each top-level function by name, from whichever file defines it (savedPositions moved into analytics-page.js)
  const fn = name => [page, html].map(s => s.indexOf(`function ${name}(`)).map((a, i) => a < 0 ? '' : [page, html][i].slice(a, [page, html][i].indexOf('\n}\n', a) + 3)).join('');
  for (const layoutKind of ['fr', 'circle']) {
    const store = {}, ctx = vm.createContext({ console, GKEY: 'g', LW: 1000, LH: 708, layoutKind,
      localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; } },
      fnv: s => { let h = 2166136261; for (const ch of s) h = Math.imul(h ^ ch.codePointAt(0), 16777619); return h >>> 0; } });
    vm.runInContext(read('graph-layout.js') + ['savedPositions', 'writePositions', 'savePositions', 'layout3d'].map(fn).join('\n'), ctx);
    const gd = n => { const names = Array.from({ length: n }, (_, i) => 'ag' + i);
      return { names, edges: names.map((a, i) => ({ a, b: names[(i * 7 + 3) % n], w: 1 + i % 5, ab: 1, ba: 0 })).filter(e => e.a !== e.b) }; };
    ctx.layout3d(gd(20), true);
    const first = JSON.parse(store.g);
    let shown;
    for (let n = 21; n <= 25; n++) {
      shown = ctx.layout3d(gd(n), false); const saved = JSON.parse(store.g);
      for (let i = 0; i < 20; i++) assert.deepEqual(saved['ag' + i], first['ag' + i], `${layoutKind}: ag${i} keeps its saved place when ag${n - 1} arrives`);
      for (const a of shown.slice(0, 20)) assert.deepEqual([Math.round(a.px / 1000 * 1e4) / 1e4, Math.round(a.py / 708 * 1e4) / 1e4], first[a.name].slice(0, 2), `${layoutKind}: ${a.name} is shown where it was saved`);
      assert(saved['ag' + (n - 1)], `${layoutKind}: the new agent is saved`);
    }
    const gap = Math.min(...shown.flatMap((a, i) => shown.slice(i + 1).map(b => Math.hypot(a.px - b.px, a.py - b.py))));
    assert(gap > 20, `${layoutKind}: arriving agents never land on top of another (closest pair ${gap.toFixed(1)} px apart)`);
  }
  console.log('ok: 3D agents keep their places when new agents arrive (springs and circle)');
}

// A typed draft keeps its To when the view changes (a pin-strip click, Back): it is only ever sent where To said when it was typed.
async function draftKeepsTo() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const agents = ['alpha', 'beta', 'gamma'].map(name => ({ name, state: 'idle', host: 'local', local: true, cwd: '/repo', projects: {} }));
    const { p } = await mockPage(browser, u => u.pathname === '/agents' ? agents : u.pathname === '/resolve' ? { has_project: false, reach: ['gamma'], unknown: [], unsubscribed: [], broadcast: false }
      : u.pathname.endsWith('/transcript') ? { available: false, reason: 'mock' } : u.pathname === '/send' ? { id: 1, reach: ['gamma'] } : []);
    const sends = [];
    p.on('request', r => { if (new URL(r.url()).pathname === '/send') sends.push(JSON.parse(r.postData())); });
    await p.addInitScript(() => localStorage.setItem('ab.pins', JSON.stringify(['alpha', 'beta'])));
    await p.goto('http://arke-review.test/#agent/alpha');
    await p.evaluate(() => { location.hash = '#agent/gamma'; });
    await p.waitForFunction(() => document.getElementById('to').value === 'gamma');
    await p.fill('#text', 'for gamma');
    await p.click('#strip a[data-pin="beta"]');
    await p.waitForFunction(() => location.hash === '#agent/beta');
    assert.equal(await p.inputValue('#to'), 'gamma', 'a pin-strip click keeps the draft\'s To');
    await p.goBack(); await p.goBack();
    await p.waitForFunction(() => location.hash === '#agent/alpha');
    assert.equal(await p.inputValue('#to'), 'gamma', 'Back keeps the draft\'s To');
    assert.equal(await p.inputValue('#text'), 'for gamma', 'the draft survives navigation');
    await p.evaluate(() => { document.getElementById('toast').hidden = true; });
    await p.click('#rename'); await p.focus('#rename-in');  // another input: the pin key is ignored there, so no draft warning either
    await p.keyboard.press('PageDown');
    assert(await p.evaluate(() => document.getElementById('toast').hidden), 'no draft toast for a pin key pressed in the rename box');
    await p.click('#rename-no');
    await p.focus('#text');
    await p.keyboard.press('Control+Enter');
    await p.waitForFunction(() => document.getElementById('text').value === '');
    assert.deepEqual(sends.map(b => b.to), [['gamma']], 'the draft goes where it was typed for');
    await p.fill('#text', '');
    await p.evaluate(() => { location.hash = '#agent/beta'; });
    await p.waitForFunction(() => document.getElementById('to').value === 'beta');  // no draft: To follows the view
  } finally { await browser.close(); }
  console.log('ok: navigating keeps a draft\'s To');
}

// Board drafts remain quiet when the user visits another view, including browser Back.
async function draftKeepsDeliveryMode() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const agents = ['alpha', 'beta'].map(name => ({ name, state: 'idle', host: 'local', local: true, cwd: '/repo', projects: {} }));
    const { p } = await mockPage(browser, u => u.pathname === '/agents' ? agents : u.pathname === '/resolve' ? { has_project: true, reach: ['alpha', 'beta'], unknown: [], unsubscribed: [], broadcast: false }
      : u.pathname.endsWith('/transcript') ? { available: false, reason: 'mock' } : u.pathname === '/send' ? { id: 1, reach: ['alpha', 'beta'] } : []);
    const sends = [];
    p.on('request', r => { if (new URL(r.url()).pathname === '/send') sends.push(JSON.parse(r.postData())); });
    await p.goto('http://arke-review.test/#agent/alpha');
    await p.fill('#to', '/repo');
    await p.fill('#text', 'Quiet project update');
    await p.locator('#mode label').filter({ hasText: 'Board' }).click();
    await p.evaluate(() => { location.hash = '#agent/beta'; });
    await p.waitForFunction(() => view.name === 'beta');
    assert(await p.isChecked('#mode-board'), 'navigation preserves Board');
    await p.goBack();
    await p.waitForFunction(() => view.name === 'alpha');
    assert(await p.isChecked('#mode-board'), 'Back preserves Board');
    await p.focus('#text');
    await p.keyboard.press('Control+Enter');
    await p.waitForFunction(() => document.getElementById('text').value === '');
    assert.deepEqual(sends.map(b => ({ to: b.to, mode: b.mode })), [{ to: ['/repo'], mode: 'board' }]);
    await p.evaluate(() => { location.hash = '#agent/beta'; });
    await p.waitForFunction(() => view.name === 'beta');
    assert.equal(await p.inputValue('#to'), 'beta');
    assert(await p.isChecked('#mode-notify'), 'an empty draft uses the new view defaults');
  } finally { await browser.close(); }
  console.log('ok: a Board draft stays quiet through navigation and Back');
}

async function boardPreviewAndGitHost() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const now = Date.now() / 1000, agents = [
      { name: 'alice', host: 'local', local: true, state: 'idle', cwd: '/repo', projects: { '/demo': true }, files: [] },
      { name: 'remote', host: 'elsewhere', local: false, state: 'idle', cwd: '/repo', projects: { '/demo': true }, files: [] },
    ];
    let events = [
      { event: 'tool', ts: now - 20, agent: 'alice', host: 'local', cwd: '/repo', tool: 'Bash', target: 'git commit -m local' },
      { event: 'tool', ts: now - 11, agent: 'remote', host: 'elsewhere', cwd: '/repo', tool: 'Bash', target: 'git commit -m remote' },
    ];
    const { p } = await mockPage(browser, u => {
      if (u.pathname === '/agents') return agents;
      if (u.pathname === '/resolve') return { has_project: true, reach: ['alice', 'remote'], unknown: [], unsubscribed: [],
        to: u.searchParams.get('to').split(',').map(name => name.startsWith('/') ? { name, kind: 'project' } : { name: 'alice', kind: 'agent' }) };
      if (u.pathname === '/events') return { events: u.searchParams.get('type') === 'procs' || Number(u.searchParams.get('since')) ? [] : events, next: 100, generation: 'g' };
      if (u.pathname === '/git') return { repo: '/repo', commits: [{ ts: now - 10, hash: '123456789abc', subject: 'Local commit', author: 'Local Author', files: [] }] };
      if (u.pathname === '/machine') return { host: 'local', cores: 4, mem_total_mb: 8192 };
      if (u.pathname === '/horatio/history') return { records: [] };
      return [];
    });
    await p.goto('http://arke-review.test/');
    await p.fill('#to', '/demo');
    await p.waitForSelector('#mode', { state: 'visible' });
    await p.locator('#mode label').filter({ has: p.locator('#mode-board') }).click();
    await p.waitForFunction(() => document.getElementById('reach').textContent.includes('nobody is interrupted'));
    for (const name of ['alice', 'old-alice']) {
      const resolved = p.waitForResponse(r => new URL(r.url()).pathname === '/resolve' && new URL(r.url()).searchParams.get('to') === name + ',/demo');
      await p.fill('#to', name + ',/demo');
      await resolved;
      await p.waitForFunction(() => document.getElementById('reach').textContent.includes('also notifies alice'));
      assert(!(await p.textContent('#reach')).includes('nobody is interrupted'));
    }
    // Inspect the rendered commit tooltip; attribution must use the event's original host.
    await p.route('**/analytics3d', r => r.fulfill({ contentType: 'text/html', body: read('analytics3d.html').replace('window.ab3d = {',
      'window.reviewGitInfo = () => world.children.filter(o => o.userData.group === "panel" && o.userData.info).map(o => o.userData.info(0));\nwindow.ab3d = {') }));
    for (const withLocal of [true, false]) {
      if (!withLocal) events = events.filter(e => e.host !== 'local');
      await p.goto('http://arke-review.test/analytics3d');
      await p.waitForFunction(() => window.reviewGitInfo?.().some(s => s.includes('Local commit')));
      const tooltip = (await p.evaluate(() => reviewGitInfo())).find(s => s.includes('Local commit'));
      assert(tooltip.includes(withLocal ? 'by alice (its git commit call)' : 'author Local Author (no agent'), tooltip);
      assert(!tooltip.includes('by remote'), tooltip);
    }
  } finally { await browser.close(); }
  console.log('ok: Board previews disclose direct notifications and local commits never match remote commands');
}

// The stacked tab on a wide screen has no fitted height: the transcript is capped, so filling it fetches a few pages, not the whole history.
async function stackedTranscriptBounded() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const agents = [{ name: 'alpha', state: 'idle', host: 'local', local: true, cwd: '/repo', projects: {} }];
    const page = (lo, hi) => Array.from({ length: hi - lo }, (_, i) => ({ id: `${lo + i}:0`, ts: 1700000000 + lo + i, kind: 'assistant', text: 'line ' + (lo + i) }));
    const { p, seen } = await mockPage(browser, u => {
      if (u.pathname === '/agents') return agents;
      if (!u.pathname.endsWith('/transcript')) return [];
      const before = u.searchParams.get('before');
      if (before) { const n = Number(before), lo = Math.max(0, n - 40); return { available: true, prev: lo ? String(lo) : null, entries: page(lo, n) }; }
      if (u.searchParams.has('cursor')) return { available: true, next: 'c', entries: [] };
      return { available: true, next: 'c', prev: '1988', entries: page(1988, 2000) };
    });
    await p.setViewportSize({ width: 1400, height: 1000 });
    await p.goto('http://arke-review.test/#agent/alpha/stacked');
    await p.waitForSelector('#transcript li.t');
    await p.waitForTimeout(2000);
    const n = seen.filter(x => x.endsWith('/transcript')).length, h = await p.evaluate(() => [document.getElementById('transcript').offsetHeight, innerHeight]);
    assert(n <= 6, `a few transcript requests fill the stacked tab, not the whole history: ${n}`);
    assert(h[0] <= 0.7 * h[1] + 1, `the stacked transcript is capped: ${h[0]}px of ${h[1]}`);
  } finally { await browser.close(); }
  console.log('ok: the stacked transcript is capped and loads only what fills it');
}

// A session restart leaves a status-less wake record ("NaN ago"); analytics live reloads wait out a drag or brush
// (a lost pointerup must not stop them), and a filter's render survives a pointermove in the same frame.
async function pagesKeepState() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    const agents = [{ name: 'alpha', state: 'idle', host: 'local', local: true, cwd: '/repo', projects: {}, started_at: Date.now() / 1000 - 60, wake: { typed: 'x' } }];
    let m = await mockPage(browser, u => u.pathname === '/agents' ? agents : u.pathname.endsWith('/transcript') ? { available: false, reason: 'mock' } : []);
    await m.p.goto('http://arke-review.test/#agent/alpha');
    await m.p.waitForSelector('#agent-panel:not([hidden])');
    const panel = await m.p.textContent('#agent-panel');
    assert(!panel.includes('Wake-up') && !panel.includes('NaN'), 'no "Wake-up · NaN ago" for a wake record without a status: ' + panel);
    await m.p.close();
    const ts = Math.floor(Date.now() / 1000);
    m = await mockPage(browser, u => u.pathname === '/events'
      ? { events: [{ event: 'join', agent: 'alpha', sid: 'a', ts }, { event: 'tool', agent: 'alpha', sid: 'a', ts, target: 'a.py' }], next: 1, generation: 'g' }
      : u.pathname === '/agents' ? agents : u.pathname === '/horatio/history' ? 404 : u.pathname === '/machine' ? { host: 'h', cores: 4, mem_total_mb: 8192 } : []);
    await m.p.clock.install();
    await m.p.goto('http://arke-review.test/analytics');
    await m.p.waitForFunction(() => typeof G === 'object' && M);
    const loads = () => m.seen.filter(x => x === '/events').length;
    for (const hold of ['G.drag = { node: {} }', 'TS.br = { svg: null, x: 0 }', 'drag = { svg: null, x: 0 }']) {
      await m.p.evaluate(hold);
      let before = loads();
      await m.p.clock.runFor(16000); await m.p.waitForTimeout(50);
      assert.equal(loads(), before, `no live reload while ${hold}`);
      await m.p.evaluate(() => dispatchEvent(new PointerEvent('pointercancel')));
      before = loads();
      await m.p.clock.runFor(16000); await m.p.waitForTimeout(50);
      assert(loads() > before, `a cancelled pointer resumes live reloads after ${hold}`);
    }
    const rendered = await m.p.evaluate(() => new Promise(done => {
      let n = 0; const real = render; window.render = () => { n++; real(); };
      const lq = document.getElementById('lq'); lq.value = 'zzz'; lq.dispatchEvent(new Event('input'));
      document.getElementById('lanesvg').dispatchEvent(new PointerEvent('pointermove', { bubbles: true, clientX: 400, clientY: 50 }));
      requestAnimationFrame(() => requestAnimationFrame(() => done(n)));
    }));
    assert(rendered >= 1, 'the filter renders even with a pointermove in the same frame');
  } finally { await browser.close(); }
  console.log('ok: a status-less wake shows no time; analytics reloads wait out a drag or brush; filters always render');
}

// A request can start before a drag. Its response must preserve the captured chart until release/cancellation.
async function analyticsRefreshDuringBrush() {
  const { chromium } = playwright();
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true });
  try {
    for (const mode of ['events', 'horatio', 'lostcapture']) {
      const ts = Date.now() / 1000;
      const { p } = await mockPage(browser, u => u.pathname === '/events'
        ? { events: [{ event: 'join', agent: 'alpha', sid: 'a', ts: ts - 3600 }, { event: 'tool', agent: 'alpha', sid: 'a', ts: ts - 30 }], next: 1 }
        : u.pathname === '/agents' ? [{ name: 'alpha', state: 'idle', projects: {} }]
        : u.pathname === '/horatio/history' ? 404 : []);
      await p.clock.install();
      await p.goto('http://arke-review.test/analytics');
      await p.waitForFunction(() => M && !loading && !TS.horBusy);
      await p.locator('#tsgrid svg.tsc').first().scrollIntoViewIfNeeded();
      const box = await p.locator('#tsgrid svg.tsc').first().boundingBox();
      let release, started, requests = 0;
      const gate = new Promise(resolve => { release = resolve; }), request = new Promise(resolve => { started = resolve; });
      await p.route(mode === 'horatio' ? '**/horatio/history?*' : '**/events?*', async route => {
        requests++; started(); await gate;
        await route.fulfill({ contentType: 'application/json', body: JSON.stringify(mode === 'horatio' ? { records: [] } : { events: [], next: 1 }) });
      });
      await p.evaluate(mode => {
        if (mode === 'horatio') { TS.horOff = false; TS.horAt = 0; tsHoratio(winNow()[0]); }
        else void load();
      }, mode);
      await request;
      await p.mouse.move(box.x + 80, box.y + 60); await p.mouse.down();
      await p.mouse.move(box.x + 130, box.y + 60);
      assert(await p.evaluate(() => TS.br?.moved), 'real pointer input starts a brush');
      release();
      await p.waitForFunction(() => !loading && !TS.horBusy && renderPending);
      assert(await p.evaluate(() => TS.br.svg.isConnected), `${mode}: response preserves the captured chart`);
      if (mode === 'lostcapture') {
        await p.evaluate(() => TS.br.svg.releasePointerCapture(1));
        await p.mouse.move(box.x + 140, box.y + 60);
        await p.waitForFunction(() => !TS.br);
      }
      await p.mouse.move(5, 5); await p.mouse.up();
      await p.waitForFunction(() => !TS.br && !renderPending);
      if (mode !== 'lostcapture') assert(await p.evaluate(() => !!win), 'release outside still applies the brush');
      if (mode !== 'horatio') {
        const before = requests;
        await p.clock.runFor(16000);
        await p.waitForFunction(() => !loading);
        assert(requests > before, 'live refresh continues after the drop');
      }
      await p.close();
    }
  } finally { await browser.close(); }
  console.log('ok: event/history responses preserve a real brush, release outside zooms, and lost capture resumes rendering');
}

// The static demo (demo/build.py + demo/fake-api.js), served from a subpath as GitHub Pages does: every page draws from
// the fake bus with no errors, and no request goes unanswered (a new endpoint needs a route in the fake).
async function demoSite() {
  const { chromium } = playwright(), root = path.dirname(__dirname), out = fs.mkdtempSync(path.join(os.tmpdir(), 'arke-demo-'));
  const built = spawnSync('python3', [path.join(root, 'demo', 'build.py'), out], { encoding: 'utf8' });
  assert.equal(built.status, 0, built.stderr);
  const types = { html: 'text/html', js: 'text/javascript', css: 'text/css', md: 'text/markdown' };
  const browser = await chromium.launch({ executablePath: chrome(chromium), headless: true, args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  try {
    const open = async file => {
      const p = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
      p.problems = []; p.on('pageerror', e => p.problems.push(e.message));
      await p.route('**/*', route => {
        const u = new URL(route.request().url()), f = path.join(out, u.pathname.replace(/^\/arke\//, '') || 'index.html');
        if (u.hostname !== 'arke-demo.test' || !u.pathname.startsWith('/arke/') || !fs.existsSync(f)) { p.problems.push('not served: ' + u.href); return route.fulfill({ status: 404, contentType: 'text/html', body: 'not found' }); }
        return route.fulfill({ contentType: types[f.split('.').pop()], body: fs.readFileSync(f) });
      });
      await p.goto('http://arke-demo.test/arke/' + file);
      return p;
    };
    const clean = async (p, what) => { assert.deepEqual(await p.evaluate(() => ArkeDemo.misses), [], what + ': every request has a fake'); assert.deepEqual(p.problems, [], what); };

    let p = await open('');
    await p.waitForFunction(() => document.querySelectorAll('#agents tr').length >= 6 && document.querySelectorAll('#feed .msg').length > 10);
    assert.match(await p.textContent('#agent-scope'), /1 offline/, 'the agent that ended is counted as offline');
    assert.equal(await p.getAttribute('#analytics', 'href'), 'analytics.html');
    assert.equal(await p.getAttribute('#analytics3d', 'href'), 'analytics3d.html', 'the 3D page is linked from the console');
    // a header sorts, reverses, then returns to the default order; a hidden column says so and can be undone
    const sortOf = () => p.getAttribute('th[data-col="tokens"]', 'aria-sort');
    for (const want of ['descending', 'ascending', 'none']) { await p.click('th[data-col="tokens"] .sortb'); assert.equal(await sortOf(), want); }
    assert.equal(await p.evaluate(() => localStorage.getItem('ab.sort')), 'null');
    await p.click('th[data-col="tokens"]', { button: 'right' }); await p.click('#colmenu [data-act="desc"]');
    assert.equal(await sortOf(), 'descending', 'the header menu sorts');
    await p.click('th[data-col="tokens"]', { button: 'right' }); await p.click('#colmenu [data-act="hide"]');
    assert.equal(await p.$('th[data-col="tokens"]'), null);
    assert.match(await p.textContent('#cols-pick'), /1 hidden/);
    assert.equal(await p.evaluate(() => localStorage.getItem('ab.sort')), 'null', 'no sort is left on a column that is not shown');
    await p.click('#toast button');
    assert(await p.$('th[data-col="tokens"]'), 'undo brings the column back');
    assert.equal(await p.textContent('#cols-pick'), 'choose');
    await p.fill('#to', 'trainer'); await p.fill('#text', 'a message typed into the demo');
    await p.click('#compose button[type="submit"]');
    await p.waitForFunction(() => document.querySelector('#feed .msg')?.textContent.includes('a message typed into the demo'));
    await p.evaluate(() => { location.hash = '#agent/reviewer'; });
    await p.waitForFunction(() => document.querySelectorAll('#transcript li.t').length > 5 && document.querySelectorAll('#files li').length > 3);
    await p.evaluate(() => { location.hash = '#help'; });
    await p.waitForFunction(() => document.getElementById('help-text').textContent.includes('message bus'));
    await clean(p, 'console');

    p = await open('analytics.html');
    await p.waitForFunction(() => M && M.lanes.size >= 8 && M.msgs.length > 50 && M.procs.length > 100 && /events/.test(document.getElementById('status').textContent));
    await p.waitForFunction(() => /CPU avg/.test(document.body.textContent), null, { timeout: 10000 });  // the machine chart drew: horatio's history is faked too
    await clean(p, '2D analytics');

    p = await open('analytics3d.html');
    await p.waitForFunction(() => window.ab3d?.files().length > 20 && /events/.test(document.getElementById('status').textContent), null, { timeout: 20000 });
    assert.equal(await p.textContent('#conn'), '', 'the 3D page reports no errors');
    // the toolbar: the guide closes and comes back, a plane can be switched off, facing it switches it back on, Esc closes a menu
    const planes = () => p.evaluate(() => ab3d.state().lines.find(l => l[0] === 'git')?.[2]);
    await p.click('#helpx'); assert.equal(await p.isVisible('#help'), false);
    await p.keyboard.press('?'); assert.equal(await p.isVisible('#help'), true, '? brings the guide back');
    assert.equal(await planes(), true);
    await p.click('#showmenu > summary'); await p.click('#teast');
    assert.equal(await planes(), false, 'the git plane and its lines can be switched off');
    await p.keyboard.press('Escape');
    assert.equal(await p.evaluate(() => document.getElementById('showmenu').open), false, 'Esc closes the menu');
    await p.click('[data-preset="east"]');
    assert.equal(await p.isChecked('#teast'), true, 'the git view shows the git plane again');
    assert.equal(await p.isVisible('#fpspeedl'), false, 'the walking speed shows only while walking');
    await clean(p, '3D analytics');
  } finally { await browser.close(); fs.rmSync(out, { recursive: true, force: true }); }
  console.log('ok: the static demo draws the console, both analytics pages and the 3D city from the fake bus, from a subpath');
}

(async () => { await markdownIsInert(); await browserOriginIsolation(); await experimentalKeyAccess(); analyticsIdentity(); analyticsFileTargets(); await analyticsFiles(); await analyticsRoofRefresh(); await analyticsSizeRefresh(); await analyticsSizeBatching(); await analyticsHistoryRaces(); analyticsStressLayout(); await analyticsLoad(); await analyticsRecovery(); await analyticsMatrix(); await analyticsSingleAgentLayout(); await consoleUI(); await consoleHighlighting(); await consoleRaces(); await humanHistoryAndFileIdentity(); await analytics3dResilience(); analytics3dStablePlaces(); await analytics3dNotes(); await prefsFromLinks(); await connectionLight(); await analyticsWidthAfterEmpty(); await horatioOff(); await phoneWidth(); await composeKeys(); await consoleBadMarkdownAndMissingAgent(); await draftKeepsTo(); await draftKeepsDeliveryMode(); await boardPreviewAndGitHost(); await stackedTranscriptBounded(); await pagesKeepState(); await analyticsRefreshDuringBrush(); await demoSite(); })().catch(e => { console.error(e); process.exitCode = 1; });
