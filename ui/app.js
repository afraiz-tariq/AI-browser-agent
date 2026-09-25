// The agent's app window (see app_ui.py). Talks to Python through
// window.pywebview.api; Python pushes events with window.app.onEvent(event).
//
// SAFETY: everything shown here is inserted as TEXT (textContent), never as
// HTML -- this file never assigns inner HTML. Steps, results and questions can carry
// text the agent read from web pages and apps, and this window can answer
// approvals, so none of that text may ever become markup or script.
"use strict";

const MODE = document.body.dataset.mode === "compact" ? "compact" : "main";

// ---------- small DOM helpers (text only) ----------
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function svg(d, size) {
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", size || 18);
  s.setAttribute("height", size || 18);
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "2");
  s.setAttribute("stroke-linecap", "round");
  s.setAttribute("stroke-linejoin", "round");
  for (const part of d.split("|")) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", part);
    s.appendChild(p);
  }
  return s;
}
const ICON = {
  mic: "M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z|M19 10v1a7 7 0 0 1-14 0v-1|M12 18v4",
  send: "M5 12h14|M13 6l6 6-6 6",
  pause: "M8 5v14|M16 5v14",
  play: "M7 4l12 8-12 8z",
  stop: "M6 6h12v12H6z",
  hand: "M8 13V5.5a1.5 1.5 0 0 1 3 0V12|M11 11.5v-8a1.5 1.5 0 0 1 3 0V12|M14 10.5a1.5 1.5 0 0 1 3 0V13|M17 11.5a1.5 1.5 0 0 1 3 0V16a6 6 0 0 1-6 6h-2a6 6 0 0 1-4.5-2L4 16.5a1.6 1.6 0 0 1 2.4-2.1L8 16",
  compact: "M4 14h6v6|M20 10h-6V4|M14 10l7-7|M3 21l7-7",
  expand: "M15 3h6v6|M9 21H3v-6|M21 3l-7 7|M3 21l7-7",
  minus: "M5 12h14",
  plus: "M12 5v14|M5 12h14",
  back: "M15 18l-6-6 6-6",
  again: "M3 12a9 9 0 1 0 3-6.7|M3 4v5h5",
};
function button(cls, label, onClick, icon) {
  const b = el("button", cls);
  if (icon) b.appendChild(svg(ICON[icon], 16));
  if (label) b.appendChild(el("span", "", label));
  b.addEventListener("click", onClick);
  return b;
}
const api = () => (window.pywebview && window.pywebview.api) || null;
async function call(name, ...args) {
  const a = api();
  if (!a || typeof a[name] !== "function") return null;
  try { return await a[name](...args); } catch (e) { console.error(name, e); return null; }
}

// ---------- plain-language names for actions ----------
const ACTIONS = {
  goto: "Opening a page", click: "Clicking", type: "Typing", scroll: "Scrolling", go_back: "Going back",
  wait: "Waiting", extract: "Reading the page", finish: "Finishing up", login_required: "Needs a login",
  excel_open: "Opening a spreadsheet", excel_read_cell: "Reading a cell", excel_read_range: "Reading cells",
  excel_write_cell: "Writing a cell", excel_save: "Saving the spreadsheet",
  windows_launch_app: "Opening an app", windows_list_windows: "Looking at open windows",
  windows_list_controls: "Looking at the window", windows_click_control: "Clicking a button",
  windows_click_controls: "Clicking buttons", windows_type_into_control: "Typing",
  windows_read_control_text: "Reading text", windows_close_window: "Closing a window",
  windows_screenshot: "Taking a screenshot", ask_user: "Asking you",
};
const KEYS = { ctrl_r: "Right Ctrl", ctrl_l: "Left Ctrl", ctrl: "Ctrl", alt_r: "Right Alt", alt_gr: "AltGr",
  alt_l: "Left Alt", shift_r: "Right Shift", shift_l: "Left Shift", caps_lock: "Caps Lock",
  scroll_lock: "Scroll Lock", page_up: "Page Up", page_down: "Page Down" };
const keyName = (k) => KEYS[k] || (k ? k.toUpperCase() : "");
const actionName = (a) => ACTIONS[a] || (a || "").replace(/_/g, " ");
function clock(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
function secs(ms) { return ms < 950 ? `${Math.max(1, Math.round(ms / 100)) / 10}s` : `${(ms / 1000).toFixed(1)}s`; }
function when(text) {
  if (!text) return "";
  const d = new Date(text);
  if (isNaN(d)) return text;
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  return same ? `Today ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
              : d.toLocaleDateString([], { month: "short", day: "numeric" }) + " " +
                d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ---------- state ----------
const S = {
  busy: false, paused: false, takeover: false, status: { kind: "ready", text: "Ready" },
  question: null, info: {}, run: null, feedStarted: false, viewing: null,
  latest: { task: "", line: "", result: null },
};

// ---------- toast ----------
let toastTimer = null;
function toast(text) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
}

// =====================================================================
// MAIN WINDOW
// =====================================================================
const M = {};

function buildMain(root) {
  const app = el("div", "app");
  // sidebar
  const side = el("aside", "sidebar");
  const brand = el("div", "brand");
  brand.append(el("div", "brand-mark", "✦"), el("span", "", "AI Agent"));
  const newTask = button("new-task", "New task", () => { backToLive(); M.input.focus(); }, "plus");
  M.history = el("div", "history");
  M.foot = el("div", "side-foot");
  side.append(brand, newTask, el("div", "side-label", "History"), M.history, M.foot);

  // main column
  const main = el("section", "main");
  const top = el("div", "topbar");
  M.pill = el("div", "pill");
  M.dot = el("span", "dot ready");
  M.statusText = el("span", "", "Ready");
  M.pill.append(M.dot, M.statusText);
  M.pauseBtn = button("btn", "Pause", () => call("pause"), "pause");
  M.resumeBtn = button("btn primary", "Continue", () => call("resume"), "play");
  M.takeBtn = button("btn", "Take over", () => call("take_over"), "hand");
  M.stopBtn = button("btn danger", "Stop", () => call("stop"), "stop");
  const compactBtn = button("btn icon", "", () => call("compact"), "compact");
  compactBtn.title = "Compact mode (small window on top)";
  top.append(M.pill, el("div", "spacer"), M.pauseBtn, M.resumeBtn, M.takeBtn, M.stopBtn, compactBtn);

  M.feedWrap = el("div", "feed-wrap");
  M.feed = el("div", "feed");
  M.feedWrap.appendChild(M.feed);

  const cw = el("div", "composer-wrap");
  const comp = el("div", "composer");
  M.input = el("textarea");
  M.input.rows = 1;
  M.input.addEventListener("input", autosize);
  M.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFromComposer(); }
  });
  M.mic = button("round mic", "", () => call("mic"), "mic");
  M.mic.title = "Talk (same as tapping your talk key)";
  M.send = button("round send", "", sendFromComposer, "send");
  M.send.title = "Send (Enter)";
  comp.append(M.input, M.mic, M.send);
  M.hint = el("div", "hint");
  cw.append(comp, M.hint);

  main.append(top, M.feedWrap, cw);
  app.append(side, main);
  root.append(app, Object.assign(el("div", "toast"), { id: "toast" }));
  showWelcome();
  refreshControls();
}

function autosize() {
  M.input.style.height = "auto";
  M.input.style.height = Math.min(M.input.scrollHeight, 140) + "px";
}

function scrollDown() { M.feedWrap.scrollTop = M.feedWrap.scrollHeight; }

function showWelcome() {
  M.feed.replaceChildren();
  const w = el("div", "welcome");
  const mark = el("div", "brand-mark", "✦");
  const key = keyName(S.info.talk_key) || "Right Ctrl";
  w.append(mark, el("h1", "", "What should I do?"),
    el("p", "", `${S.info.voice_mode === "hold" ? "Hold" : "Tap"} ${key} and speak, or type below. I'll show every step and ask before anything risky.`));
  const chips = el("div", "chips");
  for (const ex of ["Open Notepad and write hello", "Search YouTube for lofi beats", "Take a screenshot",
                    "What is 25 times 4 in Calculator?"]) {
    chips.appendChild(button("chip", ex, () => { M.input.value = ex; autosize(); M.input.focus(); }));
  }
  w.appendChild(chips);
  M.feed.appendChild(w);
  S.feedStarted = false;
}

function ensureFeed() {
  if (!S.feedStarted) { M.feed.replaceChildren(); S.feedStarted = true; }
}

function refreshControls() {
  if (MODE !== "main") return;
  const busy = S.busy;
  M.pauseBtn.classList.toggle("hidden", !busy || S.paused);
  M.takeBtn.classList.toggle("hidden", !busy || S.paused);
  M.resumeBtn.classList.toggle("hidden", !(busy && S.paused));
  M.stopBtn.classList.toggle("hidden", !busy);
  const q = S.question;
  M.input.placeholder = q && q.kind === "question" ? "Type your answer…"
    : q && q.kind === "confirm" ? "Type yes or no, or use the buttons…"
    : busy ? "Message the agent — it reads this before its next step…"
    : "Ask me to do something…";
  const key = keyName(S.info.talk_key) || "Right Ctrl";
  M.hint.textContent = busy
    ? `Messages you send now steer the running task. ${keyName(S.info.stop_key) || "F10"} or Stop ends it.`
    : `${S.info.voice_mode === "hold" ? "Hold" : "Tap"} ${key} to talk · Enter to send · Shift+Enter for a new line`;
}

function setStatus(kind, text) {
  S.status = { kind, text };
  const cls = `dot ${kind}`;
  if (MODE === "main") {
    M.dot.className = cls;
    M.statusText.textContent = text;
    M.mic.classList.toggle("on", kind === "listening");
  } else {
    C.dot.className = cls;
    C.status.textContent = text;
    C.mic.classList.toggle("on", kind === "listening");
  }
}

// ----- a task's card -----
function startRun(text, source, ts) {
  ensureFeed();
  const u = el("div", "msg-user");
  u.append(el("div", "bubble", text), el("div", "msg-meta", `${source === "typed" ? "Typed" : "Said"} · ${clock(ts)}`));
  const run = el("div", "run");
  const head = el("div", "run-head");
  const title = el("b", "", "Working on it");
  head.append(el("div", "brand-mark", "✦"), title);
  const steps = el("div", "steps");  // the timeline: steps, cards, notes, in order
  run.append(head, steps);
  M.feed.append(u, run);
  S.run = { el: run, title, steps, current: null, cards: {} };
  scrollDown();
}

function currentRun(ts) {
  if (!S.run) startRun("(task)", "voice", ts);
  return S.run;
}

function finishStep(run, ts) {
  const cur = run.current;
  if (!cur) return;
  cur.row.classList.add("done");
  cur.icon.replaceChildren();
  cur.icon.textContent = "✓";
  cur.time.textContent = secs(((ts || Date.now() / 1000) - cur.start) * 1000);
  run.current = null;
}

function addStep(n, text, action, ts) {
  const run = currentRun(ts);
  finishStep(run, ts);
  const row = el("div", "step");
  const icon = el("div", "step-icon");
  icon.appendChild(el("div", "spinner"));
  const body = el("div", "step-text");
  body.append(el("div", "", text || actionName(action)), el("div", "step-action", `${n}. ${actionName(action)}`));
  const time = el("div", "step-time", "");
  row.append(icon, body, time);
  run.steps.appendChild(row);
  run.current = { row, icon, time, start: ts || Date.now() / 1000 };
  scrollDown();
}

function addInfo(text, ts) {
  if (!S.run) { ensureFeed(); M.feed.appendChild(el("div", "banner", text)); scrollDown(); return; }
  S.run.steps.appendChild(el("div", "info-line", text));
  scrollDown();
}

function questionCard(q, compact) {
  const kind = q.kind;
  const card = el("div", `card ${kind}`);
  const titles = { confirm: "Allow this?", question: "The agent asks", handoff: "Your turn" };
  card.append(el("div", "card-title", titles[kind] || "Question"), el("div", "card-text", q.text));
  const actions = el("div", "card-actions");
  if (kind === "confirm") {
    actions.append(button("btn ok", "Allow", () => answer(q.qid, true)),
                   button("btn", "Don't allow", () => answer(q.qid, false)));
  } else if (kind === "handoff") {
    actions.append(button("btn primary", "Continue", () => answer(q.qid, true)),
                   button("btn danger", "Stop task", () => answer(q.qid, false)));
  } else {
    const input = el("input");
    input.placeholder = "Your answer…";
    const send = () => { if (input.value.trim()) answer(q.qid, input.value.trim()); };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
    actions.append(input, button("btn primary", "Send", send));
    setTimeout(() => input.focus(), 50);
  }
  card.appendChild(actions);
  card.dataset.qid = q.qid;
  return card;
}

async function answer(qid, value) {
  const ok = await call("answer", qid, value);
  if (ok === false) toast("That question is already closed.");
}

function showQuestion(q, ts) {
  S.question = q;
  if (MODE === "main") {
    const run = currentRun(ts);
    const card = questionCard(q);
    run.steps.appendChild(card);
    run.cards[q.qid] = card;
    scrollDown();
  }
}

function closeQuestion(qid, label) {
  if (S.question && S.question.qid === qid) S.question = null;
  if (MODE !== "main") return;
  const card = S.run && S.run.cards[qid];
  if (!card) return;
  card.classList.add("closed");
  const actions = card.querySelector(".card-actions");
  if (actions) actions.remove();
  card.appendChild(el("div", "card-answer", label || "Closed"));
}

function showResult(ok, text, ts) {
  const run = currentRun(ts);
  finishStep(run, ts);
  run.title.textContent = ok ? "Done" : "Stopped";
  const r = el("div", `result ${ok ? "ok" : "bad"}`);
  r.append(el("div", "result-icon", ok ? "✓" : "✕"), el("div", "result-text", text));
  run.el.appendChild(r);
  S.run = null;
  scrollDown();
  setTimeout(loadHistory, 800);
}

function addNote(text, ts) {
  ensureFeed();
  if (S.run) {
    const line = el("div", "note-line");
    line.append(el("span", "note-who", "You"), el("span", "", text));
    S.run.steps.appendChild(line);
  } else {
    const u = el("div", "msg-user msg-note");
    u.append(el("div", "bubble", text), el("div", "msg-meta", `Message to the agent · ${clock(ts)}`));
    M.feed.appendChild(u);
  }
  scrollDown();
}

function showPaused(paused, takeover) {
  S.paused = paused;
  S.takeover = takeover;
  if (MODE === "main" && S.run) {
    if (S.run.banner) { S.run.banner.remove(); S.run.banner = null; }
    if (paused) {
      S.run.banner = el("div", "banner", takeover
        ? "Your turn — do what you need on the screen, then press Continue. You can also send a message first."
        : "Paused before the next step. Press Continue to carry on.");
      S.run.steps.appendChild(S.run.banner);
      scrollDown();
    }
  }
}

async function sendFromComposer() {
  const text = M.input.value.trim();
  if (!text) return;
  const res = await call("submit", text);
  if (res === "refused") {
    toast(S.question ? "Answer the question above first." : "Still working on the last task.");
    return;
  }
  if (res === null) { toast("Not connected yet."); return; }
  M.input.value = "";
  autosize();
  if (res === "note") toast("Sent — the agent will read it before its next step.");
}

// ----- history -----
async function loadHistory() {
  if (MODE !== "main") return;
  const runs = await call("history");
  M.history.replaceChildren();
  if (!runs || !runs.length) { M.history.appendChild(el("div", "h-empty", "Your finished tasks show up here.")); return; }
  for (const r of runs) {
    const item = el("button", "h-item");
    const dot = el("div", `h-dot ${r.ok ? "" : "bad"}`);
    const body = el("div", "");
    body.append(el("div", "h-task", r.task || "(task)"), el("div", "h-when", `${when(r.when)} · ${r.steps} steps`));
    item.append(dot, body);
    item.title = r.task;
    item.addEventListener("click", () => viewPast(r.id, item));
    M.history.appendChild(item);
  }
}

async function viewPast(id, item) {
  const r = await call("run_details", id);
  if (!r) { toast("Couldn't open that task."); return; }
  if (!S.viewing) S.viewing = { saved: Array.from(M.feed.childNodes), started: S.feedStarted };
  for (const n of M.history.querySelectorAll(".h-item")) n.classList.remove("active");
  if (item) item.classList.add("active");
  M.feed.replaceChildren();
  const head = el("div", "past-head");
  head.append(button("btn", "Back to now", backToLive, "back"), el("span", "", `Past task · ${when(r.when)}`));
  const u = el("div", "msg-user");
  u.appendChild(el("div", "bubble", r.task));
  const run = el("div", "run");
  const rh = el("div", "run-head");
  rh.append(el("div", "brand-mark", "✦"), el("b", "", `${r.actions.length} steps`));
  const chips = el("div", "chip-list");
  for (const a of r.actions) chips.appendChild(el("span", "a-chip", `${a.n}. ${actionName(a.action)}`));
  const res = el("div", `result ${r.ok ? "ok" : "bad"}`);
  res.append(el("div", "result-icon", r.ok ? "✓" : "✕"), el("div", "result-text", r.summary || "(no summary)"));
  run.append(rh, chips, res);
  const again = button("btn", "Run it again", async () => {
    backToLive();
    const out = await call("submit", r.task);
    if (out === "refused") toast("Still working on the last task.");
  }, "again");
  M.feed.append(head, u, run, again);
  M.feedWrap.scrollTop = 0;
}

function backToLive() {
  if (!S.viewing) return;
  M.feed.replaceChildren(...S.viewing.saved);
  S.feedStarted = S.viewing.started;
  S.viewing = null;
  for (const n of M.history.querySelectorAll(".h-item")) n.classList.remove("active");
  scrollDown();
}

function renderFoot() {
  if (MODE !== "main") return;
  M.foot.replaceChildren();
  const i = S.info || {};
  const line = (label, value) => { const d = el("div", ""); d.append(el("span", "", label + " "), el("b", "", value)); return d; };
  if (i.model) M.foot.appendChild(line("Model", `${i.provider || ""} · ${i.model}`));
  const keys = el("div", "");
  keys.append(el("span", "", "Talk "), el("span", "kbd", keyName(i.talk_key) || "Right Ctrl"), el("span", "", "  Stop "),
              el("span", "kbd", keyName(i.stop_key) || "F10"));
  M.foot.appendChild(keys);
}

// =====================================================================
// COMPACT WINDOW
// =====================================================================
const C = {};

function buildCompact(root) {
  const box = el("div", "compact");
  const top = el("div", "c-top pywebview-drag-region");
  C.dot = el("span", "dot ready");
  C.status = el("div", "c-status pywebview-drag-region", "Ready");
  const expand = button("ghost", "", () => call("expand"), "expand");
  expand.title = "Open the full window";
  const hide = button("ghost", "", () => call("hide"), "minus");
  hide.title = "Hide (the icon by the clock brings it back)";
  C.stop = button("ghost stop", "", () => call("stop"), "stop");
  C.stop.title = "Stop the task";
  top.append(C.dot, C.status, C.stop, expand, hide);
  C.body = el("div", "c-body");
  const foot = el("div", "c-foot");
  C.input = el("input");
  C.input.placeholder = "Type a task…";
  C.input.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter") return;
    const text = C.input.value.trim();
    if (!text) return;
    const res = await call("submit", text);
    if (res === "refused") { C.status.textContent = S.question ? "Answer the question first" : "Still working…"; return; }
    C.input.value = "";
  });
  C.mic = button("round mic", "", () => call("mic"), "mic");
  foot.append(C.input, C.mic);
  box.append(top, C.body, foot);
  root.appendChild(box);
  renderCompact();
}

function renderCompact() {
  if (MODE !== "compact") return;
  C.body.replaceChildren();
  const L = S.latest;
  if (L.task && !S.question) C.body.appendChild(el("div", "c-heard", `“${L.task}”`));
  if (S.question) C.body.appendChild(questionCard(S.question, true));
  else if (L.result) C.body.appendChild(el("div", "c-line", `${L.result.ok ? "✓" : "✕"} ${L.result.text}`));
  else if (L.line) C.body.appendChild(el("div", "c-line", L.line));
  else C.body.appendChild(el("div", "c-line", `Tap ${keyName(S.info.talk_key) || "Right Ctrl"} and speak, or type below.`));
  C.stop.classList.toggle("hidden", !S.busy);
  C.input.placeholder = S.question && S.question.kind === "question" ? "Type your answer…"
    : S.busy ? "Message the agent…" : "Type a task…";
}

// =====================================================================
// EVENTS FROM PYTHON
// =====================================================================
function handle(e, replay) {
  const ts = e.at;
  switch (e.type) {
    case "status": setStatus(e.kind, e.text); break;
    case "task":
      S.busy = true; S.paused = false;
      S.latest = { task: e.text, line: "Working on it…", result: null };
      if (MODE === "main") { backToLive(); startRun(e.text, e.source, ts); }
      break;
    case "step":
      S.latest.line = `Step ${e.n}: ${e.text}`;
      if (MODE === "main") addStep(e.n, e.text, e.action, ts);
      break;
    case "info":
      S.latest.line = e.text;
      if (MODE === "main") addInfo(e.text, ts);
      break;
    case "ask": showQuestion({ qid: e.qid, kind: e.kind, text: e.text }, ts); break;
    case "closed": closeQuestion(e.qid, e.label); break;
    case "note": if (MODE === "main") addNote(e.text, ts); break;
    case "paused": showPaused(e.paused, e.takeover); break;
    case "result":
      S.busy = false; S.paused = false; S.question = null;
      S.latest.result = { ok: e.ok, text: e.text };
      if (MODE === "main") showResult(e.ok, e.text, replay ? ts : null);
      break;
  }
  refreshControls();
  renderCompact();
}

window.app = { onEvent: (e) => handle(e, false) };

async function init() {
  const st = await call("get_state");
  if (!st) return;
  S.info = st.info || {};
  if (MODE === "main") { renderFoot(); showWelcome(); }
  for (const e of st.feed || []) handle(e, true);
  // the replayed feed ends in the live state
  S.busy = !!st.busy;
  S.paused = !!st.paused;
  if (st.question && !S.question) showQuestion(st.question);
  if (st.status) setStatus(st.status.kind, st.status.text);
  refreshControls();
  renderCompact();
  loadHistory();
}

(function boot() {
  const root = document.getElementById("root");
  if (MODE === "main") buildMain(root); else buildCompact(root);
  if (api()) init(); else window.addEventListener("pywebviewready", init);
})();
