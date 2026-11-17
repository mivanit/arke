"use strict";

// Graph layouts shared by the 2D and 3D analytics pages.
// A node is {name, px, py, pinned, r}, an edge {na, nb, w, ab, ba} (ab: messages na -> nb, ba: back); the box is W x H, y down.
// Iterative layouts move nodes a step at a time (layoutStep) and cool down until they settle; the others compute
// target positions at once (layoutTargets), which the 2D graph animates towards. Pinned nodes never move.
const LAYOUTS = [["fr", "springs (Fruchterman-Reingold)"], ["force", "force (springs + charge)"], ["stress", "stress (Kamada-Kawai-like)"],
  ["circle", "circle"], ["layers", "layers (senders above receivers)"]];
const ITERATIVE = new Set(["fr", "force"]);
const labelHalf = a => Math.max(a.r, a.name.length * 3.6) + 6;  // keep labels inside the box too
function clampNode(a, W, H) { const lw = labelHalf(a); a.px = Math.max(lw, Math.min(W - lw, a.px)); a.py = Math.max(a.r + 6, Math.min(H - a.r - 18, a.py)); }
const maxWeight = edges => Math.max(1, ...edges.map(e => e.w));
// one step of an iterative layout at temperature `temp` (max move in px); returns the largest move made.
function layoutStep(kind, nodes, edges, W, H, temp, held) {
  const n = nodes.length, maxW = maxWeight(edges);
  for (const a of nodes) { a.dx = 0; a.dy = 0; }
  if (kind === "force") {
    // linear springs with a rest length that shrinks with message volume, inverse-distance charge, gentle centering
    const L0 = Math.min(W, H) * 0.24;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = nodes[i], b = nodes[j], dx = a.px - b.px, dy = a.py - b.py, d = Math.max(1, Math.hypot(dx, dy));
      let f = L0 * L0 * 0.5 / d; const sep = a.r + b.r + 18; if (d < sep) f += (sep - d) * 2;
      a.dx += dx / d * f; a.dy += dy / d * f; b.dx -= dx / d * f; b.dy -= dy / d * f;
    }
    for (const e of edges) {
      const a = e.na, b = e.nb, dx = b.px - a.px, dy = b.py - a.py, d = Math.max(1, Math.hypot(dx, dy)), s = Math.sqrt(e.w / maxW);
      const f = (d - L0 * (1.2 - 0.8 * s)) * (0.3 + 0.7 * s);
      a.dx += dx / d * f; a.dy += dy / d * f; b.dx -= dx / d * f; b.dy -= dy / d * f;
    }
    for (const a of nodes) { a.dx -= (a.px - W / 2) * 0.06; a.dy -= (a.py - H / 2) * 0.06 * W / H; }
  } else {
    // Fruchterman-Reingold: k^2/d repulsion between every pair, d^2/k attraction along edges (scaled by volume)
    const k = Math.sqrt(W * H / Math.max(1, n)) * 0.5;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
      const a = nodes[i], b = nodes[j], dx = a.px - b.px, dy = a.py - b.py, d = Math.max(0.5, Math.hypot(dx, dy));
      let f = k * k / d; const sep = a.r + b.r + 18; if (d < sep) f += (sep - d) * 3;
      a.dx += dx / d * f; a.dy += dy / d * f; b.dx -= dx / d * f; b.dy -= dy / d * f;
    }
    for (const e of edges) {
      const a = e.na, b = e.nb, dx = a.px - b.px, dy = a.py - b.py, d = Math.max(0.5, Math.hypot(dx, dy)), f = d * d / k * (0.08 + 1.4 * e.w / maxW);
      a.dx -= dx / d * f; a.dy -= dy / d * f; b.dx += dx / d * f; b.dy += dy / d * f;
    }
    // centering on an ellipse that fits the box: a pull of 1.1 k at its rim, so leaves don't pile up on the edges
    for (const a of nodes) { a.dx -= 1.1 * k * (a.px - W / 2) / (W * 0.42); a.dy -= 1.1 * k * (a.py - H / 2) / (H * 0.42); }
  }
  let moved = 0;
  for (const a of nodes) {
    if (a.pinned || a === held) continue;
    const dl = Math.hypot(a.dx, a.dy), step = Math.min(dl, temp);
    if (dl > 0) { a.px += a.dx / dl * step; a.py += a.dy / dl * step; moved = Math.max(moved, step); }
    clampNode(a, W, H);
  }
  return moved;
}
// positions for the non-iterative layouts: Map name -> [x, y] for every unpinned node
function layoutTargets(kind, nodes, edges, W, H) {
  const out = new Map(), free = nodes.filter(a => !a.pinned), idx = new Map(nodes.map((a, i) => [a.name, i])), n = nodes.length, maxW = maxWeight(edges);
  if (!free.length) return out;
  const nbr = nodes.map(() => new Map());
  for (const e of edges) { const i = idx.get(e.na.name), j = idx.get(e.nb.name); nbr[i].set(j, (nbr[i].get(j) || 0) + e.w); nbr[j].set(i, (nbr[j].get(i) || 0) + e.w); }
  if (kind === "circle") {
    // greedy order around the ring: start at the busiest node, then always the free node most connected to the last one placed
    const left = new Set(free.map(a => idx.get(a.name))), order = [];
    const deg = i => [...nbr[i].values()].reduce((s, w) => s + w, 0);
    let cur = [...left].sort((a, b) => deg(b) - deg(a))[0];
    while (left.size) {
      left.delete(cur); order.push(cur);
      let best = -1, bw = -1;
      for (const j of left) { const w = (nbr[cur].get(j) || 0) * 1000 + deg(j); if (w > bw) { bw = w; best = j; } }
      cur = best;
    }
    order.forEach((i, k) => { const t = -Math.PI / 2 + 2 * Math.PI * k / order.length; out.set(nodes[i].name, [W / 2 + W * 0.4 * Math.cos(t), H / 2 + H * 0.38 * Math.sin(t)]); });
  } else if (kind === "layers") {
    // direction from net flow (more messages a -> b than back: a above b); cycles broken by a DFS from the biggest net senders;
    // rank = longest path from a source; order within a layer by the mean position of neighbors (a few barycenter sweeps)
    const out_ = nodes.map(() => []), net = nodes.map(() => 0);
    for (const e of edges) {
      const i = idx.get(e.na.name), j = idx.get(e.nb.name), d = e.ab - e.ba;
      if (d > 0) { out_[i].push(j); net[i] += d; net[j] -= d; } else if (d < 0) { out_[j].push(i); net[j] -= d; net[i] += d; }
    }
    const state = nodes.map(() => 0), dag = nodes.map(() => []);
    const visit = i => { state[i] = 1; for (const j of out_[i]) { if (state[j] === 1) continue; dag[i].push(j); if (!state[j]) visit(j); } state[i] = 2; };
    for (const i of nodes.map((_, i) => i).sort((a, b) => net[b] - net[a])) if (!state[i]) visit(i);
    const rank = nodes.map(() => 0);
    for (let it = 0; it < n; it++) { let ch = false; for (let i = 0; i < n; i++) for (const j of dag[i]) if (rank[j] < rank[i] + 1) { rank[j] = rank[i] + 1; ch = true; } if (!ch) break; }
    const iso = nodes.map((_, i) => nbr[i].size === 0), maxR = Math.max(0, ...rank.filter((_, i) => !iso[i]));
    rank.forEach((r, i) => { if (iso[i]) rank[i] = maxR + 1; });
    const nl = Math.max(...rank) + 1, layers = Array.from({ length: nl }, () => []);
    rank.forEach((r, i) => layers[r].push(i));
    const pos = new Array(n).fill(0);
    layers.forEach(L => L.forEach((i, k) => pos[i] = (k + 1) / (L.length + 1)));
    for (let sweep = 0; sweep < 6; sweep++) for (const L of (sweep % 2 ? [...layers].reverse() : layers)) {
      const bc = i => { let s = 0, c = 0; for (const [j, w] of nbr[i]) if (rank[j] !== rank[i]) { s += pos[j] * w; c += w; } return c ? s / c : pos[i]; };
      L.sort((a, b) => bc(a) - bc(b)); L.forEach((i, k) => pos[i] = (k + 1) / (L.length + 1));
    }
    for (const a of free) { const i = idx.get(a.name); out.set(a.name, [W * (0.06 + 0.88 * pos[i]), H * (nl === 1 ? 0.5 : 0.08 + 0.8 * rank[i] / (nl - 1))]); }
  } else {
    // stress majorization: graph distances (busier edges are shorter) become target distances on the page
    const L0 = Math.min(W, H) * 0.2, D = nodes.map(() => new Array(n).fill(Infinity));
    for (let i = 0; i < n; i++) { D[i][i] = 0; for (const [j, w] of nbr[i]) D[i][j] = Math.min(D[i][j], L0 * (1.4 - 0.9 * Math.sqrt(w / maxW))); }
    for (let k = 0; k < n; k++) for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) if (D[i][k] + D[k][j] < D[i][j]) D[i][j] = D[i][k] + D[k][j];
    let far = L0;
    for (const row of D) for (const d of row) if (isFinite(d)) far = Math.max(far, d);
    far += L0;
    for (const row of D) for (let j = 0; j < n; j++) if (!isFinite(row[j])) row[j] = far;
    const X = nodes.map(a => a.px), Y = nodes.map(a => a.py);
    for (let it = 0; it < 300; it++) for (let i = 0; i < n; i++) {
      if (nodes[i].pinned) continue;
      let sx = 0, sy = 0, sw = 0;
      for (let j = 0; j < n; j++) {
        if (i === j) continue;
        const d = D[i][j], w = 1 / (d * d), dx = X[i] - X[j], dy = Y[i] - Y[j], dist = Math.max(1e-3, Math.hypot(dx, dy));
        sx += w * (X[j] + d * dx / dist); sy += w * (Y[j] + d * dy / dist); sw += w;
      }
      if (sw) { X[i] = sx / sw; Y[i] = sy / sw; } // a lone node has no pairwise weights
    }
    // fit into the box (only when nothing is pinned: pinned nodes fix the frame)
    if (!nodes.some(a => a.pinned)) {
      const x0 = Math.min(...X), x1 = Math.max(...X), y0 = Math.min(...Y), y1 = Math.max(...Y);
      const s = Math.min(4, W * 0.84 / Math.max(1, x1 - x0), H * 0.8 / Math.max(1, y1 - y0));
      for (let i = 0; i < n; i++) { X[i] = W / 2 + (X[i] - (x0 + x1) / 2) * s; Y[i] = H / 2 + (Y[i] - (y0 + y1) / 2) * s; }
    }
    for (const a of free) { const i = idx.get(a.name); out.set(a.name, [X[i], Y[i]]); }
  }
  for (const [name, p] of out) { const a = nodes[idx.get(name)], q = { ...a, px: p[0], py: p[1] }; clampNode(q, W, H); p[0] = q.px; p[1] = q.py; }
  return out;
}
