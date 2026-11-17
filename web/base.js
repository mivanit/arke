// Shared by the console and both analytics pages, loaded in <head> right after auth.js: preferences and the theme,
// small helpers, colors and the connection light. A classic script, so these names are globals.
"use strict";
// Preferences live in localStorage ("ab.<key>"). Only ?theme= on the URL sets one (and remembers it): a link from
// anywhere must not be able to change the others (hidden agents, dismissed questions, ...).
const pref = (k, d) => {
  const u = k === "theme" ? new URLSearchParams(location.search).get(k) : null;
  try { if (u != null) localStorage.setItem("ab." + k, u); return localStorage.getItem("ab." + k) ?? d; }
  catch { return u ?? d; }
};
const setPref = (k, v) => { try { localStorage.setItem("ab." + k, v); } catch (e) { console.warn("can't save preference", k, e); } };
const darkMq = matchMedia("(prefers-color-scheme: dark)");
function applyTheme() {
  const t = pref("theme", "system");
  document.documentElement.dataset.theme = t === "system" ? (darkMq.matches ? "dark" : "light") : t;
  document.documentElement.classList.toggle("hack", pref("an.hack", "0") === "1");  // analytics' easter egg
}
darkMq.addEventListener("change", applyTheme);
applyTheme();

const $ = id => document.getElementById(id);
const esc = s => s == null ? "" : String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const under = (p, q) => p === q || p.startsWith(q + "/");
const BUS_LABEL = location.host;  // every request goes to the server that served the page

// colors: every agent/project has one. The default is a hue picked by hashing the name (same on every device);
// a server override ("color") wins.
const hsl = (h, s = 0.45, l = 0.5) => {  // -> "#rrggbb" (standard HSL formula, so <input type=color> can show it)
  const a = s * Math.min(l, 1 - l), f = n => { const k = (n + h / 30) % 12; return Math.round((l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))) * 255).toString(16).padStart(2, "0"); };
  return "#" + f(0) + f(8) + f(4);
};
const fnv = s => { let h = 2166136261; for (const ch of s) h = Math.imul(h ^ ch.codePointAt(0), 16777619); return h >>> 0; };  // FNV-1a
// hue 25..325 (no reds, which read as errors), from the name's hash: few collisions, same on every device
// plus one of three lightness levels from other hash bits, so names with close hues still look different
// known names (projects + agents, sorted) get golden-ratio-spaced defaults so no two collide; deterministic for a given set.
// Names the bus doesn't know (yet) fall back to the hash.
let defMap = new Map();
function buildDefaults(known) {
  const names = [...known].sort(), g = (Math.sqrt(5) - 1) / 2, used = new Set();
  defMap = new Map(names.map((n, i) => {
    let h = 25 + 300 * ((0.13 + i * g) % 1), c;  // golden-angle steps through hues 25..325 (no reds)
    do { c = hsl(h, [0.55, 0.42, 0.68][i % 3], [0.45, 0.56, 0.38, 0.62][i % 4]); h = 25 + (h - 25 + 3) % 300; } while (used.has(c));
    used.add(c); return [n, c];
  }));
}
const defColor = n => n === "human" ? "var(--human)" : n === "*" ? "var(--off)"
  : defMap.get(n) || hsl(25 + fnv(n) % 300, 0.5, [0.4, 0.5, 0.6][(fnv(n) >>> 12) % 3]);

// connection light: every request to the bus counts. Green once one gets an answer, amber while requests fail (no
// answer, or none within 10 s), red once they have failed for 10 s; the tooltip says when the last answer came.
// Key errors (a locked page waiting for its key) aren't network failures. The tooltip goes in #netstat's data-tip if it has one, else title.
const net = { ok: 0, fails: 0, since: 0, err: "" };
function netShow() {
  const el = $("netstat"), st = net.fails && Date.now() - net.since >= 10000 ? "down" : net.fails ? "retry" : net.ok ? "up" : "wait";
  el.className = "netstat " + st;
  // how old what's on screen is: the last answer from the bus (the agents' own state is as of their last hook)
  const age = Math.round((Date.now() - net.ok) / 1000), ago = net.ok ? age < 60 ? `${age}s ago` : `${Math.round(age / 60)}m ago` : "none";
  el.textContent = { up: `updated ${ago}`, retry: `reconnecting · data ${ago}`, down: `disconnected · data ${ago}`, wait: "connecting" }[st];
  const tip = (net.ok ? `last answer ${age}s ago` : "no answer yet") + (net.fails ? `\n${net.fails} failed in a row: ${net.err}` : "");
  el.setAttribute(el.hasAttribute("data-tip") ? "data-tip" : "title", tip);
}
addEventListener("DOMContentLoaded", () => setInterval(netShow, 1000));
async function netFetch(url, opts) {
  try {
    const r = await ArkeAuth.fetch(url, opts);
    net.ok = Date.now(); net.fails = 0; netShow();
    return r;
  } catch (e) {
    if (!e.key) { if (!net.fails++) net.since = Date.now(); net.err = e.message; }
    netShow(); throw e;
  }
}
