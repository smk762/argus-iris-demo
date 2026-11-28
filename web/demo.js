// The replay: owns recording time, and hands the dashboard (in the iframe, untouched) the recorded API through
// dash/shim.js. Recording time runs faster than life and skips quiet stretches; captions hold it still to be read.
const $ = (id) => document.getElementById(id);
const SCENES = ["contention", "eviction", "spill", "crash", "stall", "rehearse"];
const SPEED = 4; // recorded seconds per second
const MAX_GAP = 2; // seconds of real time any quiet stretch may take

const tapes = new Map();
let tape = null;
let marks = []; // [{t, w}]: recording time -> wall time, piecewise linear
let t = 0; // recording time, s
let playing = false;
let hold = 0; // wall seconds left to read the current caption
let shown = -1; // index of the caption on screen
let sent = 0; // events already delivered to the dashboard
let source = null; // the dashboard's event stream
let last = 0;

async function load(key) {
  if (!tapes.has(key)) {
    tapes.set(key, fetch(`tape/${key}.json`).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }));
    tapes.get(key).catch(() => tapes.delete(key));
  }
  return tapes.get(key);
}

function timeline(tp) {
  const ts = [...new Set([0, ...tp.events.map((e) => e.t), ...tp.beats.map((b) => b.t), tp.duration])].sort((a, b) => a - b);
  let w = 0;
  return ts.map((at, i) => {
    if (i) w += Math.min((at - ts[i - 1]) / SPEED, MAX_GAP);
    return { t: at, w };
  });
}
function convert(x, from, to) {
  const i = marks.findIndex((m) => m[from] >= x);
  if (i <= 0) return i ? marks.at(-1)[to] : marks[0][to];
  const [a, b] = [marks[i - 1], marks[i]];
  return b[from] === a[from] ? b[to] : a[to] + ((x - a[from]) / (b[from] - a[from])) * (b[to] - a[to]);
}
const total = () => marks.at(-1).w;

// ---------- what the dashboard asks for ----------
window.irisReplay = {
  now: () => (tape ? tape.start + t * 1000 : 0),
  http: (path) => tape?.http[path],
  attach(stream) {
    source = stream;
    resync();
  },
};

/** Bring the dashboard to time t from scratch, as a reconnect does: every node, then every active lease. */
function resync() {
  if (!source || !tape) return;
  const state = new Map();
  const active = new Map();
  sent = 0;
  for (const e of tape.init) state.set(e.d.name, e.d);
  while (sent < tape.events.length && tape.events[sent].t <= t) {
    const { e, d } = tape.events[sent++];
    if (e === "node") state.set(d.name, d);
    else if (d.state === "queued" || d.state === "granted") active.set(d.lease_id, d);
    else active.delete(d.lease_id);
  }
  source.emit("open");
  for (const d of state.values()) source.emit("node", d);
  for (const d of active.values()) source.emit("lease", d);
}
function deliver() {
  if (!source) return;
  while (sent < tape.events.length && tape.events[sent].t <= t) {
    const { e, d } = tape.events[sent++];
    source.emit(e, d);
  }
}

// ---------- captions ----------
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const readingTime = (b) => Math.max(5, (b.title + b.text).split(/\s+/).length * 0.3);
const beatAt = (at) => tape.beats.findLastIndex((b) => b.t <= at + 1e-6);

function show(i) {
  shown = i;
  const b = tape.beats[i];
  $("step").textContent = b ? `Step ${i + 1} of ${tape.beats.length}` : `${tape.beats.length} steps`;
  $("beat-title").textContent = b ? b.title : tape.title;
  $("beat-text").textContent = b ? b.text : "Press Play, or step through it.";
  $("prev").disabled = i <= 0 && SCENES.indexOf(tape.key) === 0;
  $("next").disabled = i >= tape.beats.length - 1 && SCENES.indexOf(tape.key) === SCENES.length - 1;
  focusNode(b?.focus);
}
function focusNode(name) {
  const doc = $("dash").contentDocument;
  if (!doc) return;
  for (const n of doc.querySelectorAll(".demo-focus")) n.classList.remove("demo-focus");
  const node = name && doc.getElementById(`node-${name}`);
  if (!node) return;
  node.classList.add("demo-focus");
  const win = $("dash").contentWindow;
  const bar = doc.querySelector(".bar")?.offsetHeight ?? 0;
  const calm = matchMedia("(prefers-reduced-motion: reduce)").matches;
  // On a phone the tiles fill the frame: start at the memory bar, just above the leases.
  const at = win.innerWidth < 600 ? (node.querySelector(".vram:not([hidden])") ?? node) : node;
  win.scrollTo({ top: at.getBoundingClientRect().top + win.scrollY - bar - 12, behavior: calm ? "auto" : "smooth" });
}

// ---------- log ----------
let logged = -1;
const HIDE = new Set(["t", "level", "event", "lease", "reason"]);
function renderLog() {
  const n = tape.logs.filter((l) => l.t <= t + 1e-6).length;
  if (n === logged) return;
  logged = n;
  $("log-count").textContent = n ? `(${n})` : "";
  $("log").replaceChildren(...tape.logs.slice(0, n).map((l) => {
    const li = document.createElement("li");
    li.className = l.level;
    const name = document.createElement("b");
    name.textContent = l.event;
    const rest = Object.entries(l).filter(([k]) => !HIDE.has(k))
      .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
    li.append(`${mmss(l.t)} `, name, ` ${rest}`);
    return li;
  }));
  $("log").scrollTop = $("log").scrollHeight;
}

// ---------- playing ----------
function paint() {
  const w = convert(t, "t", "w");
  const seek = $("seek");
  seek.value = Math.round((w / total()) * 1000);
  seek.setAttribute("aria-valuetext", `${mmss(t)} of ${mmss(tape.duration)}`);
  $("clock").textContent = `${mmss(t)} / ${mmss(tape.duration)}`;
  const label = playing ? "Pause" : t >= tape.duration ? (SCENES.at(-1) === tape.key ? "Replay" : "Next scene") : "Play";
  if ($("play").textContent !== label) $("play").textContent = label;
  renderLog();
}
function setPlaying(on) {
  playing = on;
  last = performance.now();
  paint();
}
function seekTo(at, { keepPlaying = playing } = {}) {
  t = Math.max(0, Math.min(tape.duration, at));
  hold = 0;
  resync();
  show(beatAt(t));
  if (shown >= 0 && keepPlaying) hold = readingTime(tape.beats[shown]);
  setPlaying(keepPlaying);
}
function frame(now) {
  requestAnimationFrame(frame);
  const dt = Math.min(0.25, (now - last) / 1000);
  last = now;
  if (!playing || !tape) return;
  if (hold > 0) {
    hold -= dt;
    return;
  }
  const before = t;
  t = convert(convert(t, "t", "w") + dt, "w", "t");
  const next = tape.beats[shown + 1];
  if (next && t >= next.t) {
    t = Math.max(before, next.t);
    deliver();
    show(shown + 1);
    hold = readingTime(next);
  } else {
    deliver();
  }
  if (t >= tape.duration) setPlaying(false);
  paint();
}

let opening = 0;
async function open(key, { at = null, play = playing, beat = null } = {}) {
  const mine = ++opening;
  let next;
  try {
    next = await load(key);
  } catch (e) {
    if (mine !== opening) return;
    $("beat-title").textContent = "The recording did not load";
    $("beat-text").textContent = `${e.message}. Reload the page to try again.`;
    setButtons(true);
    return;
  }
  if (mine !== opening) return; // a later click won
  setButtons(false);
  tape = next;
  marks = timeline(tape);
  logged = -1;
  history.replaceState(null, "", `#${key}`);
  $("lede").textContent = tape.lede;
  $("why-text").textContent = tape.why;
  $("version").textContent = `argus-iris ${tape.iris}`;
  $("code").replaceChildren(...tape.code.map((c) => {
    const li = document.createElement("li");
    const a = Object.assign(document.createElement("a"), { href: c.url, textContent: c.label });
    li.append(a);
    return li;
  }));
  for (const b of $("scenes").querySelectorAll("button")) {
    b.setAttribute("aria-current", String(b.dataset.key === key));
    const list = $("scenes"); // on a phone the list scrolls sideways
    if (b.dataset.key === key) list.scrollLeft = b.parentElement.offsetLeft - list.offsetLeft - 16;
  }
  seekTo(beat === "last" ? tape.beats.at(-1).t : (at ?? 0), { keepPlaying: play });
}
function setButtons(disabled) {
  for (const id of ["prev", "play", "next", "seek"]) $(id).disabled = disabled;
}
function step(d) {
  if (!tape) return;
  const i = shown + d;
  const s = SCENES.indexOf(tape.key);
  if (i >= 0 && i < tape.beats.length) seekTo(tape.beats[i].t);
  else if (d > 0 && s < SCENES.length - 1) open(SCENES[s + 1]);
  else if (d < 0 && s > 0) open(SCENES[s - 1], { beat: "last" });
}

$("play").addEventListener("click", () => {
  if (!tape) return;
  if (t < tape.duration) return setPlaying(!playing);
  const s = SCENES.indexOf(tape.key);
  if (s < SCENES.length - 1) open(SCENES[s + 1], { play: true });
  else open(SCENES[0], { play: true });
});
$("prev").addEventListener("click", () => step(-1));
$("next").addEventListener("click", () => step(1));
$("seek").addEventListener("input", (e) => {
  if (tape) seekTo(convert((e.target.value / 1000) * total(), "w", "t"), { keepPlaying: false });
});
addEventListener("hashchange", () => {
  const key = location.hash.slice(1);
  if (SCENES.includes(key) && key !== tape?.key) open(key, { play: false });
});

const TITLES = ["Two jobs, one card", "Trust the GPU", "Anywhere it fits", "When a client dies", "Alive, but not working", "Rehearse before you enforce"];
$("scenes").append(...SCENES.map((key, i) => {
  const li = document.createElement("li");
  const b = Object.assign(document.createElement("button"), { type: "button" });
  b.dataset.key = key;
  const n = Object.assign(document.createElement("span"), { className: "n", textContent: i + 1 });
  b.append(n, TITLES[i]);
  b.addEventListener("click", () => open(key, { play: true }));
  li.append(b);
  return li;
}));

// The dashboard follows this page's theme.
document.addEventListener("dh-theme-change", (e) => $("dash").contentWindow?.dhTheme?.set(e.detail.theme));
$("dash").addEventListener("load", () => {
  const win = $("dash").contentWindow;
  if (document.documentElement.dataset.theme) win.dhTheme?.set(document.documentElement.dataset.theme);
  focusNode(tape?.beats[shown]?.focus);
});

// First paint: a moment where something is already on screen, paused, so nothing moves until asked to.
const first = SCENES.includes(location.hash.slice(1)) ? location.hash.slice(1) : SCENES[0];
setButtons(true);
await open(first, { play: false });
$("dash").src = "dash/ui/index.html";
last = performance.now();
requestAnimationFrame(frame);
