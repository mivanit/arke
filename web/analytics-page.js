// Page code shared by analytics.html and analytics3d.html (not the console): helpers, the bus, the event log, the view, the side pane.
// Loaded as a classic script before each page's own script, so these names are globals. The pages assign
// agentsNow / projects; a page must not declare its own (a module-scoped `let` would silently shadow them).
"use strict";
const nowS = () => Date.now() / 1000;
const pad = n => String(n).padStart(2, "0");
const clock = (ts, sec = false) => { const d = new Date(ts * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + (sec ? ":" + pad(d.getSeconds()) : ""); };
const dayOf = ts => new Date(ts * 1000).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
const dur = s => { s = Math.max(0, Math.round(s)); if (s < 60) return s + "s"; if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h" + pad(Math.floor(s % 3600 / 60)) + "m"; return Math.floor(s / 86400) + "d" + Math.floor(s % 86400 / 3600) + "h"; };
const kb = b => b == null ? "?" : b < 1024 ? b + " B" : b < 1048576 ? (b / 1024).toFixed(1) + " kB" : (b / 1048576).toFixed(1) + " MB";

// ---------- bus: every request goes to the server that served the page ----------
const agentHref = n => "/#agent/" + encodeURIComponent(n);
const projHref = p => "/#project/" + encodeURIComponent(p);
async function api(path) {
  let r;
  try { r = await netFetch(path); }
  catch (e) { if (e.key) throw e; throw new Error(`no answer from ${BUS_LABEL} for ${path.slice(0, 80)} (${e.message}). Is server.py running there?`); }
  if (!(r.headers.get("Content-Type") || "").startsWith("application/json"))
    throw new Error(`${BUS_LABEL} isn't the arke server (it answered ${path.slice(0, 80)} with ${r.headers.get("Content-Type") || "no content type"}). Open the console from the bus itself.`);
  const body = await r.json();
  if (!r.ok) { const e = new Error(body.error || r.statusText); e.status = r.status; throw e; }
  return body;
}

// ---------- colors (base.js), with the agents and projects the page loaded ----------
let agentsNow = [], projects = [];
const colorOf = n => esc((n.startsWith("/") ? projects.find(p => p.path === n) : agentsNow.find(a => a.name === n))?.color || defColor(n));  // server colors land in style attributes

// ---------- the event log, and the model built from it (analytics-shared.js; rebuilt on every new batch: the whole log is a few MB) ----------
let raw = [], next = 0, M = null, eventGeneration = "";
function build() { M = buildModel(raw, agentsNow, nowS()); }
// read the events of `types` since the last read, then the agents and projects; restarted() runs when the log was replaced.
// Returns why the log was reread from the start, or "".
async function readEvents(types, restarted) {
  let got = 0, rotated = "";
  const restart = why => { rotated = why; console.warn(why); raw.length = 0; next = 0; eventGeneration = ""; restarted?.(); };
  for (;;) {
    let r;
    try { r = await api(`/events?since=${next}&type=${types}&limit=20000&procs=totals&generation=${encodeURIComponent(eventGeneration)}`); }
    catch (e) {
      // 400 on a nonzero cursor: the log was rotated or truncated under us, so the old offset will never be valid again.
      // Reread from the start and drop what we had, or every event would appear twice.
      if (e.status !== 400 || (!next && !eventGeneration)) throw e;
      restart(`event log shrank (${e.message}): reloaded from the start`); continue;
    }
    // the log went missing or was replaced by a shorter one (a 200 with an earlier next)
    if (r.next < next) { restart("event log was replaced: reloaded from the start"); continue; }
    eventGeneration = r.generation || eventGeneration;
    // procs=totals: the server cut procs events to each agent's cpu/rss (their process lists are most of the log)
    for (const e of r.events) raw.push(e); got += r.events.length;
    const moved = r.next !== next; next = r.next;
    if (r.events.length < 20000 || !moved) break;
    $("status").textContent = `loading… ${raw.length} events`;
  }
  const [ag, pr] = await Promise.all([api("/agents"), api("/projects")]);
  agentsNow = ag; projects = pr; buildDefaults([...projects.map(p => p.path), ...agentsNow.map(a => a.name)]);
  if (got || rotated || !M) build(); else M.t1 = Math.max(M.t1, nowS());
  fillProjects();
  return rotated;
}

// ---------- view state ----------
let range = Number(pref("an.range", "21600"));  // seconds back from now; 0 = all
let win = null;  // [w0, w1] when zoomed (2D only); null = follow range
const winNow = () => win || [range ? Math.max(M.t0, M.t1 - range) : M.t0, M.t1];
const projSel = () => $("proj").value;
function fillProjects() {
  const s = $("proj"), cur = s.value, ps = projects.map(p => p.path);
  const html = `<option value="">all projects</option>` + ps.map(p => `<option${p === cur ? " selected" : ""}>${esc(p)}</option>`).join("");
  if (s.innerHTML !== html) s.innerHTML = html;
}
const inProj = L => !projSel() || [...L.projects].some(p => under(p, projSel()) || under(projSel(), p)) || L.name === "human";
const STEPS = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800];  // time axis ticks (s)

// ---------- graph positions, shared by the 2D Graph panel and the 3D view ----------
// localStorage "ab.an.graph.<host>": {name: [x, y, pinned]}, x and y normalized to [0, 1]
const GKEY = "ab.an.graph." + BUS_LABEL;
function savedPositions() {
  const out = new Map();
  try { for (const [n, v] of Object.entries(JSON.parse(localStorage.getItem(GKEY) || "{}"))) out.set(n, { x: v[0], y: v[1], pinned: !!v[2] }); }
  catch (e) { console.warn("can't read saved graph positions", e); }
  return out;
}
function writePositions(pos) {
  const o = Object.create(null);
  for (const [n, p] of pos) o[n] = [Math.round(p.x * 1e4) / 1e4, Math.round(p.y * 1e4) / 1e4, p.pinned ? 1 : 0];
  try { localStorage.setItem(GKEY, JSON.stringify(o)); } catch (e) { console.warn("can't save graph positions", e); }
}

// the who-talks-to-whom graph for the window (2D Graph panel and 3D view): agents with activity (tool calls, messages),
// optionally human and project nodes; edges {a, b, ab, ba, w} with a < b
function graphData(w0, w1, withProj, withHuman) {
  const act = new Map(), stat = new Map(), add = (n, k) => { act.set(n, (act.get(n) || 0) + 1); const s = stat.get(n) || { tools: 0, sent: 0, recv: 0 }; s[k]++; stat.set(n, s); };
  for (const L of M.lanes.values()) if (inProj(L) && L.name !== "human") for (const e of L.tools) if (e.ts >= w0 && e.ts <= w1) add(L.name, "tools");
  const edges = new Map(), edge = (a, b) => { const k = a < b ? a + "\u0000" + b : b + "\u0000" + a; if (!edges.has(k)) edges.set(k, { a: a < b ? a : b, b: a < b ? b : a, ab: 0, ba: 0 }); const e = edges.get(k); a < b ? e.ab++ : e.ba++; };
  const ok = n => n !== "?" && n !== "*" && (withHuman || n !== "human") && (n.startsWith("/") || n === "human" || !M.lanes.has(n) || inProj(M.lanes.get(n)));
  for (const m of M.msgs) {
    if (m.ts < w0 || m.ts > w1 || !ok(m.from)) continue;
    let any = false;
    for (const r of m.reach) if (r !== m.from && ok(r)) { edge(m.from, r); add(r, "recv"); any = true; }
    if (withProj) for (const t of m.to) if (t.kind === "project" && ok(t.name)) { edge(m.from, t.name); add(t.name, "recv"); any = true; }
    if (any || m.reach.length === 0) add(m.from, "sent");
  }
  const names = new Set([...act.keys()].filter(ok));
  for (const e of edges.values()) { names.add(e.a); names.add(e.b); }
  return { names: [...names].sort(), act, stat, edges: [...edges.values()].map(e => ({ ...e, w: e.ab + e.ba })) };
}

// ---------- side pane: details for a file or an agent ----------
// Files are M.touches entries, keyed by host + normalized path. A page can set paneResized() to redraw when the pane moves.
const fileAt = key => M?.touches.find(t => t.key === key);
const fileTitle = key => { const f = fileAt(key); return f ? fileLabel(f) : ""; };
let paneResized = () => {};
const pane = { kind: "", key: "", seq: 0 };
function paneSetWidth(px) {
  const w = Math.max(280, Math.min(Math.round(px), Math.round(innerWidth * 0.7)));
  document.documentElement.style.setProperty("--pw", w + "px");
  return w;
}
paneSetWidth(Number(pref("an.panew", "440")));
function paneShow(title, html) {
  $("pane").hidden = false; document.documentElement.classList.add("pane-open");
  $("ptitle").innerHTML = title; $("pbody").innerHTML = html; $("pbody").scrollTop = 0;
  paneResized();
}
function paneClose() {
  $("pane").hidden = true; document.documentElement.classList.remove("pane-open"); pane.kind = ""; pane.key = "";
  paneResized();
}
// a strip per agent over the file's whole history: tall ticks = edits, short = reads
function paneTimeline(touches) {
  if (!touches.length) return "";
  const t0 = touches[0].ts, t1 = Math.max(touches[touches.length - 1].ts, t0 + 60), W = 300, RH = 14;
  const by = new Map(); for (const t of touches) { if (!by.has(t.agent)) by.set(t.agent, []); by.get(t.agent).push(t); }
  const rows = [...by].sort((a, b) => b[1].length - a[1].length).slice(0, 8);
  let h = "";
  rows.forEach(([ag, ts], i) => {
    const y = i * RH, c = colorOf(ag);
    h += `<text x="0" y="${y + 10}" class="pt-l">${esc(ag.length > 14 ? ag.slice(0, 13) + "…" : ag)}</text><line x1="92" x2="${92 + W}" y1="${y + 7}" y2="${y + 7}" stroke="var(--line)"/>`;
    for (const t of ts) { const x = 92 + (t.ts - t0) / (t1 - t0) * W;
      h += `<line x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="${y + (t.write ? 1 : 4)}" y2="${y + (t.write ? 13 : 10)}" stroke="${c}" stroke-width="${t.write ? 2 : 1.2}"><title>${esc(ag)} ${t.write ? "edited" : "read"} ${dayOf(t.ts)} ${clock(t.ts, true)} (${esc(t.tool)})</title></line>`; }
  });
  const H = rows.length * RH;
  return `<svg class="ptl" viewBox="0 0 ${92 + W + 4} ${H + 14}" width="100%">${h}<text x="92" y="${H + 11}" class="pt-l">${dayOf(t0)} ${clock(t0)}</text><text x="${92 + W}" y="${H + 11}" class="pt-l" text-anchor="end">${clock(t1)}</text></svg>`
    + (by.size > rows.length ? `<div class="sub">+ ${by.size - rows.length} more agents</div>` : "");
}
async function paneFile(key) {
  const seq = ++pane.seq; pane.kind = "file"; pane.key = key;
  const f = fileAt(key), file = f ? { path: f.path, label: fileLabel(f), local: isLocalFile(f, agentsNow) } : { path: key, label: key, local: true }, abs = file.path;
  const touches = M.touches.filter(t => t.key === key).map(t => ({ ts: t.ts, agent: t.agent, write: t.write, tool: t.ev.tool, target: t.ev.target, desc: t.ev.desc })).sort((a, b) => a.ts - b.ts);
  const edits = touches.filter(t => t.write).length, agents = new Map();
  for (const t of touches) { const o = agents.get(t.agent) || { e: 0, r: 0, last: 0 }; t.write ? o.e++ : o.r++; o.last = Math.max(o.last, t.ts); agents.set(t.agent, o); }
  const head = `<div class="pp mono">${esc(file.label)}</div>`;
  const evPart = `<h4>Touches <span class="sub">${edits} edits, ${touches.length - edits} reads in the log${touches.length ? `, first ${dayOf(touches[0].ts)} ${clock(touches[0].ts)}` : ""}</span></h4>${paneTimeline(touches)}`
    + `<table class="ptab"><tbody>${[...agents].sort((a, b) => b[1].last - a[1].last).map(([n, o]) => `<tr><td>${paneAgentLink(n)}</td><td class="r">${o.e} edits</td><td class="r">${o.r} reads</td><td class="r sub">${dur(nowS() - o.last)} ago</td></tr>`).join("")}</tbody></table>`
    + `<h4>Latest</h4><ul class="plist">${touches.slice(-25).reverse().map(t => `<li><span class="sub">${clock(t.ts)}</span> ${paneAgentLink(t.agent)} <span class="pk ${t.write ? "e" : "r"}">${t.write ? "edit" : "read"}</span> <span class="sub">${esc(t.tool)}</span>${t.desc || (t.target && t.target !== abs && !abs.endsWith("/" + t.target)) ? `<div class="sub ell" title="${esc((t.desc || t.target).slice(0, 500))}">${esc((t.desc || t.target).slice(0, 200))}</div>` : ""}</li>`).join("")}</ul>`;
  paneShow(`<span class="pk f">file</span> ${esc(abs.split("/").pop())}`, head + `<div id="pfinfo" class="sub">looking the file up…</div>` + evPart);
  let info;
  try { info = !file.local ? { available: false, reason: "file contents are available only for files on this server" } : abs.startsWith("/") ? await api("/fileinfo?path=" + encodeURIComponent(abs)) : { available: false, reason: "a relative path whose working directory isn't known" }; }
  catch (e) { info = { available: false, reason: String(e.message || e) }; console.error(e); }
  if (seq !== pane.seq) return;
  const el = $("pfinfo"); if (!el) return;
  if (!info.available) { el.innerHTML = `<span class="note">No file details: ${esc(info.reason || "unavailable")}.</span>`; return; }
  const lines = info.text != null ? info.text.split("\n") : null;
  if (lines && lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
  el.outerHTML = `<dl class="pdl"><dt>size</dt><dd>${kb(info.size)} <span class="sub">(${Number(info.size).toLocaleString()} bytes)</span></dd><dt>type</dt><dd>${esc(info.type)}</dd>`
    + `<dt>modified</dt><dd>${dayOf(info.mtime)} ${clock(info.mtime, true)} <span class="sub">(${dur(nowS() - info.mtime)} ago)</span></dd>`
    + (info.resolved && info.resolved !== abs ? `<dt>resolves to</dt><dd class="mono">${esc(info.resolved)}</dd>` : "") + `</dl>`
    + (lines ? `<h4>Text <span class="sub">${lines.length} lines</span></h4><pre class="pcode">${lines.map(l => `<span>${esc(l)}</span>`).join("")}</pre>`
      : `<p class="sub">${info.binary ? "Binary file: no text shown." : `Larger than the 64 kB text limit: no text shown.`}</p>`);
}
const paneAgentLink = n => n === "human" ? `<span class="mono" style="color:var(--human)">human</span>`
  : `<a class="name" style="--c:${colorOf(n)}" href="${esc(agentHref(n))}">${esc(n)}</a>`;
async function paneAgent(name) {
  const seq = ++pane.seq; pane.kind = "agent"; pane.key = name;
  const a = agentsNow.find(x => x.name === name), tools = (M?.lanes.get(name)?.tools || []).slice(-25).reverse();
  const kv = (k, v) => v == null || v === "" ? "" : `<dt>${k}</dt><dd>${v}</dd>`;
  const info = a ? `<dl class="pdl">${kv("state", `<span class="flag ${esc(a.state)}">${esc(a.state)}</span>${a.reachable && a.reachable !== "yes" ? ` <span class="flag off" title="${esc(a.reachable_why || "")}">${esc(a.reachable)}</span>` : ""}${a.last_active ? ` <span class="sub">active ${dur(nowS() - a.last_active)} ago</span>` : ""}`)}`
    + kv("task", a.about ? esc(a.about) : `<span class="sub">no task set</span>`)
    + kv("harness", esc(a.kind || "?") + (a.model ? ` · ${esc(a.model)}` : "") + (a.effort ? ` · ${esc(a.effort)}` : ""))
    + kv("context", a.context && a.window ? `${Math.round(a.context / 1000)}k of ${Math.round(a.window / 1000)}k (${Math.round(a.context / a.window * 100)}%)` : "")
    + kv("cwd", a.cwd ? `<span class="mono">${esc(a.cwd)}</span>${a.branch ? ` <span class="sub">(${esc(a.branch)})</span>` : ""}` : "")
    + kv("host", esc(a.host || "")) + kv("tmux", a.tmux_session ? `<span class="mono">${esc(a.tmux_session)}</span>` : "")
    + kv("projects", Object.keys(a.projects || {}).map(p => `<a class="proj" style="--c:${colorOf(p)}" href="${esc(projHref(p))}">${esc(p)}</a>`).join(" "))
    + `</dl>` : `<p class="sub">Not on the bus right now (only in the event log).</p>`;
  const toolHtml = `<h4>Recent tool calls <span class="sub">${tools.length ? "newest first" : "none in the log"}</span></h4><ul class="plist">${tools.map(t => `<li><span class="sub">${clock(t.ts)}</span> <span class="pk ${t.kind === "edit" ? "e" : t.kind === "read" ? "r" : "s"}">${esc(t.tool)}</span><div class="sub ell" title="${esc((t.desc || t.target).slice(0, 600))}">${esc((t.desc || t.target).slice(0, 220))}</div></li>`).join("")}</ul>`;
  paneShow(`<span class="pk a">agent</span> ${esc(name)} <a class="btn" href="${esc(agentHref(name))}" title="open ${esc(name)} in the console">console &rarr;</a>`,
    info + `<div id="pmsgs"><h4>Messages</h4><p class="sub">loading…</p></div>` + toolHtml);
  let msgs = [], note = "";
  try { msgs = (await api(`/log?agent=${encodeURIComponent(name)}&limit=30`)).map(m => ({ ...m, reach: m.reach || [], to: m.to || [], text: m.text || "" })); }
  catch (e) { note = `couldn't read /log: ${esc(e.message || e)}`; console.error(e); }
  if (seq !== pane.seq) return;
  const el = $("pmsgs"); if (!el) return;
  el.innerHTML = `<h4>Messages <span class="sub">${msgs.length ? "last " + msgs.length + ", newest first" : "none"}${note ? " · " + note : ""}</span></h4><ul class="plist">`
    + msgs.slice().reverse().map(m => { const out = m.from === name;
      return `<li><span class="sub">${dayOf(m.ts) === dayOf(nowS()) ? clock(m.ts) : dayOf(m.ts) + " " + clock(m.ts)}</span> ${out ? `<span class="pk o">to</span> ${(m.reach.length ? m.reach : m.to.map(t => t.name)).slice(0, 6).map(r => r.startsWith("/") ? esc(r) : paneAgentLink(r)).join(", ")}` : `<span class="pk i">from</span> ${paneAgentLink(m.from)}`}<div class="pmsg">${esc(m.text.slice(0, 600))}</div></li>`; }).join("") + `</ul>`;
}
function paneOpen(kind, key) {
  (kind === "file" ? paneFile(key) : paneAgent(key)).catch(e => { console.error(e); paneShow(esc(key), `<p class="note">${esc(e.message || e)}</p>`); });
}
{
  $("pclose").onclick = paneClose;
  // drag the left edge to resize; the width is remembered
  const grip = $("pgrip");
  grip.addEventListener("pointerdown", ev => { grip.setPointerCapture(ev.pointerId); grip.dataset.on = "1"; ev.preventDefault(); });
  grip.addEventListener("pointermove", ev => { if (grip.dataset.on) { paneSetWidth(innerWidth - ev.clientX); paneResized(); } });
  grip.addEventListener("pointerup", () => { delete grip.dataset.on; setPref("an.panew", String(parseInt(getComputedStyle(document.documentElement).getPropertyValue("--pw")))); paneResized(); });
  document.addEventListener("keydown", ev => { if (ev.key === "Escape" && !$("pane").hidden) paneClose(); });
}

