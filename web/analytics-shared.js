// ---------- tool kinds and file paths ----------
const KIND = t => /^(Edit|Write|MultiEdit|NotebookEdit|apply_patch|write_file|edit_file|str_replace)/i.test(t) ? "edit"
  : /^(Read|Grep|Glob|LS|view|read_file|list_dir|search|WebFetch|WebSearch)/i.test(t) ? "read"
  : /^(Bash|exec|exec_command|shell|local_shell|BashOutput|unified_exec)/i.test(t) ? "shell" : "other";
const EXT = "py|pyi|md|ts|tsx|js|mjs|cjs|json|jsonl|html|css|sh|bash|zsh|txt|tsv|csv|toml|yaml|yml|cfg|ini|rs|go|c|h|cc|cpp|hpp|java|lua|nix|sql|ipynb|lock|tex|rb|conf|xml|svg|log|patch|diff|pt|npz|parquet|el|vim";
// a path-ish token ending in a known extension, bounded by start/space/quote/shell punctuation on both sides
const PATH_RE = new RegExp(`(?:^|[\\s'"=(<>|;&,\`])((?:~|\\.{1,2})?/?(?:[\\w.@+-]+/)*[\\w@+-][\\w.@+-]*\\.(?:${EXT}))(?=$|[\\s'"\`:;,)|&<>\\]])`, "g");
// shell segments whose file arguments are written: in-place editors and file movers
const WRITE_CMD = /\b(sed\s+(-\w+\s+)*-i|perl\s+-\w*i|truncate|touch|rm|mv|cp|patch|git\s+(checkout|restore|apply))\b/;
// absolute, normalized path: relative targets resolve against the event's cwd; "." and ".." segments folded
function absPath(p, cwd) {
  if (p.startsWith("~")) return p;
  const full = p.startsWith("/") ? p : cwd ? cwd + "/" + p : p;
  const out = [];
  for (const s of full.split("/")) { if (s === "" || s === ".") continue; if (s === "..") out.pop(); else out.push(s); }
  return (full.startsWith("/") ? "/" : "") + out.join("/");
}
const fileLabel = f => (f.host ? f.host + ":" : "") + f.path;
// File identity includes the machine and normalized path; labels never stand in for identity.
function filesOf(e) {
  const k = KIND(e.tool || ""), tgt = String(e.target || ""), cwd = e.cwd || "";
  const file = (p, write) => { const path = absPath(p, cwd), host = e.host || ""; return { path, host, key: JSON.stringify([host, path]), write }; };
  const pathTool = /^(Read|Edit|Write|MultiEdit|NotebookEdit|read_file|write_file|edit_file|str_replace)$/i.test(e.tool || "");
  // only path tools name a file: Grep / Glob patterns, URLs and searches are not paths
  if (pathTool && tgt) return [file(tgt, k === "edit")];
  if (k !== "shell" && k !== "edit") return [];
  const out = new Map();
  for (const seg of tgt.split(/\n|;|&&|\|\|/)) {
    const segWrites = WRITE_CMD.test(seg);
    const redirected = new Set([...seg.matchAll(/(?:>>?|\btee\s+(?:-a\s+)?)\s*['"]?([^\s'";&|<>]+)/g)].map(m => m[1]));
    for (const m of seg.matchAll(PATH_RE)) {
      const raw = m[1];
      if (/^\d/.test(raw.split("/").pop())) continue;  // 0.71.py-ish numbers
      const f = file(raw, k === "edit" || segWrites || redirected.has(raw));
      out.set(f.key, { ...f, write: f.write || out.get(f.key)?.write || false });
    }
    if (out.size > 16) break;
  }
  return [...out.values()];
}

// Only files on the bus host can be read by its file endpoints. Older events may omit host.
const isLocalFile = (f, agents) => !f.host || agents.some(a => a.local && a.host === f.host);

// ---------- the event model shared by analytics.html and analytics3d.html (pure: the page passes its state in) ----------
// Resolve names at each event, then follow that owner's later renames. A spelling can be reused by
// another session: a global old-name -> new-name map would steal all of that new session's history.
function eventNames(events) {
  const current = new Map(), sessions = new Map(), refs = new Map();
  const root = o => { if (o.parent) o.parent = root(o.parent); return o.parent || o; };
  const owner = (name, sid = "") => {
    let o = (sid && sessions.get(sid)) || current.get(name);
    o = o ? root(o) : { name };
    if (!current.has(name)) current.set(name, o);
    if (sid) sessions.set(sid, o);
    return o;
  };
  for (const e of events) {
    if (e.event === "rename" && e.old && e.agent) {
      const o = owner(e.old, e.sid);
      if (current.has(e.old) && root(current.get(e.old)) === o) current.delete(e.old);
      o.name = e.agent; current.set(e.agent, o);
    } else if (e.event === "takeover" && e.agent) {
      const name = e.new_name || e.agent, old = owner(e.agent, e.old_sid), next = owner(name, e.new_sid);
      if (old !== next) old.parent = next;  // replacement sessions inherit the retired agent's history
      if (e.agent !== name) current.delete(e.agent);
      next.name = name; current.set(name, next);
    }
    const at = new Map();
    const remember = (name, sid = "") => {
      if (typeof name === "string" && name !== "human" && name !== "*" && !name.startsWith("/")) at.set(name, owner(name, sid));
    };
    remember(e.agent, e.sid); remember(e.from, e.sid); remember(e.sender);
    for (const n of e.reach || []) remember(n);
    if (typeof e.to === "string") remember(e.to);
    else for (const t of e.to || []) if (t.kind === "agent" || !t.kind) remember(t.name);
    for (const n of Object.keys(e.agents || {})) remember(n);
    for (const p of [...(e.started || []), ...(e.ended || [])]) remember(p.agent);
    refs.set(e, at);
    if (e.event === "forget") current.delete(e.agent);
  }
  const nameAt = (e, name) => { const o = refs.get(e)?.get(name); return o ? root(o).name : name; };
  nameAt.ofSid = sid => { const o = sessions.get(sid); return o ? root(o).name : null; };  // a session's current name
  return nameAt;
}
// A takeover can merge owners that shared a sample: keep both their usage and their process lists.
function processAgents(e, nameAt) {
  const agents = Object.create(null);
  for (const [name, v] of Object.entries(e.agents || {})) {
    const n = nameAt(e, name), a = agents[n] ||= { cpu: 0, rss_mb: 0, n: 0, top: [] };
    a.cpu += Number(v.cpu) || 0;
    a.rss_mb += Number(v.rss_mb) || 0;
    a.n += Number(v.n) || 0;
    a.top.push(...(v.top || []));
  }
  return agents;
}
// The page's model from the raw event log (both analytics pages; 3D asks for fewer event types, so some lists stay empty).
function buildModel(raw, agentsNow, now) {
  const nameAt = eventNames(raw);
  const lanes = new Map(), lane = n => {
    if (!lanes.has(n)) lanes.set(n, { name: n, trans: [], tools: [], prompts: [], wakes: [], sent: [], recv: [], projects: new Set(), first: Infinity, last: -Infinity, kind: "" });
    return lanes.get(n);
  };
  const seen = (L, ts) => { if (ts < L.first) L.first = ts; if (ts > L.last) L.last = ts; };
  const msgs = [], touches = [], usage = [], procs = [];
  let t0 = Infinity, t1 = -Infinity;
  for (const e of raw) {
    const C = n => nameAt(e, n);
    const ts = e.ts; if (ts < t0) t0 = ts; if (ts > t1) t1 = ts;
    switch (e.event) {
      case "state": case "update": {
        if (!e.state || !e.agent) break;
        const L = lane(C(e.agent)), last = L.trans[L.trans.length - 1];
        if (!last || last[1] !== e.state) L.trans.push([ts, e.state]);
        seen(L, ts); break;
      }
      case "join": { const L = lane(C(e.agent)); L.kind = e.kind || L.kind; for (const p of e.projects || []) L.projects.add(p); seen(L, ts); break; }
      case "forget": { const L = lane(C(e.agent)); L.trans.push([ts, "offline"]); break; }
      case "tool": {
        const L = lane(C(e.agent)), ev = { ts, agent: L.name, host: e.host || "", tool: e.tool || "?", kind: KIND(e.tool || ""), target: String(e.target || ""), desc: e.desc || "", cwd: e.cwd || "" };
        for (const p of e.projects || []) L.projects.add(p);
        ev.files = filesOf(e);
        for (const f of ev.files) touches.push({ ts, agent: L.name, ...f, ev });
        L.tools.push(ev); seen(L, ts); break;
      }
      case "prompt": { const L = lane(C(e.agent)); L.prompts.push({ ts, wake: !!e.wake }); seen(L, ts); break; }
      case "wake": { if (!e.agent) break; const L = lane(C(e.agent)); L.wakes.push({ ts, sender: C(e.sender || "?"), ok: e.ok !== false, error: e.error || "" }); break; }
      case "usage": { if (e.agent) usage.push({ ts, agent: C(e.agent), sid: e.sid || e.agent, src: e.source || "", tokens: Number(e.tokens) || 0, context: Number(e.context) || 0, window: Number(e.window) || 0, model: e.model || "" }); break; }
      case "procs": {
        const ag = Object.create(null);
        for (const [n, v] of Object.entries(processAgents(e, nameAt))) ag[n] = { cpu: v.cpu, rss: v.rss_mb };
        procs.push({ ts, agents: ag }); break;
      }
      case "message": {
        const from = C(e.from || "?");
        // old log lines: to is a plain name; new: [{name, kind}] plus reach (who actually got it)
        const to = typeof e.to === "string" ? [{ name: e.to, kind: e.to.startsWith("/") ? "project" : e.to === "human" ? "human" : e.to === "*" ? "broadcast" : "agent" }] : (e.to || []);
        let reach = (e.reach || []).map(C);
        if (!reach.length) reach = to.filter(t => t.kind === "agent" || t.kind === "human" || (!t.kind && !t.name.startsWith("/"))).map(t => C(t.name));
        const m = { ts, from, to: to.map(t => t.kind === "agent" || !t.kind ? { ...t, name: C(t.name) } : t),
          reach: [...new Set(reach)].filter(r => r !== from), text: e.text || "", id: e.id };
        msgs.push(m);
        lane(m.from).sent.push(m); seen(lane(m.from), ts);
        for (const r of m.reach) { lane(r).recv.push(m); seen(lane(r), ts); }
        for (const p of e.projects || []) if (m.from !== "human") lane(m.from).projects.add(p);
        break;
      }
    }
  }
  for (const a of agentsNow) { const L = lanes.get(a.name); if (L) { for (const p of Object.keys(a.projects || {})) L.projects.add(p); L.kind = a.kind || L.kind; } }
  const firstTool = Math.min(...[...lanes.values()].map(L => L.tools.length ? L.tools[0].ts : Infinity));
  // tokens out: positive steps of each session's running total (a drop is a reset: skipped; a session's first sample has no baseline).
  // Per session AND source: the hook and the server's transcript reader count differently, so interleaving them would fake huge steps.
  usage.sort((a, b) => a.ts - b.ts);
  const prevTok = new Map(), big = new Set();
  for (const u of usage) {
    const k = u.sid + "\u0000" + u.src, p = prevTok.get(k); u.dtok = p != null && u.tokens > p ? u.tokens - p : 0; prevTok.set(k, u.tokens);
    // Claude transcripts don't record the window: same guess as server.py (1M if the model says so or the session went past 200k)
    if (u.context > 200000) big.add(u.sid);
    u.win = u.window || (u.context ? (u.model.includes("[1m]") || big.has(u.sid) ? 1e6 : 2e5) : 0);
  }
  procs.sort((a, b) => a.ts - b.ts);
  return { lanes, msgs, touches, usage, procs, firstTool, ofSid: nameAt.ofSid, t0: isFinite(t0) ? t0 : now - 3600, t1: Math.max(isFinite(t1) ? t1 : 0, now) };
}
// busy spans [a, b] clipped to [w0, w1]
function busySpans(L, w0, w1, agentsNow, now) {
  const out = [];
  for (let i = 0; i < L.trans.length; i++) {
    const [ts, s] = L.trans[i];
    if (s !== "busy") continue;
    const end = i + 1 < L.trans.length ? L.trans[i + 1][0] : (agentsNow.find(a => a.name === L.name)?.state === "busy" ? now : L.last);
    if (end < w0 || ts > w1) continue;
    out.push([Math.max(ts, w0), Math.min(end, w1)]);
  }
  return out;
}

