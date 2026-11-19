// The demo's bus: replaces fetch() and answers every arke API request with invented but plausible data, computed in
// the browser. The pages in web/ run unchanged (demo/build.py only makes their paths relative and loads this first).
//
// One simulated timeline drives every answer, so the views agree with each other: a handful of agents work in turns
// (prompt, tool calls, messages, commits), and the event log, the agent table, the message log, the transcripts, git
// history, process samples and the machine history are all read off that one run. The run is seeded from a 12-hour
// anchor, so the console and the analytics pages (separate page loads) see the same history, and it keeps advancing
// with the clock, so live reloads bring new activity. Sends, renames and colors are kept in memory until reload.
(() => {
"use strict";

// ---------- seeded randomness ----------
const fnv = s => { let h = 2166136261; for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619); return h >>> 0; };
const mulberry = seed => () => { seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; };
const h01 = (key, n = 0) => mulberry(fnv(key) + Math.imul(n, 2654435761))();  // a fixed number in [0, 1) per (key, n)
const pick = (r, list) => list[Math.floor(r() * list.length)];
const int = (r, lo, hi) => lo + Math.floor(r() * (hi - lo + 1));
const fill = (s, o) => s.replace(/\{(\w+)\}/g, (_, k) => o[k] ?? "");

// ---------- the invented world ----------
const HOST = "devbox", CORES = 16, MEM_MB = 65536, REC = 220;  // REC: pretend bytes per log line (cursors are byte offsets)
const ATLAS = "/home/dev/atlas", WEB = ATLAS + "/web", LUMEN = "/home/dev/lumen";
const TREE = {
  [ATLAS]: `src/atlas/app.py src/atlas/config.py src/atlas/db.py src/atlas/models.py src/atlas/schemas.py
    src/atlas/auth/tokens.py src/atlas/auth/sessions.py src/atlas/auth/middleware.py src/atlas/auth/passwords.py
    src/atlas/api/users.py src/atlas/api/projects.py src/atlas/api/billing.py src/atlas/api/webhooks.py src/atlas/api/search.py
    src/atlas/services/email.py src/atlas/services/queue.py src/atlas/services/storage.py src/atlas/services/ratelimit.py
    migrations/0041_session_table.py migrations/0042_user_mfa.py migrations/0043_billing_invoices.py
    tests/conftest.py tests/test_auth.py tests/test_sessions.py tests/test_users.py tests/test_billing.py
    tests/test_webhooks.py tests/test_search.py tests/test_ratelimit.py scripts/seed_db.py scripts/dev.sh
    docs/api.md docs/auth.md docs/deploy.md pyproject.toml README.md`,
  [WEB]: `src/App.tsx src/main.tsx src/routes.tsx src/components/LoginForm.tsx src/components/SignupForm.tsx
    src/components/MfaPrompt.tsx src/components/UserTable.tsx src/components/ProjectCard.tsx src/components/BillingPanel.tsx
    src/components/InvoiceList.tsx src/components/SearchBox.tsx src/components/Toast.tsx src/components/Modal.tsx
    src/components/Nav.tsx src/lib/api.ts src/lib/auth.ts src/lib/format.ts src/lib/validators.ts src/lib/hooks.ts
    src/styles/tokens.css src/styles/forms.css src/styles/layout.css tests/LoginForm.test.tsx tests/validators.test.ts
    tests/api.test.ts package.json tsconfig.json vite.config.ts index.html README.md`,
  [LUMEN]: `lumen/data/loader.py lumen/data/tokenize.py lumen/data/shards.py lumen/data/augment.py lumen/data/dedupe.py
    lumen/model/transformer.py lumen/model/attention.py lumen/model/embeddings.py lumen/model/heads.py
    lumen/train/loop.py lumen/train/optim.py lumen/train/schedule.py lumen/train/checkpoint.py lumen/train/distributed.py
    lumen/eval/metrics.py lumen/eval/benchmarks.py lumen/eval/report.py configs/base.yaml configs/small.yaml
    configs/large.yaml configs/sweep.yaml scripts/launch.sh scripts/prep_shards.py scripts/export_onnx.py
    tests/test_loader.py tests/test_tokenize.py tests/test_schedule.py tests/test_metrics.py
    notebooks/loss_curves.ipynb docs/data.md docs/training.md pyproject.toml README.md`,
};
for (const k of Object.keys(TREE)) TREE[k] = TREE[k].split(/\s+/).filter(Boolean);
const ROOTS = [WEB, ATLAS, LUMEN];  // longest first: a file's project is the first root it is under
const under = (p, q) => p === q || p.startsWith(q + "/");
const repoOf = p => under(p, LUMEN) ? LUMEN : under(p, ATLAS) ? ATLAS : null;
const KNOWN = new Set(Object.entries(TREE).flatMap(([root, fs]) => fs.map(f => root + "/" + f)));
const SYMS = { [ATLAS]: ["verify_token", "rotate_session", "load_user", "apply_rate_limit", "sign_webhook", "invoice_total"],
  [WEB]: ["useSession", "apiFetch", "validateEmail", "formatCents", "LoginForm", "InvoiceList"],
  [LUMEN]: ["build_loader", "cosine_schedule", "save_checkpoint", "tokenize_shard", "eval_suite", "attention_mask"] };
const SUBJECTS = { [ATLAS]: ["Rotate refresh tokens on every use", "Add the session table migration", "Cover webhook signature failures",
    "Fix off-by-one in the pagination cursor", "Rate-limit login attempts per account", "Remove the dead retry branch", "Document the deploy checklist",
    "Return 409 for duplicate project slugs", "Move invoice totals into the service layer"],
  [WEB]: ["Validate the MFA code length before submitting", "Split the billing panel into list and summary", "Tighten types in the API client",
    "Show server errors inline on the login form", "Debounce the search box", "Use design tokens for form spacing", "Add tests for the email validator"],
  [LUMEN]: ["Cache tokenizer output per shard", "Warm up the learning rate over 500 steps", "Report per-task metrics in the eval summary",
    "Resume from the latest checkpoint by default", "Drop near-duplicate documents before sharding", "Fix the attention mask for padded batches", "Add the large config"] };

// kind/model/effort as the console shows them; focus: the files an agent mostly works on; mix: [read, edit, shell, grep] weights
const AGENTS = [
  { id: "auth-refactor", kind: "claude", model: "claude-opus-5-5", effort: "high", cwd: ATLAS, branch: "auth/session-rotation", window: 1000000,
    about: "Rotating refresh tokens and moving sessions into their own table", focus: /auth\/|sessions|test_auth|0041/, mix: [40, 30, 22, 8] },
  { id: "codex-2", renameTo: "api-tests", kind: "codex", model: "gpt-5-codex", effort: "medium", cwd: ATLAS, branch: "tests/billing-webhooks", window: 272000,
    about: "Raising test coverage on the billing and webhook handlers", focus: /tests\/|billing|webhooks/, mix: [38, 28, 28, 6] },
  { id: "migrations", kind: "claude", model: "claude-sonnet-5-5", effort: "medium", cwd: ATLAS, branch: "db/invoices", window: 200000,
    about: "Invoice tables: migration 0043 and the models that go with it", focus: /migrations\/|models|db\.py|schemas|seed_db/, mix: [42, 30, 22, 6] },
  { id: "reviewer", kind: "claude", model: "claude-opus-5-5", effort: "high", cwd: ATLAS, extra: [WEB], branch: "main", window: 1000000, chatty: 0.6,
    about: "Reviewing the open branches before Friday's release", focus: /auth|billing|api\.ts|LoginForm|docs\//, mix: [66, 4, 16, 14] },
  { id: "web-login", kind: "claude", model: "claude-sonnet-5-5", effort: "medium", cwd: WEB, branch: "web/mfa-login", window: 200000,
    about: "Login and signup forms with the new MFA step", focus: /Login|Signup|Mfa|lib\/auth|validators|forms\.css/, mix: [38, 34, 20, 8] },
  { id: "web-billing", kind: "codex", model: "gpt-5-codex", effort: "high", cwd: WEB, branch: "web/invoices", window: 272000, holds: true,
    about: "Billing panel: invoice list, totals and empty states", focus: /Billing|Invoice|lib\/api|format|api\.test/, mix: [36, 34, 24, 6] },
  { id: "trainer", kind: "claude", model: "claude-opus-5-5", effort: "high", cwd: LUMEN, branch: "exp/cosine-warmup", window: 1000000,
    about: "Comparing warmup schedules on the small config", focus: /train\/|configs\/|launch|test_schedule/, mix: [36, 24, 34, 6] },
  { id: "data-prep", kind: "codex", model: "gpt-5-codex", effort: "medium", cwd: LUMEN, branch: "data/dedupe", window: 272000,
    about: "Deduplicating the corpus before sharding", focus: /data\/|prep_shards|test_loader|test_tokenize|docs\/data/, mix: [40, 30, 24, 6] },
  { id: "eval-bench", kind: "claude", model: "claude-sonnet-5-5", effort: "medium", cwd: LUMEN, branch: "eval/per-task", window: 200000, stopAfter: 15 * 3600,
    about: "Per-task breakdown in the eval report", focus: /eval\/|test_metrics|notebooks/, mix: [44, 28, 22, 6] },
];

// ---------- text pools ----------
const T = {
  peer: ["I'm about to change the signature of `{sym}` in {file}. Are you calling it anywhere on your branch?",
    "Heads up: {file} now raises instead of returning None when the lookup fails. Your tests in {test} will need updating.",
    "Done with {file}; it's all yours. I left a TODO at the top about the retry path.",
    "Can you rerun {test}? I see 2 failures after rebasing on main, both in the fixture setup.",
    "Reviewed your last commit. One issue: {file} swallows the exception around line {n}. Otherwise it looks fine.",
    "Which of us owns {file}? I have a small fix there but don't want to collide with you.",
    "Merged main into my branch. The only conflicts were in {file}; I resolved them in favour of your version.",
    "The flaky test was a shared temp dir, not your change. Fixed in {test}.",
    "Before you touch {file}: I have uncommitted edits there. Pushing in about 10 minutes.",
    "Thanks, that fixed it. {test} passes locally now ({n} passed).",
    "I need the new field from {file} to land before I can finish. Do you have an ETA?",
    "FYI I kept the old `{sym}` as a thin wrapper so your callers keep working until you migrate.",
    "Your change to {file} breaks the type check on my side: `{sym}` no longer accepts None. Intended?",
    "I read through {file}. The second branch can't be reached any more, so I'd delete it rather than fix it."],
  board: ["Status: {about}. Tests are green on my branch; what remains is docs.",
    "Note for everyone here: `make check` now runs the type checker too. Expect new failures in untyped modules.",
    "Claiming {file} for the next hour.", "Released {file}; nobody should be blocked on me now.",
    "CI is red on main because of {test}. I'm looking at it.",
    "I rebased on main. If you depend on `{sym}`, pull before you continue.",
    "Finished a pass over {file}: {n} lines removed, behaviour unchanged, tests still pass."],
  ask: ["Question: should expired sessions be deleted or kept for audit? I've implemented deletion behind a flag for now.",
    "I need a decision: keep the old response shape for `{sym}` or break it? Three callers depend on it.",
    "Finished: {about}. All tests pass. It's ready for you to review.",
    "Blocked: I don't have credentials for the staging database, so I can't do the dry run.",
    "The benchmark regressed 6% after my change to {file}. Do you want the simpler code or the faster one?",
    "Two options for {file}: (a) patch it in place, (b) split it into two modules. I'd pick (b). OK to proceed?",
    "{test} has been failing on main since yesterday and it isn't mine. Should I fix it or leave it to its owner?",
    "The run finished: validation loss went from 2.31 to 2.18 over 4k steps. Want me to launch the large config?"],
  reply: ["Go with (b). Keep the commits small.", "Delete them; the audit log lives elsewhere.", "Keep the old shape for now. We'll break it in the next major version.",
    "Looks good, merge it.", "Yes, go ahead, but cap it at 8 GPU-hours.", "Fix it, and tell its owner what you changed.",
    "Simpler code. 6% doesn't matter on that path.", "I'll get you the credentials this afternoon. Work on something else until then."],
  order: ["Please also update the docs in {file}.", "Stop what you're doing and look at the failing CI on main first.",
    "When you reach a good stopping point, send me a two-line summary.", "Check with {peer} before changing {file}; they are in there too."],
  orderAll: ["Release freeze starts Friday at 17:00. Only fixes after that.", "Please rebase on main, all of you: the lockfile changed.",
    "Reminder: post to the board when you claim a file, so two of you don't edit it at once."],
  prompt: ["continue", "run the tests and fix what fails", "check your inbox and reply", "rebase on main and resolve the conflicts",
    "write tests for the change you just made", "why is {test} failing?", "tidy up and commit", "read {file} and tell me what it does before changing anything",
    "that's not what I meant: keep the public function names as they are"],
  think: ["The failure is in the fixture setup, not in the code under test. I should read the conftest first.",
    "Two callers pass None here. Changing the signature would break them, so a wrapper is safer.",
    "The quickest check is to run only the affected test file before touching anything else.",
    "This duplicates logic in {file}. Better to call the existing helper than to copy it.",
    "I changed {file} in the last turn; the tests there need the same update."],
  done: ["Updated {file}. Ran {test}: {n} passed.", "The cause was a stale fixture. I fixed it in {test} and reran the suite: all green.",
    "I read {file} and the callers of `{sym}`. Nothing else depends on the old behaviour, so the change is safe.",
    "Done. {file} is committed on `{branch}`. One thing left: the docs still describe the old flow.",
    "I replied on the bus and I'm waiting for an answer before changing {file}.",
    "Rebased on main. One conflict in {file}, resolved. Tests pass.", "Partial progress: {file} compiles, but {test} still has 1 failure that I'm looking into."],
};

// ---------- fake file contents: deterministic per path, and the reported size is their length ----------
const WORDS = "load save parse build check merge apply reset fetch store render format verify rotate split count".split(" ");
const NOUNS = "user session token invoice project shard batch config record cursor payload schedule metric request".split(" ");
const TEXTS = new Map();
function fileText(path) {
  if (TEXTS.has(path)) return TEXTS.get(path);
  const r = mulberry(fnv(path)), ext = path.split(".").pop(), stem = path.split("/").pop().replace(/\.\w+$/, "");
  const target = Math.round(Math.exp(5.8 + 3.4 * r() * r())), out = [];
  const v = () => pick(r, WORDS), n = () => pick(r, NOUNS);
  const block = {
    py: () => `def ${v()}_${n()}(${n()}: str, *, strict: bool = False) -> dict[str, object]:\n    """${v()[0].toUpperCase() + v().slice(1)} one ${n()} and return what changed."""\n    if not strict and ${n()} is None:\n        return {}\n    result = _${v()}(${n()})\n    log.debug("${stem}: %s", result)\n    return result\n\n`,
    ts: () => `export function ${v()}${n()[0].toUpperCase() + n().slice(1)}(${n()}: string, limit = ${int(r, 5, 80)}): ${n()[0].toUpperCase() + n().slice(1)}[] {\n  const out = ${v()}(${n()}).slice(0, limit);\n  if (!out.length) throw new Error("no ${n()} for " + ${n()});\n  return out;\n}\n\n`,
    tsx: () => `export function ${stem}${v()[0].toUpperCase() + v().slice(1)}({ ${n()}, onChange }: Props) {\n  const [${n()}, set${n()[0].toUpperCase() + n().slice(1)}] = useState("");\n  return (\n    <label className="field">\n      {${n()}.label}\n      <input value={${n()}} onChange={e => onChange(e.target.value)} />\n    </label>\n  );\n}\n\n`,
    md: () => `## ${v()[0].toUpperCase() + v().slice(1)} the ${n()}\n\nEach ${n()} is ${v()}ed once per ${n()}, then cached. To ${v()} it by hand:\n\n    make ${v()}-${n()}\n\nIf the ${n()} is missing, the command stops with an error instead of guessing.\n\n`,
    yaml: () => `${n()}_${v()}:\n  ${n()}: ${int(r, 1, 512)}\n  ${v()}_every: ${int(r, 10, 2000)}\n  enabled: ${r() < 0.7}\n`,
    css: () => `.${n()}-${v()} {\n  display: flex;\n  gap: var(--space-${int(r, 1, 6)});\n  padding: ${int(r, 2, 24)}px;\n  color: var(--${n()});\n}\n\n`,
    sh: () => `${v()}_${n()}() {\n  echo "${v()} ${n()}: $1" >&2\n  python -m ${stem} --${n()} "$1" || return 1\n}\n\n`,
    json: () => `  "${n()}-${v()}": "${int(r, 0, 9)}.${int(r, 0, 20)}.${int(r, 0, 9)}",\n`,
    toml: () => `[tool.${n()}.${v()}]\n${n()} = ${int(r, 1, 99)}\n${v()} = "${n()}"\n\n`,
  };
  const head = { py: `"""${stem.replace(/_/g, " ")}: part of ${repoOf(path)?.split("/").pop()}."""\nfrom __future__ import annotations\n\nimport logging\n\nlog = logging.getLogger(__name__)\n\n\n`,
    ts: `// ${stem}: shared helpers\n\n`, tsx: `import { useState } from "react";\n\n`, md: `# ${stem}\n\n`, sh: `#!/usr/bin/env bash\nset -euo pipefail\n\n`, json: `{\n` };
  const kind = block[ext] ? ext : ext === "ipynb" || ext === "html" ? "json" : "md";
  out.push(head[kind] || "");
  let len = out[0].length;
  while (len < target) { const b = block[kind](); out.push(b); len += b.length; }
  if (kind === "json") out.push(`  "name": "${stem}"\n}\n`);
  const text = out.join("");
  TEXTS.set(path, text);
  return text;
}
const TYPES = { py: "text/x-python", md: "text/markdown", css: "text/css", html: "text/html", json: "application/json", sh: "application/x-sh" };

// ---------- the simulation ----------
const nowS = () => Date.now() / 1000;
const ANCHOR = (Math.floor(nowS() / 43200) - 3) * 43200;  // history starts 36 to 48 hours back, on a 12 h boundary
const GEN = "demo:" + ANCHOR;
const LOG = [], MSGS = [], LOAD = [], QUESTIONS = [], COMMITS = { [ATLAS]: [], [LUMEN]: [] }, COLORS = {}, MTIME = new Map();
let pending = [], seq = 0, nextId = 1, lastTick = ANCHOR, simNow = ANCHOR;
const put = (ts, item) => pending.push({ ts, seq: seq++, ...item });
const HUMAN = { r: mulberry(fnv("human" + ANCHOR)), next: ANCHOR + 2400 };

for (const [i, a] of AGENTS.entries()) {
  a.name = a.id; a.aka = []; a.r = mulberry(fnv(a.id + ANCHOR)); a.sid = "s-" + fnv(a.id + ANCHOR).toString(16).padStart(8, "0");
  a.projects = Object.fromEntries([a.cwd, ...(a.extra || [])].map(p => [p, true]));
  a.repo = repoOf(a.cwd); a.pid = 4100 + i * 173; a.nextPid = a.pid + 1;
  a.files = Object.keys(a.projects).flatMap(root => TREE[root].map(f => root + "/" + f));
  // a fixed popularity order per project, so agents in the same project share their hot files
  a.weights = a.files.map(f => (a.focus.test(f) ? 6 : 1) / (1 + 12 * h01(f)));
  a.state = "idle"; a.started_at = ANCHOR + 30 + i * 41; a.horizon = a.started_at + a.r() * 3000; a.left = 0;
  a.tokens = 0; a.context = int(a.r, 12000, 40000); a.recent = []; a.inbox = []; a.tr = []; a.off = 0; a.calls = 0;
  a.children = []; a.dirty = new Map(); a.set = [];
  if (a.renameTo) a.renameAt = ANCHOR + 5 * 3600;
  if (a.stopAfter) a.stopAt = ANCHOR + a.stopAfter;
  put(a.started_at, { a, ev: { event: "join", kind: a.kind, host: HOST, wanted: a.id, projects: Object.keys(a.projects) } });
}
const alive = (a, t) => !(a.stopAt && t >= a.stopAt);
const peersOf = (a, t) => AGENTS.filter(b => b !== a && b.repo === a.repo && alive(b, t));
const names = a => [a.name, ...a.aka];
const byName = n => AGENTS.find(a => a.name === n);
const weighted = (r, items, w) => { let x = r() * w.reduce((s, y) => s + y, 0); for (let i = 0; i < items.length; i++) if ((x -= w[i]) < 0) return items[i]; return items[items.length - 1]; };
const rel = (a, p) => under(p, a.cwd) ? p.slice(a.cwd.length + 1) : p;
const testOf = a => { const ts = a.files.filter(f => /test/i.test(f)); return rel(a, ts.length ? pick(a.r, ts) : a.files[0]); };
const slots = (a, t, file) => ({ file: rel(a, file || pick(a.r, a.set.length ? a.set : a.files)), test: testOf(a), sym: pick(a.r, SYMS[under(a.cwd, WEB) ? WEB : a.repo]),
  n: int(a.r, 3, 64), about: a.about, branch: a.branch, peer: pick(a.r, peersOf(a, t))?.name || "reviewer" });

// transcript entries: ids are "<file offset>:<k>" like the server's, increasing with time
const entry = (a, ts, e) => put(ts, { a, tr: e });
// one turn of one agent: a prompt, some tool calls, maybe a message and a commit, then idle again
function nextTurn(a) {
  const r = a.r, codex = a.kind === "codex";
  let t = a.horizon;
  if (a.left <= 0) {  // a new work session after a break, with a fresh set of files in hand
    t += 1800 + r() * r() * 14400; a.left = int(r, 3, 11);
    a.set = Array.from({ length: 4 }, () => weighted(r, a.files, a.weights));
  } else t += 15 + r() * r() * 400;
  a.left--;
  if (a.stopAt && t > a.stopAt) { put(a.stopAt, { a, off: true }); a.done = true; a.horizon = Infinity; return; }
  if (a.renameTo && t > a.renameAt) { put(t - 2, { a, rename: a.renameTo }); a.renameTo = null; }
  const wake = r() < 0.3, S = slots(a, t);
  put(t, { a, ev: { event: "prompt", wake } });
  put(t, { a, ev: { event: "state", state: "busy" } });
  entry(a, t, { kind: "prompt", text: wake ? `[arke] 1 unread message from ${S.peer}. Read it with \`arke inbox\`.` : fill(pick(r, T.prompt), S) });
  if (r() < 0.5) entry(a, t += 2, { kind: "thinking", text: fill(pick(r, T.think), S) });
  const edited = [], nTools = 3 + Math.floor(r() ** 1.5 * 18), sayAt = r() < (a.chatty || 0.32) ? int(r, 0, nTools - 1) : -1;
  for (let i = 0; i < nTools; i++) {
    t += 3 + r() * r() * 50;
    const op = weighted(r, ["read", "edit", "shell", "grep"], a.mix), file = r() < 0.6 ? pick(r, a.set) : weighted(r, a.files, a.weights);
    const call = `call_${a.sid.slice(2)}_${++a.calls}`, rp = rel(a, file);
    let tool, target, desc = "", command = "", input, edits = [], result;
    if (op === "read") {
      if (codex) { tool = "exec_command"; target = command = `sed -n '1,${int(r, 80, 240)}p' ${rp}`; }
      else { tool = "Read"; target = file; input = { file_path: file }; }
      result = fileText(file).split("\n").slice(0, 12).join("\n");
    } else if (op === "edit") {
      const add = 1 + Math.floor(r() * r() * 40), del = Math.floor(r() * r() * 25);
      edits = [{ path: file, add, del }]; edited.push(file);
      const d = a.dirty.get(file) || { add: 0, del: 0 }; a.dirty.set(file, { add: d.add + add, del: d.del + del });
      if (codex) { tool = "apply_patch"; target = `*** Begin Patch\n*** Update File: ${rp}\n@@`; input = { patch: target }; result = "Success. Updated the following files:\nM " + rp; }
      else { tool = r() < 0.12 ? "Write" : "Edit"; target = file; input = { file_path: file, old_string: "…", new_string: "…" }; result = `The file ${file} has been updated.`; }
    } else if (op === "grep") {
      const sym = pick(r, SYMS[under(a.cwd, WEB) ? WEB : a.repo]);
      if (codex) { tool = "exec_command"; target = command = `rg -n "${sym}" .`; }
      else { tool = "Grep"; target = sym; input = { pattern: sym, path: a.cwd }; }
      result = Array.from({ length: int(r, 1, 5) }, () => `${rel(a, pick(r, a.files))}:${int(r, 8, 220)}:    ${sym}(`).join("\n");
    } else {
      tool = codex ? "exec_command" : "Bash";
      const test = testOf(a), passed = int(r, 6, 58), fails = r() < 0.2 ? int(r, 1, 3) : 0, web = under(a.cwd, WEB);
      const train = a.repo === LUMEN && r() < 0.25, choice = train ? 0 : int(r, 1, 4);
      [command, desc, result] = train ? [`python -m lumen.train.loop --config configs/small.yaml --steps ${int(r, 2, 8) * 500}`, "Train on the small config", `step 500 loss 2.${int(r, 30, 60)}\nstep 1000 loss 2.${int(r, 10, 29)}`]
        : choice === 1 ? [web ? `npm test -- ${test}` : `pytest ${test} -q`, `Run ${test.split("/").pop()}`, fails ? `${fails} failed, ${passed} passed in ${(2 + r() * 9).toFixed(2)}s` : `${passed} passed in ${(2 + r() * 9).toFixed(2)}s`]
        : choice === 2 ? ["git status --short", "Show the working tree", [...a.dirty.keys()].slice(0, 6).map(f => " M " + (under(f, a.repo) ? f.slice(a.repo.length + 1) : f)).join("\n")]
        : choice === 3 ? [web ? "npx tsc --noEmit" : "ruff check .", web ? "Type-check the project" : "Lint the project", web ? "" : "All checks passed!"]
        : ["git diff --stat", "Summarize uncommitted changes", `${a.dirty.size} files changed, ${[...a.dirty.values()].reduce((s, d) => s + d.add, 0)} insertions(+), ${[...a.dirty.values()].reduce((s, d) => s + d.del, 0)} deletions(-)`];
      target = command;
      if (choice <= 1 || choice === 3) {  // a child process the samples can show
        const life = train ? 240 + r() * 900 : 6 + r() * 70;
        a.children.push({ pid: a.nextPid++, cmd: command.split(" ").slice(0, train ? 3 : 2).join(" "), t0: t, t1: t + life, cpu: train ? 280 + r() * 200 : 60 + r() * 90, rss: train ? 5200 + r() * 3000 : 140 + r() * 380, gpu: train });
      }
    }
    put(t, { a, ev: { event: "tool", tool, target, desc, cwd: a.cwd, repo: a.repo, branch: a.branch, host: HOST, projects: Object.keys(a.projects) }, edits });
    entry(a, t, { kind: "tool", call, tool, target, desc, command, input: JSON.stringify(input || { command }), edits });
    entry(a, t + 1, { kind: "result", for: call, error: /failed/.test(result), text: result });
    if (i === sayAt) say(a, t += 2);
  }
  if (a.dirty.size >= 3 && r() < 0.3) {  // commit what's been edited so far
    t += 4 + r() * 20;
    const subject = pick(r, SUBJECTS[under(a.cwd, WEB) ? WEB : a.repo]), command = `git add -A && git commit -m "${subject}"`, call = `call_${a.sid.slice(2)}_${++a.calls}`;
    const files = [...a.dirty].map(([p, d]) => ({ path: p.slice(a.repo.length + 1), ...d }));
    COMMITS[a.repo].push({ hash: Array.from({ length: 5 }, () => Math.floor(r() * 4294967296).toString(16).padStart(8, "0")).join(""), ts: Math.floor(t), author: "Robin Dev", email: "robin@example.com", subject, files });
    put(t, { a, ev: { event: "tool", tool: codex ? "exec_command" : "Bash", target: command, desc: "Commit the changes", cwd: a.cwd, repo: a.repo, branch: a.branch, host: HOST, projects: Object.keys(a.projects) } });
    entry(a, t, { kind: "tool", call, tool: codex ? "exec_command" : "Bash", target: command, desc: "Commit the changes", command, input: JSON.stringify({ command }), edits: [] });
    entry(a, t + 1, { kind: "result", for: call, error: false, text: `[${a.branch} ${Math.floor(r() * 268435456).toString(16).padStart(7, "0")}] ${subject}\n ${files.length} files changed` });
    a.dirty.clear();
  }
  t += 3 + r() * 8;
  entry(a, t, { kind: "assistant", text: fill(pick(r, T.done), slots(a, t, edited[0])) });
  a.tokens += int(r, 300, 3200); a.context += int(r, 1500, 9000);
  if (a.context > a.window * 0.8) { a.context = Math.round(a.window * 0.22); entry(a, t, { kind: "notice", text: "Context compacted", detail: "The conversation was summarized to free up the context window." }); }
  put(t, { a, ev: { event: "usage", source: "hook", model: a.model, effort: a.effort, tokens: a.tokens, context: a.context, window: a.window }, usage: [a.tokens, a.context] });
  put(t, { a, ev: { event: "state", state: "idle" } });
  a.horizon = t;
}
// a message from an agent: mostly to a peer, sometimes to its project's board, the human or everyone
function say(a, t) {
  const r = a.r, x = r(), S = slots(a, t), peers = peersOf(a, t);
  if (x < 0.58 && peers.length) put(t, { msg: { from: a, to: [{ agent: pick(r, peers) }], mode: "notify", text: fill(pick(r, T.peer), S) } });
  else if (x < 0.9) put(t, { msg: { from: a, to: [{ project: a.cwd }], mode: "board", text: fill(pick(r, T.board), S) } });
  else if (x < 0.98) { put(t, { msg: { from: a, to: [{ human: true }], mode: "notify", text: fill(pick(r, T.ask), S) } }); QUESTIONS.push({ ts: t, a, answered: false }); }
  else put(t, { msg: { from: a, to: [{ all: true }], mode: "notify", text: "Main is broken for everyone right now (the lockfile). Don't rebase until I say it's fixed." } });
}
// the person at the console: answers the oldest open question, or sends an instruction
function humanTurn(t) {
  const r = HUMAN.r, open = QUESTIONS.filter(q => q.ts < t - 300 && !q.answered && alive(q.a, t)).sort((x, y) => x.ts - y.ts);
  if (open.length && r() < 0.9) { open[0].answered = true; put(t, { msg: { from: null, to: [{ agent: open[0].a }], mode: "notify", text: pick(r, T.reply) } }); }
  else if (r() < 0.3) put(t, { msg: { from: null, to: [{ project: pick(r, [ATLAS, LUMEN]) }], mode: "notify", text: pick(r, T.orderAll) } });
  else { const a = pick(r, AGENTS.filter(x => alive(x, t))); put(t, { msg: { from: null, to: [{ agent: a }], mode: "notify", text: fill(pick(r, T.order), slots(a, t)) } }); }
}

// ---------- applying events in time order ----------
const reachable = (a, now) => a.state === "offline" ? ["gone", "its tmux pane is gone (the session ended)"]
  : a.state === "idle" && now - a.last_active > 3 * 3600 ? ["stale", `no hook activity for ${Math.round((now - a.last_active) / 3600)} h`] : ["yes", ""];
const membersOf = path => AGENTS.filter(a => a.state !== "offline" && Object.keys(a.projects).some(p => under(p, path)));
function deliver(ts, m) {
  const from = m.from ? m.from.name : "human", to = [], reach = new Set();
  for (const t of m.to) {
    if (t.agent) { to.push({ name: t.agent.name, kind: "agent" }); reach.add(t.agent.name); }
    else if (t.human) { to.push({ name: "human", kind: "human" }); reach.add("human"); }
    else if (t.project) { to.push({ name: t.project, kind: "project" }); for (const a of membersOf(t.project)) reach.add(a.name); }
    else { to.push({ name: "*", kind: "broadcast" }); for (const a of AGENTS) if (a.state !== "offline") reach.add(a.name); }
  }
  reach.delete(from);
  const msg = { id: nextId++, ts, from, to, reach: [...reach], via: m.from ? "agent" : "console", mode: m.mode, text: m.text,
    host: m.from ? HOST : "web", cwd: m.from?.cwd || "", branch: m.from?.branch || "", projects: m.from ? Object.keys(m.from.projects) : [] };
  MSGS.push(msg); LOG.push({ event: "message", ...msg });
  const bus = outgoing => ({ id: "bus:" + msg.id, ts, kind: "bus", from, to: to.map(t => t.name), text: m.text, outgoing, board: m.mode === "board" });
  if (m.from) { m.from.last_sent = ts; m.from.tr.push(bus(true)); }
  for (const n of reach) {
    const a = byName(n); if (!a) continue;
    a.tr.push(bus(false));
    if (m.from) a.last_recv = ts; else a.last_human = ts;
    if (m.mode === "board") continue;
    a.inbox.push(msg.id);
    a.wake = a.holds ? { status: "held", at: ts, detail: "its pane is waiting at a permission prompt; the wake-up is held until that clears", tries: 2, ids: [msg.id] }
      : { status: a.state === "busy" ? "picked up" : "typed", at: ts, detail: a.state === "busy" ? "read at its next tool call" : "typed into its tmux pane", tries: 1, ids: [msg.id] };
    LOG.push({ ts, event: "wake", agent: n, sender: from, ok: !a.holds, error: a.holds ? "pane busy" : "" });
  }
  return msg;
}
function rename(a, name, ts, by) {
  const old = a.name; a.aka.push(old); a.name = name;
  if (old in COLORS) { COLORS[name] = COLORS[old]; delete COLORS[old]; }
  LOG.push({ ts, event: "rename", agent: name, old, sid: a.sid, by });
}
// process samples every 30 s: each agent's harness process plus the commands it started
function sample(ts, prev) {
  const agents = {}, started = [], ended = [];
  let cpuAll = 0, rssAll = 0, gpu = 0;
  for (const a of AGENTS) {
    if (a.state === "offline" || ts < a.started_at) continue;
    const busy = a.state === "busy", k = Math.round(ts / 30);
    const own = { pid: a.pid, cpu: +(busy ? 6 + 30 * h01(a.id, k) : 0.2 + 1.2 * h01(a.id, k)).toFixed(1), rss_mb: Math.round(360 + 240 * h01(a.id + "m", Math.floor(ts / 3600)) + a.context / 4000), cmd: a.kind };
    const top = [own];
    for (const c of a.children) {
      if (c.t0 > prev && c.t0 <= ts) started.push({ agent: a.name, pid: c.pid, ppid: a.pid, cmd: c.cmd, start: Math.floor(c.t0) });
      if (c.t1 > prev && c.t1 <= ts) ended.push({ agent: a.name, pid: c.pid });
      if (c.t0 <= ts && c.t1 > ts) { top.push({ pid: c.pid, cpu: +(c.cpu * (0.85 + 0.3 * h01(a.id + c.pid, k))).toFixed(1), rss_mb: Math.round(c.rss), cmd: c.cmd }); if (c.gpu) gpu = 1; }
    }
    a.children = a.children.filter(c => c.t1 > ts);
    top.sort((x, y) => y.cpu - x.cpu);
    const cpu = +top.reduce((s, p) => s + p.cpu, 0).toFixed(1), rss = top.reduce((s, p) => s + p.rss_mb, 0);
    agents[a.name] = { cpu, rss_mb: rss, n: top.length, top }; cpuAll += cpu; rssAll += rss;
  }
  LOG.push({ ts, event: "procs", agents, started, ended });
  LOAD.push({ ts, cpu: Math.min(100, cpuAll / CORES + 2 + 4 * h01("cpu", ts)), mem: (9000 + rssAll + 2000 * h01("mem", Math.floor(ts / 7200))) / MEM_MB * 100,
    gpu: gpu ? 88 + 10 * h01("gpu", ts) : 1 + 3 * h01("gpu", ts), gmem: gpu ? 61 + 6 * h01("gm", Math.floor(ts / 600)) : 4 });
}
function advance(now) {
  if (now <= simNow) return;
  for (const a of AGENTS) while (!a.done && a.horizon <= now) nextTurn(a);
  for (; HUMAN.next <= now; HUMAN.next += 900 + HUMAN.r() * HUMAN.r() * 3 * 3600) humanTurn(HUMAN.next);
  for (let t = lastTick + 30; t <= now; t += 30) { put(t, { tick: lastTick }); lastTick = t; }
  const ready = pending.filter(p => p.ts <= now).sort((x, y) => x.ts - y.ts || x.seq - y.seq);
  pending = pending.filter(p => p.ts > now);
  for (const p of ready) {
    const a = p.a, ts = p.ts;
    if (a?.stopAt && ts > a.stopAt) continue;  // the turn running when its session ended never finishes
    if (p.tick != null) sample(ts, p.tick);
    else if (p.msg) deliver(ts, p.msg);
    else if (p.rename) rename(a, p.rename, ts, "human");
    else if (p.off) { a.state = "offline"; a.state_at = ts; LOG.push({ ts, event: "state", agent: a.name, sid: a.sid, state: "offline" }); }
    else if (p.tr) { a.off += 180 + (p.seq % 97); a.tr.push({ id: a.off + ":0", ts, ...p.tr }); }
    else {
      const e = { ts, agent: a.name, sid: a.sid, ...p.ev };
      LOG.push(e); a.last_active = ts;
      if (e.event === "state") { a.state = e.state; a.state_at = ts; }
      else if (e.event === "prompt") { a.inbox = []; if (!e.wake) a.last_prompt = ts; }
      else if (e.event === "usage") [a.liveTokens, a.liveContext] = p.usage;
      else if (e.event === "tool") {
        a.recent.unshift({ ts, tool: e.tool, target: e.target.slice(0, 300), desc: e.desc }); a.recent.length = Math.min(a.recent.length, 12);
        for (const d of p.edits || []) MTIME.set(d.path, ts);
      }
    }
  }
  simNow = now;
}

// ---------- the API ----------
const err = (status, error) => ({ status, body: { error } });
function publicAgent(a, now) {
  const [reach, why] = reachable(a, now);
  return { name: a.name, kind: a.kind, host: HOST, cwd: a.cwd, branch: a.branch, tmux: `${a.repo.split("/").pop()}:${AGENTS.indexOf(a) + 1}.0`, tmux_session: a.repo.split("/").pop(),
    model: a.model, effort: a.effort, about: a.about, state: a.state, started_at: a.started_at, tokens: a.liveTokens || 0, context: a.liveContext || 0, window: a.window, window_est: false,
    projects: a.projects, default_project: a.cwd, aka: a.aka, named: true, pid: a.pid, last_active: a.last_active, state_at: a.state_at, last_prompt: a.last_prompt, last_sent: a.last_sent,
    last_recv: a.last_recv, last_human: a.last_human, wake: a.wake, unread: a.inbox.length, unread_ids: a.inbox, local: true, reachable: reach, reachable_why: why,
    presence: a.state === "offline" ? "ended" : "recent", color: COLORS[a.name] ?? null, procs: null, files: a.recent };
}
function projects() {
  const paths = new Set();
  for (const a of AGENTS) for (const p of Object.keys(a.projects)) p.split("/").slice(1).forEach((_, i, parts) => paths.add("/" + parts.slice(0, i + 1).join("/")));
  return [...paths].sort().map(p => { const n = AGENTS.filter(a => Object.keys(a.projects).some(q => under(q, p))).length; return { path: p, members: n, subscribed: n, color: COLORS[p] ?? null }; });
}
function log(q) {
  const since = Number(q.get("since")) || 0, before = Number(q.get("before")) || Infinity, limit = Math.min(Number(q.get("limit")) || 200, 1000);
  const agent = q.get("agent") || "", project = q.get("project") || "", inner = q.get("scope") === "inner";
  const who = agent && AGENTS.find(a => names(a).includes(agent)), alias = new Set(who ? names(who) : [agent]);
  const members = new Set(AGENTS.filter(a => Object.keys(a.projects).some(p => under(p, project))).flatMap(names)), out = [];
  for (let i = MSGS.length - 1; i >= 0 && out.length < limit; i--) {
    const m = MSGS[i];
    if (m.id <= since) break;
    if (m.id >= before) continue;
    if (agent && !(alias.has(m.from) || m.reach.some(n => alias.has(n)))) continue;
    if (project) {
      const toProject = m.to.some(t => t.kind === "project" && (under(t.name, project) || under(project, t.name))), reaches = m.reach.some(n => members.has(n));
      if (!(toProject || reaches || m.to.some(t => t.kind === "broadcast") || m.projects.some(p => under(p, project)))) continue;
      if (inner && !(toProject || m.from === "human" || m.to.some(t => t.kind === "human") || (members.has(m.from) && reaches))) continue;
    }
    out.push(m);
  }
  return out.reverse();
}
function resolve(list, now) {
  const r = { to: [], reach: [], unknown: [], unsubscribed: [], renamed: [], ambiguous: [], has_project: false, broadcast: false }, reach = new Set(), targets = [];
  for (const n of list) {
    const a = AGENTS.find(x => names(x).includes(n));
    if (n === "*") { r.broadcast = true; r.to.push({ name: "*", kind: "broadcast" }); targets.push({ all: true }); for (const x of AGENTS) if (x.state !== "offline") reach.add(x.name); }
    else if (n === "human") { r.to.push({ name: n, kind: "human" }); targets.push({ human: true }); }
    else if (n.startsWith("/")) {
      const members = membersOf(n);
      if (!projects().some(p => p.path === n)) { r.unknown.push(n); continue; }
      if (!members.length) r.unsubscribed.push(n);
      r.has_project = true; r.to.push({ name: n, kind: "project" }); targets.push({ project: n }); for (const x of members) reach.add(x.name);
    } else if (a) { if (a.name !== n) r.renamed.push({ old: n, new: a.name }); r.to.push({ name: a.name, kind: "agent" }); targets.push({ agent: a }); reach.add(a.name); }
    else r.unknown.push(n);
  }
  r.reach = [...reach];
  r.unreachable = r.reach.map(n => [n, ...reachable(byName(n), now)]).filter(x => x[1] !== "yes").map(([name, reachable, why]) => ({ name, reachable, why }));
  return [r, targets];
}
function send(body, now) {
  const text = String(body.text || "").trim(), [r, targets] = resolve(Array.isArray(body.to) ? body.to.map(String) : [], now);
  if (!text || !targets.length && !r.unknown.length) return err(400, "a message needs a recipient and some text");
  if (r.unknown.length) return err(400, `no agent or project named ${r.unknown.join(", ")}`);
  const mode = body.mode === "board" && r.has_project ? "board" : "notify", m = deliver(now, { from: null, to: targets, mode, text });
  // one agent addressed directly answers after a moment, so the console has something to show for it
  const one = targets.length === 1 && targets[0].agent;
  if (one && one.state !== "offline") put(now + 6 + Math.random() * 9, { msg: { from: one, to: [{ human: true }], mode: "notify",
    text: pick(Math.random, ["Got it. I'll finish the current test run first, then switch to that.", "Understood, on it.", "OK. I'll report back here when it's done.", "Noted. That changes my plan slightly; starting now."]) } });
  return { ...r, id: m.id, mode };
}
function transcript(a, q) {
  const at = c => Math.max(0, Math.min(a.tr.length, Number(String(c).slice(1)) || 0)), limit = Math.min(Number(q.get("limit")) || 200, 500);
  if (q.has("cursor")) return { available: true, entries: a.tr.slice(at(q.get("cursor"))), next: "c" + a.tr.length };
  const end = q.has("before") ? at(q.get("before")) : a.tr.length, start = Math.max(0, end - limit);
  const r = { available: true, entries: a.tr.slice(start, end), prev: start ? "c" + start : null };
  if (!q.has("before")) r.next = "c" + a.tr.length;
  return r;
}
function events(q) {
  const since = Number(q.get("since")) || 0, limit = Math.max(1, Math.min(Number(q.get("limit")) || 5000, 50000))  /* the server's default and maximum */, types = q.get("type") ? new Set(q.get("type").split(",")) : null;
  if (since > LOG.length * REC || (since && q.get("generation") && q.get("generation") !== GEN)) return err(400, "the log is shorter than that offset");
  const out = [];
  let i = Math.ceil(since / REC);
  for (; i < LOG.length && out.length < limit; i++) if (!types || types.has(LOG[i].event)) out.push(LOG[i]);
  return { events: out, next: i * REC, generation: GEN };
}
function horatio(q, now) {
  const hours = Math.min(Number(q.get("hours")) || 24, 48), from = now - hours * 3600, bins = new Map();
  for (const s of LOAD) if (s.ts >= from) { const k = Math.floor(s.ts / 600); if (!bins.has(k)) bins.set(k, []); bins.get(k).push(s); }
  const avg = (list, f) => +(list.reduce((s, x) => s + x[f], 0) / list.length).toFixed(1);
  return { records: [...bins].map(([k, list]) => ({ timestamp: new Date((k + 1) * 600000).toISOString(),
    cpu: { avg: avg(list, "cpu"), min: +Math.min(...list.map(x => x.cpu)).toFixed(1), max: +Math.max(...list.map(x => x.cpu)).toFixed(1) },
    memory: { percent: avg(list, "mem"), used_gb: +(avg(list, "mem") / 100 * MEM_MB / 1024).toFixed(1), total_gb: MEM_MB / 1024 },
    gpus: [{ index: 0, util_percent: avg(list, "gpu"), mem_percent: avg(list, "gmem"), mem_used_gb: +(avg(list, "gmem") / 100 * 24).toFixed(1), mem_total_gb: 24 }] })) };
}
function fileInfo(p, now) {
  if (!KNOWN.has(p)) return { path: p, available: false, reason: `not a file inside a working directory of an agent on ${HOST} (or it no longer exists)` };
  const text = fileText(p), ext = p.split(".").pop();
  return { path: p, resolved: p, available: true, size: text.length, mtime: MTIME.get(p) || ANCHOR - 86400 * (1 + Math.floor(30 * h01(p))), type: TYPES[ext] || ext, text };
}

const ROUTES = {
  "GET /agents": (q, now) => AGENTS.map(a => publicAgent(a, now)),
  "GET /projects": projects,
  "GET /log": log,
  "GET /resolve": (q, now) => resolve((q.get("to") || "").split(",").map(s => s.trim()).filter(Boolean), now)[0],
  "GET /events": events,
  "GET /machine": () => ({ host: HOST, cores: CORES, mem_total_mb: MEM_MB }),
  "GET /horatio/history": horatio,
  "GET /fileinfo": (q, now) => fileInfo(q.get("path") || "", now),
  "GET /filesizes": q => Object.fromEntries(q.getAll("path").slice(0, 500).map(p => [p, KNOWN.has(p) ? fileText(p).length : null])),
  "GET /git": (q, now) => {
    const repo = repoOf(q.get("cwd") || ""), since = Number(q.get("since")) || 0, limit = Math.min(Number(q.get("limit")) || 200, 500);
    return repo ? { repo, commits: COMMITS[repo].filter(c => c.ts >= since && c.ts <= now).sort((a, b) => b.ts - a.ts).slice(0, limit) } : err(404, "not inside a git repository of a local agent");
  },
  "POST /send": (q, now, body) => send(body, now),
  "POST /name": (q, now, body) => {
    const a = byName(String(body.agent)), name = String(body.name || "");
    if (!a) return err(404, `no agent named ${body.agent}`);
    if (!/^[A-Za-z0-9._-]{1,40}$/.test(name) || name === "human") return err(400, "a name is 1-40 characters of A-Z a-z 0-9 . _ -");
    if (AGENTS.some(x => x !== a && x.name === name)) return err(409, `${name} is taken`);
    const old = a.name; if (old !== name) rename(a, name, now, "human");
    return { name, old };
  },
  "POST /color": (q, now, body) => {
    const out = {};
    for (const [t, c] of Object.entries(body.colors || {})) {
      if (c != null && !/^#[0-9a-f]{6}$/i.test(c)) return err(400, "a color is #rrggbb or null");
      if (c == null) delete COLORS[t]; else COLORS[t] = c;
      out[t] = c ?? null;
    }
    return { colors: out };
  },
};
// [status, body], or null when the path isn't the bus's (a page, a script, the stylesheet)
function route(method, path, query, body = {}) {
  const now = nowS();
  advance(now);
  const tr = method === "GET" && /^\/agents\/([^/]+)\/transcript$/.exec(path);
  let answer;
  if (tr) { const a = byName(decodeURIComponent(tr[1])); answer = a ? transcript(a, query) : err(404, "no such agent"); }
  else if (ROUTES[method + " " + path]) answer = ROUTES[method + " " + path](query, now, body);
  else if (path.split("/").pop().includes(".")) return null;
  else { misses.push(method + " " + path); console.warn("demo: no fake for", method, path); answer = err(404, "the demo has no answer for this request"); }
  return answer && answer.status && answer.body ? [answer.status, answer.body] : [200, answer];
}
const misses = [];

const realFetch = globalThis.fetch.bind(globalThis);
const wait = (ms, signal) => new Promise((done, fail) => {
  const stop = () => fail(signal.reason ?? new DOMException("aborted", "AbortError"));
  if (signal?.aborted) return stop();
  signal?.addEventListener("abort", stop, { once: true });
  setTimeout(done, ms);
});
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === "string" ? input : input.url ?? String(input), location.href), method = (init.method || "GET").toUpperCase();
  if (url.origin !== location.origin) return realFetch(input, init);
  if (method === "GET" && url.pathname === "/help") return realFetch(new URL("help.md", document.baseURI));  // text, copied in by the build
  let body = {};
  try { if (typeof init.body === "string") body = JSON.parse(init.body); } catch { /* answered as an empty body */ }
  const answer = route(method, url.pathname, url.searchParams, body);
  if (!answer) return realFetch(input, init);
  await wait(15 + 35 * Math.random(), init.signal);
  return new Response(JSON.stringify(answer[1]), { status: answer[0], headers: { "Content-Type": "application/json" } });
};
globalThis.ArkeDemo = { route, misses, agents: AGENTS };

addEventListener("DOMContentLoaded", () => {
  const b = document.createElement("div");
  b.style.cssText = "position:fixed;right:10px;bottom:10px;z-index:1000;padding:3px 9px;border-radius:999px;font:12px system-ui,sans-serif;"
    + "color:#fff;background:#1f2937cc;pointer-events:none";
  b.innerHTML = `Demo · synthetic data · <a href="https://github.com/mivanit/arke" style="color:#9cc2ff;pointer-events:auto">GitHub ↗</a>`;
  document.body.append(b);
});
})();
