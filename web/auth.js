// Optional shared key, remembered for this tab. No cookies or automatic write retries.
const ArkeAuth = (() => {
  let key = "", locked = false, generation = 0, dialog;
  try { key = sessionStorage.getItem("arke.key") || ""; } catch { /* memory-only when storage is disabled */ }
  function remember(value) {
    key = value;
    try { if (key) sessionStorage.setItem("arke.key", key); else sessionStorage.removeItem("arke.key"); } catch { /* memory-only */ }
  }
  function lock() {
    locked = true; generation++; remember("");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.style.cssText = "max-width:30rem;padding:1.5rem;border:1px solid #888;border-radius:8px;color:CanvasText;background:Canvas";
      dialog.innerHTML = `<form><h2>Arke key required</h2>
        <p>This server has experimental access control enabled. It comes with no guarantees.</p>
        <label>Shared key <input type="password" required autocomplete="off" spellcheck="false" style="width:100%;box-sizing:border-box"></label>
        <p role="alert"></p><button>Unlock this tab</button></form>`;
      dialog.addEventListener("cancel", e => e.preventDefault());
      dialog.querySelector("form").onsubmit = async e => {
        e.preventDefault();
        const input = dialog.querySelector("input"), button = dialog.querySelector("button"), error = dialog.querySelector("[role=alert]");
        button.disabled = true; error.textContent = "";
        try {
          const candidate = input.value;
          const r = await fetch("/agents", { headers: { Authorization: "Bearer " + candidate },
            mode: "same-origin", credentials: "omit", redirect: "error", signal: AbortSignal.timeout(10000) });
          await r.arrayBuffer();
          if (!r.ok) throw new Error(r.status === 401 ? "Incorrect key." : `Server answered ${r.status}.`);
          remember(candidate); generation++; locked = false; input.value = ""; dialog.close();
          window.dispatchEvent(new Event("arke-unlocked"));
        } catch (e) { error.textContent = e.message; }
        finally { button.disabled = false; }
      };
      document.body.append(dialog);
    }
    dialog.showModal();
  }
  // errors about the key, not the network: the page waits for the key, or it changed under a request (the connection light ignores these)
  const keyError = msg => Object.assign(new Error(msg), { key: true });
  async function request(url, opts = {}) {
    if (locked) {
      if (!dialog.open) dialog.showModal();  // a second Escape closes it without a cancel event
      throw keyError("Enter the Arke key to continue.");
    }
    const started = generation, headers = new Headers(opts.headers);
    if (key) headers.set("Authorization", "Bearer " + key);
    // Keep the existing limits: 10 s for headers, 120 s for large analytics bodies.
    const ac = new AbortController(), stop = msg => () => ac.abort(new DOMException(msg, "TimeoutError"));
    let timer = setTimeout(stop("no answer within 10 s"), 10000);
    try {
      const r = await fetch(url, { ...opts, headers, signal: ac.signal, mode: "same-origin", credentials: "omit", redirect: "error" });
      if (started !== generation) throw keyError("Key changed; discard this response.");
      if (r.status === 401) { lock(); throw keyError("Arke key required or incorrect. This request was rejected."); }
      clearTimeout(timer); timer = setTimeout(stop("download stalled (over 120 s)"), 120000);
      const body = await r.arrayBuffer();
      if (started !== generation) throw keyError("Key changed; discard this response.");
      return new Response(body, { status: r.status, statusText: r.statusText, headers: r.headers });
    } finally { clearTimeout(timer); }
  }
  return { fetch: request };
})();
