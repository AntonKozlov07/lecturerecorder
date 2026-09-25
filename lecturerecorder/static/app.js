"use strict";

/* Lecture Recorder front end. Plain JS, no build step. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  lectures: [],
  courses: [],
  kind: null,           // "lecture" | "course": what state.current holds
  search: "",
  current: null,        // full lecture detail
  tab: "transcript",
  settings: null,
  engine: null,
  recorder: null,
  streaming: { notes: null, chat: null, expand: null },
  editingNotes: false,
  transcriptFilter: "",
  quiz: { viewId: null, answers: {}, busy: false, retake: false },
  deck: null,
  cardsBusy: false,
  topicsBusy: false,
};

// Utilities -----------------------------------------------------------------

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function fmtDuration(sec) {
  sec = Math.round(sec || 0);
  if (sec < 60) return `${sec}s`;
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m} min`;
}

function fmtDate(ts, withTime = false) {
  const d = new Date(ts * 1000);
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  if (withTime) Object.assign(opts, { hour: "numeric", minute: "2-digit" });
  return d.toLocaleString(undefined, opts);
}

function parseTs(str) {
  const p = str.split(":").map(Number);
  return p.length === 3 ? p[0] * 3600 + p[1] * 60 + p[2] : p[0] * 60 + p[1];
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), isError ? 6000 : 3000);
}

async function api(path, { method, json, form } = {}) {
  const opts = { method: method || (json || form ? "POST" : "GET"), headers: { "X-Lecture-Recorder": "1" } };
  if (json !== undefined) { opts.body = JSON.stringify(json); opts.headers["Content-Type"] = "application/json"; }
  if (form) opts.body = form;
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { const body = await res.json(); msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail); } catch {}
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return (res.headers.get("content-type") || "").includes("json") ? res.json() : res.text();
}

/** POST to a server-sent-events endpoint; calls onText for each chunk and resolves with the final event. */
async function streamApi(path, body, onText) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "X-Lecture-Recorder": "1", "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail; } catch {}
    throw new Error(msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const line = buf.slice(0, i);
      buf = buf.slice(i + 2);
      if (!line.startsWith("data: ")) continue;
      const ev = JSON.parse(line.slice(6));
      if (ev.t !== undefined) onText(ev.t);
      else if (ev.error) throw new Error(ev.error);
      else if (ev.done) return ev;
    }
  }
  throw new Error("The connection closed before the response finished.");
}

// Markdown with math and clickable timestamps --------------------------------

marked.setOptions({ gfm: true, breaks: false });
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") { node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer"); }
});

function md(src) {
  const math = [];
  const hold = (tex, display) => { math.push([tex, display]); return `MATHPH${math.length - 1}ENDPH`; };
  src = String(src || "")
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => hold(t, true))
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, t) => hold(t, true))
    .replace(/\\\((.+?)\\\)/g, (_, t) => hold(t, false))
    .replace(/(^|[^\\$\w])\$(?=\S)([^\n$]+?)(?<=\S)\$(?![\d\w])/g, (_, pre, t) => pre + hold(t, false));
  let html = DOMPurify.sanitize(marked.parse(src));
  html = html.replace(/MATHPH(\d+)ENDPH/g, (_, i) => {
    const [tex, display] = math[+i];
    try { return katex.renderToString(tex, { displayMode: display, throwOnError: false, output: "html" }); }
    catch { return esc(tex); }
  });
  // Turn [12:34] references into buttons that play the recording from that point.
  if (state.kind !== "lecture") return html;
  return html.replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, (m, ts) =>
    `<button class="link ts-link" data-t="${parseTs(ts)}" title="Play from ${ts}">${ts}</button>`);
}

// Audio playback ---------------------------------------------------------------

const player = $("#player");
let playing = null; // {segId, lineEl}

function segmentAt(seconds) {
  const segs = (state.current?.segments || []).filter((s) => s.has_audio);
  let best = null;
  for (const s of segs) if (s.start <= seconds + 0.5) best = s;
  return best;
}

function playAt(seconds) {
  const seg = segmentAt(seconds);
  if (!seg) { toast("No audio recorded for that moment."); return; }
  const offset = Math.max(0, seconds - seg.start);
  const src = `/api/segments/${seg.id}/audio`;
  const go = () => { try { player.currentTime = offset; } catch {} player.play().catch(() => toast("Could not play audio.", true)); };
  if (playing?.segId === seg.id && player.src.endsWith(src)) go();
  else {
    player.src = src;
    player.addEventListener("loadedmetadata", go, { once: true });
    player.load();
  }
  playing = { segId: seg.id };
  highlightPlaying(seconds);
}

function highlightPlaying(seconds) {
  $$(".tline.playing").forEach((el) => el.classList.remove("playing"));
  const lines = $$(".tline[data-t]");
  let hit = null;
  for (const el of lines) if (+el.dataset.t <= seconds + 0.5) hit = el;
  hit?.classList.add("playing");
}

player.addEventListener("ended", () => {
  $$(".tline.playing").forEach((el) => el.classList.remove("playing"));
  // Continue into the next recorded chunk so playback feels continuous.
  const segs = (state.current?.segments || []).filter((s) => s.has_audio);
  const idx = segs.findIndex((s) => s.id === playing?.segId);
  if (idx >= 0 && idx + 1 < segs.length) playAt(segs[idx + 1].start);
  else playing = null;
});

document.addEventListener("click", (e) => {
  const ts = e.target.closest("[data-t].ts-link, .tline .ts");
  if (!ts) return;
  e.preventDefault();
  const t = +(ts.dataset.t ?? ts.closest(".tline").dataset.t);
  if (playing && !player.paused && ts.closest(".tline")?.classList.contains("playing")) { player.pause(); return; }
  playAt(t);
});

// Sidebar ---------------------------------------------------------------------

async function loadLectures() {
  const [lectures, courses] = await Promise.all([
    api(`/api/lectures?q=${encodeURIComponent(state.search)}`),
    api("/api/courses"),
  ]);
  state.lectures = lectures;
  state.courses = courses;
  renderList();
  $("#course-options").innerHTML = courses.map((c) => `<option value="${esc(c.name)}">`).join("");
}

function renderList() {
  const nav = $("#lecture-list");
  const q = state.search.toLowerCase();
  const byCourse = new Map();
  for (const l of state.lectures) {
    if (!byCourse.has(l.course)) byCourse.set(l.course, []);
    byCourse.get(l.course).push(l);
  }
  const recId = state.recorder?.lectureId;
  const item = (l) => `
    <button class="lecture-item ${state.kind === "lecture" && state.current?.id === l.id ? "active" : ""}" data-id="${l.id}">
      <span class="t">${esc(l.title)}</span>
      <span class="m">
        ${l.id === recId ? '<span><span class="rec-dot live"></span> Recording</span>' : ""}
        <span>${fmtDate(l.created_at)}</span>
        ${l.duration ? `<span>${fmtDuration(l.duration)}</span>` : ""}
        ${l.status === "processing" ? "<span>Transcribing</span>" : ""}
      </span>
    </button>`;
  const groups = state.courses
    .filter((c) => !q || byCourse.has(c.name) || c.name.toLowerCase().includes(q))
    .map((c) => `
      <button class="list-group course-link ${state.kind === "course" && state.current?.id === c.id ? "active" : ""}" data-course="${c.id}" title="Open course: materials, course-wide chat, quizzes and flashcards">
        <span>${esc(c.name)}</span>${c.material_count ? `<span class="count">${c.material_count} file${c.material_count > 1 ? "s" : ""}</span>` : ""}
      </button>
      ${(byCourse.get(c.name) || []).map(item).join("")}`);
  const loose = byCourse.get("") || [];
  if (loose.length) groups.push(`${state.courses.length ? '<div class="list-group">No course</div>' : ""}${loose.map(item).join("")}`);
  nav.innerHTML = groups.join("") || `<div class="list-empty">${q ? "No matches." : "No lectures yet."}</div>`;
}

$("#lecture-list").addEventListener("click", (e) => {
  const course = e.target.closest(".course-link");
  if (course) { location.hash = `#/course/${course.dataset.course}`; return; }
  const item = e.target.closest(".lecture-item");
  if (item) location.hash = `#/lecture/${item.dataset.id}`;
});

let searchTimer;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.search = e.target.value.trim(); loadLectures(); }, 200);
});

// Engine status --------------------------------------------------------------

async function pollStatus() {
  try {
    const s = await api("/api/status");
    state.engine = s;
    document.body.classList.toggle("on-phone", !!s.on_phone);
    $("#btn-phone").hidden = !!s.on_phone;
    const el = $("#engine-status");
    const t = s.transcriber;
    el.className = "engine-status";
    if (t.state === "loading") { el.classList.add("busy"); el.textContent = "Loading speech model"; el.title = t.detail; }
    else if (t.state === "transcribing") { el.classList.add("busy"); el.textContent = t.queued ? `Transcribing, ${t.queued} queued` : "Transcribing"; el.title = ""; }
    else if (t.state === "error") { el.classList.add("error"); el.textContent = "Transcription error"; el.title = t.detail; }
    else if (!s.ai_ready) { el.textContent = "Add an API key in Settings"; el.title = "Needed for notes, chat and quizzes"; }
    else { el.textContent = "Ready"; el.title = ""; }
  } catch { /* server restarting */ }
}

// Routing ----------------------------------------------------------------------

async function route() {
  const m = location.hash.match(/^#\/(lecture|course)\/(\w+)/);
  document.body.classList.toggle("show-main", !!m);
  if (!m) { state.current = null; state.kind = null; renderEmpty(); renderList(); return; }
  const [, kind, id] = m;
  if (state.current?.id !== id || state.kind !== kind) {
    state.editingNotes = false;
    state.transcriptFilter = "";
    state.quiz = { viewId: null, answers: {}, busy: false, retake: false, grading: false };
    state.deck = null;
    if (kind === "course" && !COURSE_TABS.some(([k]) => k === state.tab)) state.tab = "overview";
    if (kind === "lecture" && !TABS.some(([k]) => k === state.tab)) state.tab = "transcript";
    player.pause();
    playing = null;
  }
  try {
    state.current = await api(`/api/${kind}s/${id}`);
    state.kind = kind;
  } catch {
    location.hash = "";
    return;
  }
  kind === "course" ? renderCourse() : renderLecture();
  renderList();
  $("#view").scrollTop = 0;
}

/** The API path for the open lecture or course. */
const apiBase = () => `/api/${state.kind}s/${state.current.id}`;
window.addEventListener("hashchange", route);

function renderEmpty() {
  const hasAny = state.lectures.length > 0;
  $("#view").innerHTML = `
    <div class="empty">
      <h1>${hasAny ? "Pick a lecture, or record a new one" : "Record your first lecture"}</h1>
      <ol>
        <li>Press <strong>New recording</strong> when the lecture starts. The transcript fills in as you go.</li>
        <li>Press <strong>Stop</strong> at the end. Notes are written for you.</li>
        <li>Ask questions in <strong>Chat</strong>, go deeper in <strong>Topics</strong>, and test yourself with a <strong>Quiz</strong> or <strong>Flashcards</strong>.</li>
        <li>Give lectures a course, then open the course in the sidebar to add slides, readings and other files and study everything together.</li>
      </ol>
      <p class="muted">Already have a recording or transcript? Use <strong>Import</strong>. To add slides, PDFs or readings, use <strong>New course</strong> in the sidebar, or <strong>Import &gt; Course files</strong>. To use your phone, click <strong>Phone</strong> at the bottom of the sidebar. Search with <kbd>Ctrl</kbd> <kbd>K</kbd>.</p>
      <div class="actions">
        <button class="btn btn-primary" data-act="new"><span class="rec-dot"></span>New recording</button>
        <button class="btn" data-act="import">Import</button>
        ${state.engine && !state.engine.ai_ready ? '<button class="btn" data-act="settings">Add API key</button>' : ""}
      </div>
    </div>`;
}

// Lecture view -------------------------------------------------------------------

const TABS = [
  ["transcript", "Transcript"],
  ["notes", "Notes"],
  ["topics", "Topics"],
  ["chat", "Chat"],
  ["quiz", "Quiz"],
  ["cards", "Flashcards"],
];

const COURSE_TABS = [
  ["overview", "Lectures and files"],
  ["chat", "Chat"],
  ["quiz", "Quiz"],
  ["cards", "Flashcards"],
];

function tabCount(key) {
  const l = state.current;
  const n = { topics: l.expansions?.length, chat: l.messages.length / 2, quiz: l.quizzes.length, cards: l.flashcards.length,
              overview: state.kind === "course" ? l.lectures.length + l.materials.length : 0 }[key];
  return n ? `<span class="count">${Math.floor(n)}</span>` : "";
}

function renderLecture() {
  const l = state.current;
  $("#view").innerHTML = `
    <header class="lecture-head">
      <button class="btn btn-ghost back" data-act="back" aria-label="Back to library">Library</button>
      <div class="titles">
        <input class="title-input" id="title-input" value="${esc(l.title)}" aria-label="Title">
        <div class="meta" id="lecture-meta"></div>
      </div>
      <div class="head-actions">
        <button class="btn btn-sm" data-act="export" title="Download notes, deep dives, flashcards and transcript as Markdown">Export</button>
        <button class="btn btn-sm btn-ghost" data-act="delete">Delete</button>
      </div>
    </header>
    <nav class="tabs" id="tabs"></nav>
    <section class="panel" id="panel"></section>`;
  renderMeta();
  renderTabs();
  renderPanel();

  const title = $("#title-input");
  title.addEventListener("change", () => saveField("title", title.value.trim() || "Untitled lecture"));
  title.addEventListener("keydown", (e) => { if (e.key === "Enter") title.blur(); });
}

function renderTabs() {
  $("#tabs").innerHTML = (state.kind === "course" ? COURSE_TABS : TABS).map(([k, label]) =>
    `<button class="tab ${state.tab === k ? "active" : ""}" data-tab="${k}">${label}${tabCount(k)}</button>`).join("");
}

function renderMeta() {
  const l = state.current;
  const el = $("#lecture-meta");
  if (!el) return;
  const recording = state.recorder?.lectureId === l.id;
  let status = "";
  if (recording) status = '<span class="pill danger"><span class="rec-dot live"></span>Recording</span>';
  else if (l.pending_segments) status = `<span class="pill accent">Transcribing ${l.pending_segments} part${l.pending_segments > 1 ? "s" : ""}</span>`;
  else if (l.notes_status === "generating") status = '<span class="pill accent">Writing notes</span>';
  if (l.failed_segments) status += ` <span class="pill danger">${l.failed_segments} part${l.failed_segments > 1 ? "s" : ""} failed</span>`;
  el.innerHTML = `
    <input class="course-input" id="course-input" value="${esc(l.course)}" placeholder="Add course" list="course-options" aria-label="Course">
    ${l.course_id ? `<button class="link" data-act="open-course" title="Course files, course-wide chat, quizzes and flashcards">Course files and quizzes</button>` : ""}
    <span>${fmtDate(l.created_at, true)}</span>
    ${l.duration ? `<span>${fmtDuration(l.duration)}</span>` : ""}
    ${status}`;
  const course = $("#course-input");
  const fit = () => (course.size = Math.max(10, course.value.length + 1));
  fit();
  course.addEventListener("input", fit);
  course.addEventListener("change", () => saveField("course", course.value.trim()));
  course.addEventListener("keydown", (e) => { if (e.key === "Enter") course.blur(); });
}

async function saveField(field, value) {
  try {
    await api(`/api/lectures/${state.current.id}`, { method: "PATCH", json: { [field]: value } });
    state.current[field] = value;
    await loadLectures();
    if (field === "course") refreshCurrent(true);
  } catch (err) { toast(err.message, true); }
}

$("#view").addEventListener("click", async (e) => {
  const tab = e.target.closest("[data-tab]");
  if (tab) { state.tab = tab.dataset.tab; $("#view").scrollTop = 0; renderTabs(); renderPanel(); return; }
  const act = e.target.closest("[data-act]");
  if (!act) return;
  const handler = actions[act.dataset.act];
  if (handler) handler(act, e);
});

function renderPanel() {
  const panel = $("#panel");
  if (!panel) return;
  ({ transcript: renderTranscript, notes: renderNotes, topics: renderTopics, chat: renderChat, quiz: renderQuiz,
     cards: renderCards, overview: renderOverview })[state.tab](panel);
}

function hasTranscript() {
  return state.current.segments.some((s) => s.status === "done" && s.text.trim());
}

/** Lectures and files a course-wide request will send, and a rough token count. */
function courseSources() {
  const c = state.current;
  const excluded = new Set(c.excluded);
  const lectures = c.lectures.filter((l) => l.tokens > 0 && !excluded.has(`lecture:${l.id}`));
  const files = c.materials.filter((m) => !excluded.has(`material:${m.id}`));
  const tokens = lectures.reduce((n, l) => n + l.tokens, 0) + files.reduce((n, m) => n + m.tokens, 0);
  return { lectures, files, tokens };
}

function aiBlocker() {
  if (!state.engine?.ai_ready) return `<div class="notice"><strong>AI features need an Anthropic API key.</strong> <button class="link" data-act="settings">Open Settings</button> to add one.</div>`;
  if (state.kind === "course") {
    const src = courseSources();
    if (!src.lectures.length && !src.files.length) {
      return `<div class="notice">Nothing to study yet. Add lectures to this course or upload files under <button class="link" data-tab="overview">Lectures and files</button>.</div>`;
    }
    return "";
  }
  if (!hasTranscript()) {
    const busy = state.current.pending_segments || state.recorder?.lectureId === state.current.id;
    return `<div class="notice">${busy ? "Waiting for the transcript. This fills in as audio is transcribed." : "There is no transcript yet."}</div>`;
  }
  return "";
}

// Transcript ------------------------------------------------------------------

function transcriptLines() {
  const lines = [];
  for (const seg of state.current.segments) {
    if (seg.status === "queued") { lines.push({ kind: "pending", seg }); continue; }
    if (seg.status === "error") { lines.push({ kind: "failed", seg }); continue; }
    if (seg.parts.length) for (const [s, , text] of seg.parts) lines.push({ kind: "text", t: seg.start + s, text, audio: seg.has_audio });
    else if (seg.text) lines.push({ kind: "text", t: seg.start, text: seg.text, audio: seg.has_audio });
  }
  return lines;
}

function renderTranscript(panel) {
  const l = state.current;
  const view = $("#view");
  const nearBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 80;
  const recording = state.recorder?.lectureId === l.id;

  if (!l.segments.length) {
    panel.innerHTML = recording
      ? `<div class="notice">Recording. The first part of the transcript appears about ${state.settings?.segment_seconds || 30} seconds in.</div>`
      : `<div class="notice">
          <p style="margin-top:0">No transcript yet. Paste one to use the study tools on it.</p>
          <textarea class="input" id="paste-text" rows="8" placeholder="Paste lecture text"></textarea>
          <div class="toolbar" style="margin:10px 0 0"><button class="btn btn-primary" data-act="paste">Save transcript</button></div>
        </div>`;
    return;
  }

  const filter = state.transcriptFilter.toLowerCase();
  const lines = transcriptLines();
  const rows = lines.map((ln) => {
    if (ln.kind === "pending") return filter ? "" : `<div class="tline pending"><span></span><span>Transcribing ${fmtTime(ln.seg.start)}${ln.seg.duration ? `–${fmtTime(ln.seg.start + ln.seg.duration)}` : ""}</span></div>`;
    if (ln.kind === "failed") return `<div class="tline failed"><span></span><span>Could not transcribe ${fmtTime(ln.seg.start)} onward. <button class="link" data-act="retry-seg" data-id="${ln.seg.id}">Retry</button> <span class="muted small">${esc(ln.seg.error)}</span></span></div>`;
    if (filter && !ln.text.toLowerCase().includes(filter)) return "";
    let text = esc(ln.text);
    if (filter) text = text.replace(new RegExp(filter.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), (m) => `<mark>${m}</mark>`);
    const ts = ln.audio ? `<button class="ts" title="Play from here">${fmtTime(ln.t)}</button>` : `<span class="ts">${fmtTime(ln.t)}</span>`;
    return `<div class="tline" data-t="${ln.t}">${ts}<span>${text}</span></div>`;
  }).join("");

  const hadFocus = document.activeElement?.id === "tfilter";
  panel.innerHTML = `
    <div class="toolbar">
      <input class="input" id="tfilter" placeholder="Find in transcript" value="${esc(state.transcriptFilter)}" style="max-width:280px">
      <span class="spacer"></span>
      <button class="btn btn-sm" data-act="copy-transcript">Copy</button>
    </div>
    <div class="transcript">${rows || '<p class="muted">No matches.</p>'}</div>`;
  const input = $("#tfilter");
  input.addEventListener("input", () => {
    state.transcriptFilter = input.value;
    renderTranscript(panel);
  });
  if (hadFocus) { input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
  if (playing) highlightPlaying(player.currentTime + (segmentById(playing.segId)?.start || 0));
  if (recording && nearBottom && !filter) view.scrollTop = view.scrollHeight;
}

const segmentById = (id) => state.current?.segments.find((s) => s.id === id);

// Notes ---------------------------------------------------------------------------

function renderNotes(panel) {
  const l = state.current;
  const s = state.streaming.notes;
  if (s && s.id === l.id) {
    panel.innerHTML = `<div class="toolbar"><span class="muted">Writing notes</span></div>
      <div class="prose" id="notes-live">${s.text ? md(s.text) : '<span class="thinking">Reading the lecture</span>'}</div>`;
    if (s.text) $("#notes-live").lastElementChild?.classList.add("caret");
    return;
  }
  if (state.editingNotes) {
    panel.innerHTML = `
      <div class="toolbar">
        <button class="btn btn-primary" data-act="save-notes">Save</button>
        <button class="btn" data-act="cancel-notes">Cancel</button>
        <span class="muted small">Markdown. Use $...$ for math.</span>
      </div>
      <textarea class="input notes-editor" id="notes-editor">${esc(l.notes)}</textarea>`;
    $("#notes-editor").focus();
    return;
  }
  if (l.notes_status === "generating") {
    panel.innerHTML = `<div class="notice"><span class="thinking">Writing notes</span></div>`;
    return;
  }
  const blocker = aiBlocker();
  const error = l.notes_status === "error" && l.notes_error ? `<div class="notice error" style="margin-bottom:14px">Notes failed: ${esc(l.notes_error)}</div>` : "";
  if (!l.notes.trim()) {
    panel.innerHTML = error + (blocker || `
      <div class="notice">
        <p style="margin-top:0">No notes yet.</p>
        <button class="btn btn-primary" data-act="gen-notes">Write notes</button>
        <button class="btn" data-act="edit-notes">Write my own</button>
      </div>`);
    return;
  }
  panel.innerHTML = `
    ${error}
    <div class="toolbar">
      <button class="btn btn-sm" data-act="gen-notes" ${blocker ? "disabled" : ""}>Rewrite</button>
      <button class="btn btn-sm" data-act="edit-notes">Edit</button>
      <button class="btn btn-sm" data-act="copy-notes">Copy</button>
    </div>
    <article class="prose">${md(l.notes)}</article>`;
}

let renderQueued = false;
function scheduleRender(fn) {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; fn(); });
}

async function generateNotes() {
  const l = state.current;
  if (l.notes.trim() && !confirm("Replace the current notes with a new version?")) return;
  const s = (state.streaming.notes = { id: l.id, text: "" });
  renderMeta();
  renderPanel();
  try {
    await streamApi(`/api/lectures/${l.id}/notes/stream`, {}, (t) => {
      s.text += t;
      if (state.current?.id === s.id && state.tab === "notes") scheduleRender(renderPanel);
    });
    if (state.current?.id === s.id) { state.current.notes = s.text; state.current.notes_status = "done"; }
  } catch (err) {
    toast(err.message, true);
  } finally {
    state.streaming.notes = null;
    if (state.current?.id === s.id) await refreshCurrent(true);
  }
}

// Topics and deep dives ---------------------------------------------------------------

function renderTopics(panel) {
  const l = state.current;
  const blocker = aiBlocker();
  const s = state.streaming.expand;
  const live = s && s.id === l.id ? `
    <details class="expansion" open>
      <summary><strong>${esc(s.topic)}</strong><span class="muted small">Writing</span></summary>
      <div class="prose" id="expand-live">${s.text ? md(s.text) : '<span class="thinking">Thinking</span>'}</div>
    </details>` : "";
  panel.innerHTML = `
    ${blocker}
    <div class="toolbar" ${blocker ? "hidden" : ""}>
      <button class="btn ${l.topics.length ? "" : "btn-primary"}" data-act="find-topics" ${state.topicsBusy ? "disabled" : ""}>
        ${state.topicsBusy ? "Finding topics…" : l.topics.length ? "Refresh topics" : "Find topics"}</button>
    </div>
    <form class="inline-form" id="expand-form" ${blocker ? "hidden" : ""}>
      <input class="input" name="topic" placeholder="Expand on anything from the lecture, e.g. why entropy always increases" autocomplete="off">
      <button class="btn" ${s ? "disabled" : ""}>Expand</button>
    </form>
    ${l.topics.length ? `
      <h3 class="section-title">Topics covered</h3>
      <div class="topics-grid">
        ${l.topics.map((t, i) => `
          <div class="topic">
            <div class="h"><strong>${esc(t.title)}</strong>${t.timestamp ? `<button class="link quiet mono small ts-link" data-t="${parseTs(t.timestamp.replace(/[[\]]/g, "")) || 0}">${esc(t.timestamp.replace(/[[\]]/g, ""))}</button>` : ""}</div>
            <p>${esc(t.summary)}</p>
            <button class="btn btn-sm" data-act="expand-topic" data-i="${i}" ${s || blocker ? "disabled" : ""}>Go deeper</button>
          </div>`).join("")}
      </div>` : ""}
    ${live || l.expansions.length ? '<h3 class="section-title">Deep dives</h3>' : ""}
    ${live}
    ${l.expansions.map((x, i) => `
      <details class="expansion" ${i === 0 && !live ? "open" : ""}>
        <summary><strong>${esc(x.topic)}</strong><span class="muted small">${fmtDate(x.created_at, true)}</span></summary>
        <div class="prose">${md(x.content)}</div>
        <div class="toolbar" style="padding:0 18px 12px 30px;margin:0">
          <button class="btn btn-sm" data-act="copy-expansion" data-id="${x.id}">Copy</button>
          <button class="btn btn-sm btn-ghost" data-act="delete-expansion" data-id="${x.id}">Delete</button>
        </div>
      </details>`).join("")}`;
  if (s?.text && $("#expand-live")) $("#expand-live").lastElementChild?.classList.add("caret");
  $("#expand-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const topic = e.target.topic.value.trim();
    if (topic) expand(topic);
  });
}

async function findTopics() {
  const l = state.current;
  state.topicsBusy = true;
  renderPanel();
  try {
    const topics = await api(`/api/lectures/${l.id}/topics`, { json: {} });
    if (state.current?.id === l.id) state.current.topics = topics;
  } catch (err) { toast(err.message, true); }
  state.topicsBusy = false;
  if (state.current?.id === l.id && state.tab === "topics") renderPanel();
}

async function expand(topic) {
  const l = state.current;
  if (state.streaming.expand) return;
  const s = (state.streaming.expand = { id: l.id, topic, text: "" });
  renderPanel();
  try {
    await streamApi(`/api/lectures/${l.id}/expand/stream`, { topic }, (t) => {
      s.text += t;
      if (state.current?.id === s.id && state.tab === "topics") scheduleRender(() => {
        const el = $("#expand-live");
        if (el) { el.innerHTML = md(s.text); el.lastElementChild?.classList.add("caret"); }
      });
    });
  } catch (err) { toast(err.message, true); }
  state.streaming.expand = null;
  if (state.current?.id === s.id) await refreshCurrent(true);
}

// Chat ---------------------------------------------------------------------------------

const COURSE_SUGGESTIONS = [
  "What are the main themes of this course so far?",
  "Make me a one-page study guide for the exam.",
  "Which topics come up in both the lectures and the readings?",
  "What should I focus on if I only have two hours to study?",
];

const SUGGESTIONS = [
  "Summarize this lecture in five bullet points.",
  "What did the lecturer say is likely to be on the exam?",
  "Explain the hardest idea in this lecture as simply as possible.",
  "What should I review before the next lecture?",
];

function renderChat(panel) {
  const l = state.current;
  const blocker = aiBlocker();
  const s = state.streaming.chat;
  const msgs = [...l.messages];
  const pending = s && s.id === l.id;
  const log = msgs.map((m) => chatMsg(m.role, m.content)).join("") +
    (pending ? chatMsg("user", s.question) + `<div class="msg assistant"><div class="who">Assistant</div><div class="prose" id="chat-live">${s.text ? md(s.text) : '<span class="thinking">Thinking</span>'}</div></div>` : "");
  const draft = $("#chat-text")?.value || "";
  panel.innerHTML = `
    <div class="chat">
      ${blocker}
      <div class="chat-log" id="chat-log">
        ${log || (blocker ? "" : `<p class="muted">${state.kind === "course"
            ? "Ask anything about this course. Answers draw on its lectures and files and say where each point comes from."
            : "Ask anything about this lecture. Answers draw on the transcript and say when they go beyond it."}</p>
          <div class="suggestions">${(state.kind === "course" ? COURSE_SUGGESTIONS : SUGGESTIONS).map((q) => `<button class="link" data-act="suggest">${esc(q)}</button>`).join("")}</div>`)}
      </div>
      <form class="chat-input" id="chat-form" ${blocker ? "hidden" : ""}>
        <textarea class="input" id="chat-text" rows="1" placeholder="Ask a question  (Enter to send, Shift+Enter for a new line)">${esc(draft)}</textarea>
        <button class="btn btn-primary" ${pending ? "disabled" : ""}>Send</button>
        ${msgs.length && !pending ? '<button type="button" class="btn btn-ghost" data-act="clear-chat">Clear</button>' : ""}
      </form>
    </div>`;
  fitChat();
  const logEl = $("#chat-log");
  logEl.scrollTop = logEl.scrollHeight;
  const ta = $("#chat-text");
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(200, ta.scrollHeight + 2) + "px"; };
  ta.addEventListener("input", grow);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#chat-form").requestSubmit(); }
  });
  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat(ta.value); });
  grow();
  if (!pending && !blocker) ta.focus();
}

/** Size the chat column to fill the window so the input stays pinned to the bottom. */
function fitChat() {
  const chat = $(".chat");
  if (chat) chat.style.height = `${Math.max(320, window.innerHeight - chat.getBoundingClientRect().top - 20)}px`;
}
window.addEventListener("resize", fitChat);

function chatMsg(role, content) {
  return role === "user"
    ? `<div class="msg user"><div class="who">You</div><div class="body">${esc(content)}</div></div>`
    : `<div class="msg assistant"><div class="who">Assistant</div><div class="prose">${md(content)}</div></div>`;
}

async function sendChat(text) {
  text = text.trim();
  const l = state.current;
  if (!text || state.streaming.chat) return;
  $("#chat-text").value = "";
  const s = (state.streaming.chat = { id: l.id, question: text, text: "" });
  renderPanel();
  try {
    await streamApi(`${apiBase()}/chat/stream`, { message: text }, (t) => {
      s.text += t;
      if (state.current?.id === s.id && state.tab === "chat") scheduleRender(() => {
        const el = $("#chat-live");
        if (!el) return;
        const logEl = $("#chat-log");
        const stick = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60;
        el.innerHTML = md(s.text);
        el.lastElementChild?.classList.add("caret");
        if (stick) logEl.scrollTop = logEl.scrollHeight;
      });
    });
  } catch (err) {
    toast(err.message, true);
    if (state.current?.id === s.id && $("#chat-text")) $("#chat-text").value = text;
  }
  state.streaming.chat = null;
  if (state.current?.id === s.id) await refreshCurrent(true);
}

// Quiz ------------------------------------------------------------------------------------

const VERDICT = { correct: ["Correct", "correct"], partial: ["Partly right", "partial"], incorrect: ["Not quite", "wrong"] };

function renderQuiz(panel) {
  const l = state.current;
  const blocker = aiBlocker();
  const q = state.quiz;
  const quiz = l.quizzes.find((x) => x.id === q.viewId) || (q.viewId === null ? l.quizzes[0] : null);
  if (quiz) q.viewId = quiz.id;
  const graded = quiz && quiz.answers && !q.retake;
  const written = quiz?.kind === "written";
  const draftKind = q.kind || "choice";

  const setup = `
    <form class="quiz-setup" id="quiz-form" ${blocker ? "hidden" : ""}>
      <label class="field"><span>Type</span><select class="input" name="kind">
        <option value="choice" ${draftKind === "choice" ? "selected" : ""}>Multiple choice</option>
        <option value="written" ${draftKind === "written" ? "selected" : ""}>Written answers</option></select></label>
      <label class="field"><span>Questions</span><select class="input" name="count">${[5, 10, 15, 20].map((n) => `<option ${n === (draftKind === "written" ? 5 : 10) ? "selected" : ""}>${n}</option>`).join("")}</select></label>
      <label class="field"><span>Difficulty</span><select class="input" name="difficulty"><option value="easy">Easy</option><option value="medium" selected>Medium</option><option value="hard">Hard</option></select></label>
      <label class="field grow"><span>Focus (optional)</span><input class="input" name="focus" placeholder="${state.kind === "course" ? "e.g. weeks 3 to 5, or the assigned readings" : "e.g. only the second half, or formulas"}" autocomplete="off"></label>
      <button class="btn btn-primary" ${q.busy ? "disabled" : ""}>${q.busy ? "Writing quiz…" : "New quiz"}</button>
    </form>`;

  const sourceLine = (qq) => qq.source ? `<div class="source">${state.kind === "lecture" ? md(`Covered at ${/^\[/.test(qq.source) ? qq.source : `[${qq.source}]`}`).replace(/^<p>|<\/p>\s*$/g, "") : `Source: ${esc(qq.source)}`}</div>` : "";

  let body = "";
  if (quiz) {
    const answers = graded ? quiz.answers : (q.answers[quiz.id] ||= []);
    const questions = quiz.questions.map((qq, i) => {
      if (written) {
        const g = graded ? quiz.grading?.[i] : null;
        const [label, cls] = g ? VERDICT[g.verdict] || VERDICT.incorrect : [];
        return `
          <div class="question">
            <div class="qh"><span class="qn">${i + 1}.</span><div class="qt">${mdInline(qq.question)}</div></div>
            ${graded
              ? `<div class="written-answer ${cls}">${answers[i] ? esc(answers[i]) : "<em>No answer</em>"}</div>
                 <div class="explain"><span class="verdict ${cls}">${label}</span><div class="prose">${md(g.feedback)}</div>
                   <details class="model"><summary>Model answer</summary><div class="prose">${md(qq.model_answer)}</div></details>
                   ${sourceLine(qq)}</div>`
              : `<textarea class="input written-input" data-q="${i}" rows="3" placeholder="Your answer">${esc(answers[i] || "")}</textarea>`}
          </div>`;
      }
      return `
        <div class="question">
          <div class="qh"><span class="qn">${i + 1}.</span><div class="qt">${mdInline(qq.question)}</div></div>
          ${qq.options.map((opt, j) => {
            let cls = "";
            if (graded) cls = j === qq.answer_index ? "correct" : answers[i] === j ? "wrong" : "";
            return `<label class="option ${cls}"><input type="radio" name="q${i}" value="${j}" ${answers[i] === j ? "checked" : ""} ${graded ? "disabled" : ""}><span>${mdInline(opt)}</span></label>`;
          }).join("")}
          ${graded ? `<div class="explain">${answers[i] == null ? "<em>Not answered.</em> " : ""}<div class="prose">${md(qq.explanation)}</div>${sourceLine(qq)}</div>` : ""}
        </div>`;
    }).join("");
    body = `
      ${graded ? `<p class="score"><strong>${fmtScore(quiz.score)} / ${quiz.questions.length}</strong> ${scoreWord(quiz.score / quiz.questions.length)}</p>` : ""}
      <div class="${graded ? "graded" : ""}" id="quiz-body">${questions}</div>
      <div class="toolbar" style="margin-top:12px">
        ${graded
          ? `<button class="btn" data-act="retake">Retake</button>`
          : `<button class="btn btn-primary" data-act="submit-quiz" ${q.grading ? "disabled" : ""}>${q.grading ? "Grading…" : written ? "Grade my answers" : "Check answers"}</button> <span class="muted small" id="quiz-progress"></span>`}
      </div>`;
  } else if (!blocker) {
    body = `<p class="muted">Generate a quiz to test yourself. Multiple choice checks recall quickly; written answers are graded by AI with feedback on what you missed.</p>`;
  }

  const history = l.quizzes.length > 1 || (l.quizzes.length === 1 && !quiz) ? `
    <div class="history">
      <h3 class="section-title">Past quizzes</h3>
      <table>${l.quizzes.map((x) => `
        <tr>
          <td>${fmtDate(x.created_at, true)}</td>
          <td class="muted">${x.kind === "written" ? "Written" : "Multiple choice"}, ${esc(x.difficulty)}, ${x.questions.length} questions</td>
          <td>${x.score != null ? `${fmtScore(x.score)} / ${x.questions.length}` : '<span class="muted">not taken</span>'}</td>
          <td>${x.id === q.viewId ? '<span class="muted">showing</span>' : `<button class="link" data-act="open-quiz" data-id="${x.id}">Open</button>`}</td>
          <td><button class="link quiet" data-act="delete-quiz" data-id="${x.id}">Delete</button></td>
        </tr>`).join("")}
      </table>
    </div>` : "";

  panel.innerHTML = blocker + setup + body + history;

  const form = $("#quiz-form");
  form.kind.addEventListener("change", () => { q.kind = form.kind.value; form.count.value = q.kind === "written" ? "5" : "10"; });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    makeQuiz(+form.count.value, form.difficulty.value, form.focus.value, form.kind.value);
  });
  const qb = $("#quiz-body");
  if (qb && !graded) {
    qb.addEventListener("change", (e) => {
      const m = e.target.name?.match(/^q(\d+)$/);
      if (m) { q.answers[quiz.id][+m[1]] = +e.target.value; updateQuizProgress(quiz); }
    });
    qb.addEventListener("input", (e) => {
      if (e.target.dataset.q != null) { q.answers[quiz.id][+e.target.dataset.q] = e.target.value; updateQuizProgress(quiz); }
    });
    updateQuizProgress(quiz);
  }
}

const fmtScore = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

function mdInline(s) {
  const html = md(s).trim();
  return html.replace(/^<p>([\s\S]*)<\/p>$/, "$1");
}

function scoreWord(r) {
  return r === 1 ? "Perfect." : r >= 0.8 ? "Strong." : r >= 0.6 ? "Getting there." : "Worth another pass through the material.";
}

const answered = (a) => a != null && String(a).trim() !== "";

function updateQuizProgress(quiz) {
  const el = $("#quiz-progress");
  if (!el) return;
  const n = (state.quiz.answers[quiz.id] || []).filter(answered).length;
  el.textContent = `${n} of ${quiz.questions.length} answered`;
}

async function makeQuiz(count, difficulty, focus, kind) {
  const l = state.current;
  state.quiz.busy = true;
  state.quiz.kind = kind;
  renderPanel();
  try {
    const quiz = await api(`${apiBase()}/quizzes`, { json: { count, difficulty, focus, kind } });
    if (state.current?.id === l.id) {
      state.current.quizzes.unshift(quiz);
      state.quiz.viewId = quiz.id;
      state.quiz.retake = false;
    }
  } catch (err) { toast(err.message, true); }
  state.quiz.busy = false;
  if (state.current?.id === l.id) { renderTabs(); if (state.tab === "quiz") renderPanel(); }
}

async function submitQuiz() {
  const quiz = state.current.quizzes.find((x) => x.id === state.quiz.viewId);
  const answers = quiz.questions.map((_, i) => state.quiz.answers[quiz.id]?.[i] ?? null);
  const missing = answers.filter((a) => !answered(a)).length;
  if (missing && !confirm(`${missing} question${missing > 1 ? "s are" : " is"} unanswered. Check anyway?`)) return;
  state.quiz.grading = true;
  if (quiz.kind === "written") renderPanel();
  try {
    const res = await api(`/api/quizzes/${quiz.id}/submit`, { json: { answers } });
    Object.assign(quiz, res);
    state.quiz.retake = false;
    delete state.quiz.answers[quiz.id];
    $("#view").scrollTo({ top: 0 });
  } catch (err) { toast(err.message, true); }
  state.quiz.grading = false;
  if (state.tab === "quiz") renderPanel();
}

// Flashcards ----------------------------------------------------------------------------

function newDeck(cards) {
  return { order: cards.map((_, i) => i), pos: 0, flipped: false, known: 0, total: cards.length };
}

function renderCards(panel) {
  const l = state.current;
  const blocker = aiBlocker();
  const cards = l.flashcards;
  if (cards.length && (!state.deck || state.deck.total !== cards.length)) state.deck = newDeck(cards);
  const d = state.deck;

  const setup = `
    <div class="toolbar" ${blocker ? "hidden" : ""}>
      <select class="input" id="card-count" style="width:auto">${[10, 20, 30, 40].map((n) => `<option value="${n}" ${n === 20 ? "selected" : ""}>${n} cards</option>`).join("")}</select>
      <button class="btn ${cards.length ? "" : "btn-primary"}" data-act="gen-cards" ${state.cardsBusy ? "disabled" : ""}>${state.cardsBusy ? "Making cards…" : cards.length ? "Make a new deck" : "Make flashcards"}</button>
      <span class="spacer"></span>
      ${cards.length ? '<button class="btn btn-sm" data-act="shuffle">Shuffle</button> <button class="btn btn-sm" data-act="restart-deck">Restart</button>' : ""}
    </div>`;

  let deck = "";
  if (cards.length && d.pos < d.order.length) {
    const c = cards[d.order[d.pos]];
    deck = `
      <div class="deck">
        <div class="card" data-act="flip" title="Click or press Space to flip">
          <span class="side">${d.flipped ? "Answer" : "Question"}</span>
          ${d.flipped ? `<div class="prose">${md(c.back)}</div>${c.source ? `<div class="source">${esc(c.source)}</div>` : ""}` : `<div class="front">${mdInline(c.front)}</div>`}
        </div>
        <div class="deck-nav">
          <button class="btn" data-act="card-prev" ${d.pos === 0 ? "disabled" : ""}>Back</button>
          <span class="pos">${d.pos + 1} / ${d.order.length}</span>
          ${d.flipped
            ? '<button class="btn" data-act="card-again">Again</button><button class="btn btn-primary" data-act="card-known">Got it</button>'
            : '<button class="btn btn-primary" data-act="flip">Show answer</button>'}
          <span class="spacer"></span>
          <span class="muted small"><kbd>Space</kbd> flip, <kbd>1</kbd> again, <kbd>2</kbd> got it</span>
        </div>
      </div>`;
  } else if (cards.length) {
    deck = `<div class="notice">Deck finished: ${d.known} of ${d.total} marked as known. <button class="link" data-act="restart-deck">Go again</button></div>`;
  } else if (!blocker) {
    deck = `<p class="muted">Make a deck of flashcards from this ${state.kind} and review them here.</p>`;
  }

  const list = cards.length ? `
    <div class="card-list">
      <h3 class="section-title">All cards</h3>
      ${cards.map((c) => `<details><summary>${mdInline(c.front)}</summary><div class="prose">${md(c.back)}</div></details>`).join("")}
    </div>` : "";

  panel.innerHTML = blocker + setup + deck + list;
}

function deckStep(action) {
  const d = state.deck;
  if (!d) return;
  if (action === "flip") d.flipped = !d.flipped;
  else if (action === "prev" && d.pos > 0) { d.pos--; d.flipped = false; }
  else if (action === "again") { d.order.push(d.order[d.pos]); d.pos++; d.flipped = false; }
  else if (action === "known") { d.known++; d.pos++; d.flipped = false; }
  renderPanel();
}

async function makeCards() {
  const l = state.current;
  if (l.flashcards.length && !confirm("Replace the current deck?")) return;
  state.cardsBusy = true;
  const count = +($("#card-count")?.value || 20);
  renderPanel();
  try {
    const cards = await api(`${apiBase()}/flashcards`, { json: { count } });
    if (state.current?.id === l.id) { state.current.flashcards = cards; state.deck = newDeck(cards); }
  } catch (err) { toast(err.message, true); }
  state.cardsBusy = false;
  if (state.current?.id === l.id) { renderTabs(); if (state.tab === "cards") renderPanel(); }
}

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("#search").focus(); return; }
  if (state.tab !== "cards" || !state.current || !state.deck) return;
  if (e.target.closest("input, textarea, select") || document.querySelector("dialog[open]")) return;
  if (e.key === " ") { e.preventDefault(); deckStep("flip"); }
  else if (e.key === "ArrowLeft") deckStep("prev");
  else if (e.key === "1" && state.deck.flipped) deckStep("again");
  else if ((e.key === "2" || e.key === "ArrowRight") && state.deck.flipped) deckStep("known");
});

// Course view -------------------------------------------------------------------------------

function renderCourse() {
  const c = state.current;
  $("#view").innerHTML = `
    <header class="lecture-head">
      <button class="btn btn-ghost back" data-act="back" aria-label="Back to library">Library</button>
      <div class="titles">
        <input class="title-input" id="title-input" value="${esc(c.name)}" aria-label="Course name">
        <div class="meta" id="lecture-meta"></div>
      </div>
      <div class="head-actions">
        <button class="btn btn-sm btn-ghost" data-act="delete-course">Delete course</button>
      </div>
    </header>
    <nav class="tabs" id="tabs"></nav>
    <section class="panel" id="panel"></section>`;
  renderCourseMeta();
  renderTabs();
  renderPanel();
  const title = $("#title-input");
  title.addEventListener("change", async () => {
    const name = title.value.trim();
    if (!name) { title.value = c.name; return; }
    try {
      await api(apiBase(), { method: "PATCH", json: { name } });
      c.name = name;
      loadLectures();
    } catch (err) { title.value = c.name; toast(err.message, true); }
  });
  title.addEventListener("keydown", (e) => { if (e.key === "Enter") title.blur(); });
}

function renderCourseMeta() {
  const c = state.current;
  const el = $("#lecture-meta");
  if (!el || state.kind !== "course") return;
  const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
  el.innerHTML = `<span>${plural(c.lectures.length, "lecture")}</span><span>${plural(c.materials.length, "file")}</span>`;
}

const KIND_LABEL = { pdf: "PDF", pdf_scan: "Scanned PDF", slides: "Slides", document: "Word", text: "Text", image: "Image" };

function fmtSize(bytes) {
  return bytes > 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function fmtTokens(n) {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

function renderOverview(panel) {
  const c = state.current;
  const excluded = new Set(c.excluded);
  const src = courseSources();
  const model = state.settings?.model || "";
  const limit = model.startsWith("claude-haiku") ? 180000 : 900000;
  const cost = src.tokens > 150000
    ? `<div class="notice ${src.tokens > limit ? "error" : ""}" style="margin-bottom:16px">${src.tokens > limit
        ? `The selected material is about ${fmtTokens(src.tokens)} tokens, more than the model can read at once. Untick some lectures or files below.`
        : `The selected material is about ${fmtTokens(src.tokens)} tokens. Course-wide requests will be slower and cost more; untick what you don't need.`}</div>` : "";
  const check = (key) => `<input type="checkbox" class="include" data-key="${key}" ${excluded.has(key) ? "" : "checked"} title="Include in course chat, quizzes and flashcards">`;

  panel.innerHTML = `
    <p class="muted" style="margin-top:0">Everything ticked here is used by this course's Chat, Quiz and Flashcards. ${src.tokens ? `Selected: about ${fmtTokens(src.tokens)} tokens.` : ""}</p>
    ${cost}
    <h3 class="section-title">Lectures</h3>
    ${c.lectures.length ? `<div class="rows">${c.lectures.map((l) => `
      <div class="row">
        ${l.tokens ? check(`lecture:${l.id}`) : '<input type="checkbox" disabled title="No transcript yet">'}
        <a class="grow" href="#/lecture/${l.id}">${esc(l.title)}</a>
        <span class="muted small">${fmtDate(l.created_at)}${l.duration ? `, ${fmtDuration(l.duration)}` : ""}</span>
        <span class="muted small mono">${l.tokens ? `${fmtTokens(l.tokens)} tokens` : "no transcript"}</span>
      </div>`).join("")}</div>`
      : `<p class="muted">No lectures in this course yet. When you record, set the course to "${esc(c.name)}".</p>`}

    <h3 class="section-title" style="margin-top:26px">Files</h3>
    <label class="dropzone" id="dropzone">
      <input type="file" id="material-input" multiple accept="${c.accepted.join(",")}">
      <strong>Add files</strong> <span class="muted">or drop them here. PDF, PowerPoint (.pptx), Word (.docx), text, or photos of handouts and whiteboards.</span>
    </label>
    <div id="upload-status" class="muted small"></div>
    ${c.materials.length ? `<div class="rows">${c.materials.map((m) => `
      <div class="row">
        ${check(`material:${m.id}`)}
        <a class="grow" href="/api/materials/${m.id}/file" target="_blank" rel="noopener">${esc(m.filename)}</a>
        <span class="muted small">${KIND_LABEL[m.kind] || m.kind}${m.pages && m.kind !== "image" ? `, ${m.pages} ${m.kind === "slides" ? "slide" : "page"}${m.pages === 1 ? "" : "s"}` : ""}, ${fmtSize(m.size)}</span>
        <span class="muted small mono" title="${m.kind === "pdf_scan" || m.kind === "image" ? "Read as images by the AI" : "Size of the extracted text"}">${fmtTokens(m.tokens)} tokens</span>
        <button class="link quiet small" data-act="delete-material" data-id="${m.id}">Remove</button>
      </div>`).join("")}</div>` : ""}`;

  panel.querySelectorAll(".include").forEach((box) => box.addEventListener("change", saveExcluded));
  $("#material-input").addEventListener("change", (e) => uploadMaterials([...e.target.files]));
  const dz = $("#dropzone");
  dz.addEventListener("dragover", (e) => { e.preventDefault(); dz.classList.add("over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("over"));
  dz.addEventListener("drop", (e) => { e.preventDefault(); dz.classList.remove("over"); uploadMaterials([...e.dataTransfer.files]); });
}

async function saveExcluded() {
  const excluded = $$(".include").filter((b) => !b.checked).map((b) => b.dataset.key);
  try {
    await api(apiBase(), { method: "PATCH", json: { excluded } });
    state.current.excluded = excluded;
    renderPanel();
  } catch (err) { toast(err.message, true); }
}

async function uploadMaterials(files) {
  const c = state.current;
  const status = $("#upload-status");
  let done = 0;
  const failed = [];
  for (const file of files) {
    if (status) status.textContent = `Reading ${file.name} (${done + 1} of ${files.length})…`;
    const form = new FormData();
    form.append("file", file);
    try { await api(`/api/courses/${c.id}/materials`, { form }); }
    catch (err) { failed.push(`${file.name}: ${err.message}`); }
    done++;
  }
  if (failed.length) toast(failed.join("  "), true);
  else toast(`Added ${files.length} file${files.length > 1 ? "s" : ""}.`);
  if (state.current?.id === c.id) await refreshCurrent(true);
  loadLectures();
}

// Actions ---------------------------------------------------------------------------------

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied."); }
  catch { toast("Could not copy.", true); }
}

const actions = {
  back: () => { location.hash = ""; },
  "open-course": () => { if (state.current.course_id) { state.tab = "overview"; location.hash = `#/course/${state.current.course_id}`; } },
  "new-course": async () => {
    const name = prompt("Course name, e.g. PHYS 201")?.trim();
    if (!name) return;
    const course = await api("/api/courses", { json: { name } });
    await loadLectures();
    state.tab = "overview";
    location.hash = `#/course/${course.id}`;
  },
  "delete-course": async () => {
    const c = state.current;
    if (!confirm(`Delete the course "${c.name}" and its uploaded files? Its lectures are kept, without a course.`)) return;
    await api(apiBase(), { method: "DELETE" });
    location.hash = "";
    loadLectures();
  },
  "delete-material": async (el) => {
    if (!confirm("Remove this file from the course?")) return;
    await api(`/api/materials/${el.dataset.id}`, { method: "DELETE" });
    refreshCurrent(true);
    loadLectures();
  },
  phone: () => openPhone(),
  new: () => openNewDialog(),
  import: () => openImportDialog(),
  settings: () => openSettings(),
  export: () => { location.href = `/api/lectures/${state.current.id}/export`; },
  delete: async () => {
    const l = state.current;
    if (state.recorder?.lectureId === l.id) { toast("Stop the recording first.", true); return; }
    if (!confirm(`Delete "${l.title}" and its recording? This cannot be undone.`)) return;
    await api(`/api/lectures/${l.id}`, { method: "DELETE" });
    location.hash = "";
    loadLectures();
  },
  paste: async () => {
    const text = $("#paste-text").value.trim();
    if (!text) return;
    await api(`/api/lectures/${state.current.id}/transcript`, { json: { text } });
    refreshCurrent(true);
  },
  "retry-seg": async (el) => { await api(`/api/segments/${el.dataset.id}/retry`, { json: {} }); refreshCurrent(true); },
  "copy-transcript": () => copy(transcriptLines().filter((l) => l.kind === "text").map((l) => `[${fmtTime(l.t)}] ${l.text}`).join("\n")),
  "gen-notes": () => generateNotes(),
  "edit-notes": () => { state.editingNotes = true; renderPanel(); },
  "cancel-notes": () => { state.editingNotes = false; renderPanel(); },
  "save-notes": async () => {
    const notes = $("#notes-editor").value;
    await saveField("notes", notes);
    state.current.notes_status = notes.trim() ? "done" : "none";
    state.editingNotes = false;
    renderPanel();
  },
  "copy-notes": () => copy(state.current.notes),
  "find-topics": () => findTopics(),
  "expand-topic": (el) => { const t = state.current.topics[+el.dataset.i]; expand(`${t.title}: ${t.summary}`); },
  "copy-expansion": (el) => copy(state.current.expansions.find((x) => x.id === +el.dataset.id)?.content || ""),
  "delete-expansion": async (el) => {
    if (!confirm("Delete this deep dive?")) return;
    await api(`/api/expansions/${el.dataset.id}`, { method: "DELETE" });
    refreshCurrent(true);
  },
  suggest: (el) => sendChat(el.textContent),
  "clear-chat": async () => {
    if (!confirm("Clear this conversation?")) return;
    await api(`${apiBase()}/chat`, { method: "DELETE" });
    refreshCurrent(true);
  },
  "submit-quiz": () => submitQuiz(),
  retake: () => { state.quiz.retake = true; state.quiz.answers[state.quiz.viewId] = []; renderPanel(); $("#view").scrollTo({ top: 0 }); },
  "open-quiz": (el) => { state.quiz.viewId = +el.dataset.id; state.quiz.retake = false; renderPanel(); $("#view").scrollTo({ top: 0 }); },
  "delete-quiz": async (el) => {
    if (!confirm("Delete this quiz?")) return;
    await api(`/api/quizzes/${el.dataset.id}`, { method: "DELETE" });
    if (state.quiz.viewId === +el.dataset.id) state.quiz.viewId = null;
    refreshCurrent(true);
  },
  "gen-cards": () => makeCards(),
  flip: () => deckStep("flip"),
  "card-prev": () => deckStep("prev"),
  "card-again": () => deckStep("again"),
  "card-known": () => deckStep("known"),
  shuffle: () => {
    const d = state.deck;
    const rest = d.order.slice(d.pos);
    for (let i = rest.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [rest[i], rest[j]] = [rest[j], rest[i]]; }
    d.order = d.order.slice(0, d.pos).concat(rest);
    d.flipped = false;
    renderPanel();
  },
  "restart-deck": () => { state.deck = newDeck(state.current.flashcards); renderPanel(); },
};

// Wrap actions so a failed request shows a message instead of failing silently.
for (const [k, fn] of Object.entries(actions)) {
  actions[k] = async (...args) => { try { await fn(...args); } catch (err) { toast(err.message, true); } };
}
document.addEventListener("click", (e) => {
  const act = e.target.closest("[data-act]");
  if (act && !act.closest("#view")) actions[act.dataset.act]?.(act, e);
});

// Keeping the open lecture fresh ------------------------------------------------------------

function needsRefresh(l) {
  return l && (l.status !== "ready" || l.pending_segments > 0 || l.notes_status === "generating" || state.recorder?.lectureId === l.id);
}

async function refreshCurrent(force = false) {
  const l = state.current;
  if (!l || (!force && (state.kind !== "lecture" || !needsRefresh(l)))) return;
  let fresh;
  try { fresh = await api(apiBase()); } catch { return; }
  if (state.current?.id !== l.id) return;
  if (state.kind === "course") {
    state.current = fresh;
    renderCourseMeta();
    renderTabs();
    renderPanel();
    return;
  }
  const changed = JSON.stringify([fresh.segments, fresh.status, fresh.notes_status, fresh.notes, fresh.duration, fresh.title]) !==
                  JSON.stringify([l.segments, l.status, l.notes_status, l.notes, l.duration, l.title]);
  state.current = fresh;
  renderMeta();
  renderTabs();
  const busyHere = Object.values(state.streaming).some((s) => s?.id === l.id);
  if (force || (changed && !state.editingNotes && !busyHere && ["transcript", "notes"].includes(state.tab))) renderPanel();
  if (changed) loadLectures();
}

setInterval(() => { pollStatus(); refreshCurrent(); }, 2000);

// Recording --------------------------------------------------------------------------------

class Recorder {
  constructor(stream, lectureId, segSeconds) {
    this.stream = stream;
    this.lectureId = lectureId;
    this.segSeconds = segSeconds;
    this.idx = 0;
    this.accum = 0;
    this.resumedAt = performance.now();
    this.paused = false;
    this.mr = null;
    this.uploads = new Set();
    this.failed = 0;
    this.mime = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
    this.levels = new Array(80).fill(0);
  }

  elapsed() {
    return this.accum + (this.paused ? 0 : (performance.now() - this.resumedAt) / 1000);
  }

  start() {
    this.ctx = new AudioContext();
    this.ctx.resume?.().catch(() => {});
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.ctx.createMediaStreamSource(this.stream).connect(this.analyser);
    this.buf = new Float32Array(this.analyser.fftSize);
    this.startSegment();
    this.raf = requestAnimationFrame(() => this.draw());
    this.clock = setInterval(() => this.updateBar(), 250);
    navigator.wakeLock?.request("screen").then((l) => (this.wake = l)).catch(() => {});
  }

  startSegment() {
    const mr = new MediaRecorder(this.stream, { ...(this.mime && { mimeType: this.mime }), audioBitsPerSecond: 64000 });
    const chunks = [];
    const meta = { idx: this.idx++, start: this.elapsed(), t0: performance.now() };
    mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    mr.onstop = () => {
      meta.duration = (performance.now() - meta.t0) / 1000;
      const blob = new Blob(chunks, { type: mr.mimeType || this.mime || "audio/webm" });
      if (blob.size > 0 && meta.duration > 0.5) this.upload(meta, blob);
      mr.done?.();
    };
    mr.start();
    this.mr = mr;
    clearTimeout(this.segTimer);
    this.segTimer = setTimeout(() => this.rotate(), this.segSeconds * 1000);
  }

  rotate() {
    if (this.paused || !this.mr) return;
    const old = this.mr;
    this.startSegment();   // start the next chunk first so no audio falls in the gap
    old.stop();
  }

  stopSegment() {
    clearTimeout(this.segTimer);
    const mr = this.mr;
    this.mr = null;
    if (!mr || mr.state === "inactive") return Promise.resolve();
    return new Promise((resolve) => { mr.done = resolve; mr.stop(); });
  }

  upload(meta, blob) {
    const ext = blob.type.includes("ogg") ? "ogg" : blob.type.includes("mp4") ? "mp4" : "webm";
    const send = async (attempt = 0) => {
      const form = new FormData();
      form.append("audio", blob, `seg${meta.idx}.${ext}`);
      form.append("idx", meta.idx);
      form.append("start", meta.start.toFixed(2));
      form.append("duration", meta.duration.toFixed(2));
      try {
        await api(`/api/lectures/${this.lectureId}/segments`, { form });
        if (attempt) this.failed--;
      } catch (err) {
        if (!attempt) this.failed++;
        this.updateBar();
        await new Promise((r) => setTimeout(r, Math.min(30000, 2000 * 2 ** attempt)));
        return send(attempt + 1);
      }
    };
    const p = send().finally(() => { this.uploads.delete(p); this.updateBar(); });
    this.uploads.add(p);
    this.updateBar();
  }

  async pause() {
    if (this.paused) return;
    this.accum = this.elapsed();
    this.paused = true;
    await this.stopSegment();
    this.updateBar();
  }

  resume() {
    if (!this.paused) return;
    this.paused = false;
    this.resumedAt = performance.now();
    this.startSegment();
    this.updateBar();
  }

  async stop() {
    const duration = this.elapsed();
    this.paused = true;
    this.accum = duration;
    await this.stopSegment();
    cancelAnimationFrame(this.raf);
    clearInterval(this.clock);
    this.stream.getTracks().forEach((t) => t.stop());
    this.ctx?.close();
    this.wake?.release().catch(() => {});
    $("#rec-upload").textContent = this.uploads.size ? "Saving last part…" : "";
    await Promise.all([...this.uploads]);
    await api(`/api/lectures/${this.lectureId}/finish`, { json: { duration } });
  }

  updateBar() {
    $("#rec-time").textContent = fmtTime(this.elapsed());
    $("#recbar").classList.toggle("paused", this.paused);
    $("#rec-pause").textContent = this.paused ? "Resume" : "Pause";
    $("#rec-upload").textContent = this.failed ? "Can't reach the app server, retrying. Audio is kept until it's saved." : this.paused ? "Paused" : "";
  }

  draw() {
    this.raf = requestAnimationFrame(() => this.draw());
    this.analyser.getFloatTimeDomainData(this.buf);
    let sum = 0;
    for (const v of this.buf) sum += v * v;
    const rms = Math.sqrt(sum / this.buf.length);
    this.levels.push(this.paused ? 0 : Math.min(1, rms * 4));
    this.levels.shift();
    const c = $("#rec-meter");
    const g = c.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    if (c.width !== 160 * dpr) { c.width = 160 * dpr; c.height = 20 * dpr; }
    g.clearRect(0, 0, c.width, c.height);
    g.fillStyle = getComputedStyle(document.body).color;
    const w = c.width / this.levels.length;
    this.levels.forEach((lv, i) => {
      const h = Math.max(1 * dpr, lv * c.height);
      g.fillRect(i * w, (c.height - h) / 2, Math.max(1, w - dpr), h);
    });
  }
}

async function listMics() {
  let devices = await navigator.mediaDevices.enumerateDevices();
  if (devices.some((d) => d.kind === "audioinput" && !d.label)) {
    // Labels are hidden until the user grants microphone access once.
    try {
      const s = await navigator.mediaDevices.getUserMedia({ audio: true });
      s.getTracks().forEach((t) => t.stop());
      devices = await navigator.mediaDevices.enumerateDevices();
    } catch {}
  }
  return devices.filter((d) => d.kind === "audioinput");
}

async function openNewDialog() {
  if (state.recorder) { toast("Already recording. Stop the current recording first.", true); return; }
  if (!navigator.mediaDevices?.getUserMedia) { toast("This window can't access a microphone.", true); return; }
  const dlg = $("#dlg-new");
  const form = $("#form-new");
  form.reset();
  if (state.current?.course) form.course.value = state.current.course;
  form.title.value = `Lecture ${new Date().toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
  dlg.showModal();
  form.title.select();
  const mics = await listMics();
  const saved = localStorage.getItem("mic");
  $("#mic-select").innerHTML = mics.length
    ? mics.map((m) => `<option value="${esc(m.deviceId)}" ${m.deviceId === saved ? "selected" : ""}>${esc(m.label || "Microphone")}</option>`).join("")
    : '<option value="">Default microphone</option>';
}

$("#form-new").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  const f = e.target;
  const deviceId = f.mic.value;
  try { localStorage.setItem("mic", deviceId); } catch {}
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { ...(deviceId && { deviceId: { exact: deviceId } }), echoCancellation: false, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (err) {
    toast(`Microphone unavailable: ${err.message}`, true);
    return;
  }
  try {
    const lec = await api("/api/lectures", { json: { title: f.title.value, course: f.course.value } });
    const rec = new Recorder(stream, lec.id, state.settings?.segment_seconds || 30);
    state.recorder = rec;
    rec.start();
    document.body.classList.add("recording");
    if (state.engine?.on_phone) toast("Keep this screen on and the app open. iPhone pauses recording if the screen locks or you switch apps.");
    $("#recbar").hidden = false;
    $("#rec-goto").textContent = lec.title;
    rec.updateBar();
    state.tab = "transcript";
    location.hash = `#/lecture/${lec.id}`;
    loadLectures();
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    toast(err.message, true);
  }
});

$("#rec-pause").addEventListener("click", () => {
  const r = state.recorder;
  if (r) r.paused ? r.resume() : r.pause();
});

$("#rec-stop").addEventListener("click", async () => {
  const r = state.recorder;
  if (!r) return;
  $("#rec-stop").disabled = $("#rec-pause").disabled = true;
  try {
    await r.stop();
    toast(state.settings?.auto_notes && state.engine?.ai_ready
      ? "Recording saved. Notes will be written once transcription finishes."
      : "Recording saved.");
  } catch (err) {
    toast(err.message, true);
  }
  state.recorder = null;
  document.body.classList.remove("recording");
  $("#recbar").hidden = true;
  $("#rec-stop").disabled = $("#rec-pause").disabled = false;
  loadLectures();
  refreshCurrent(true);
});

$("#rec-goto").addEventListener("click", () => {
  if (state.recorder) { state.tab = "transcript"; location.hash = `#/lecture/${state.recorder.lectureId}`; }
});

window.addEventListener("beforeunload", (e) => {
  if (state.recorder) { e.preventDefault(); e.returnValue = ""; }
});

// Import -------------------------------------------------------------------------------------

let importMode = "audio";
function openImportDialog() {
  const form = $("#form-import");
  form.reset();
  setImportMode("audio");
  $("#dlg-import").showModal();
}

function setImportMode(mode) {
  importMode = mode;
  $$("#form-import .seg").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $$("#form-import [data-for]").forEach((el) => (el.hidden = !el.dataset.for.split(" ").includes(mode)));
  $("#form-import [name=course]").placeholder = mode === "files" ? "Required, e.g. PHYS 201" : "Optional";
}
$$("#form-import .seg").forEach((b) => b.addEventListener("click", () => setImportMode(b.dataset.mode)));

$("#form-import").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  const f = e.target;
  try {
    let lec;
    if (importMode === "files") {
      const files = [...f.materials.files];
      const name = f.course.value.trim();
      if (!files.length || !name) { e.preventDefault(); toast(name ? "Choose at least one file." : "Enter the course these files belong to.", true); return; }
      const course = await api("/api/courses", { json: { name } });
      state.tab = "overview";
      location.hash = `#/course/${course.id}`;
      await route();
      await uploadMaterials(files);
      return;
    }
    if (importMode === "audio") {
      const file = f.file.files[0];
      if (!file) { e.preventDefault(); toast("Choose a file to import.", true); return; }
      toast("Uploading…");
      const form = new FormData();
      form.append("file", file);
      form.append("title", f.title.value);
      form.append("course", f.course.value);
      lec = await api("/api/import", { form });
      toast("Imported. Transcribing in the background.");
    } else {
      const text = f.text.value.trim();
      if (!text) { e.preventDefault(); toast("Paste some text first.", true); return; }
      lec = await api("/api/lectures", { json: { title: f.title.value || "Pasted transcript", course: f.course.value } });
      await api(`/api/lectures/${lec.id}/transcript`, { json: { text } });
    }
    state.tab = importMode === "audio" ? "transcript" : "notes";
    location.hash = `#/lecture/${lec.id}`;
    loadLectures();
  } catch (err) { toast(err.message, true); }
});

// Phone access -----------------------------------------------------------------------------

let phoneTimer;
async function openPhone() {
  const dlg = $("#dlg-phone");
  if (!dlg.open) dlg.showModal();
  await renderPhone();
  clearInterval(phoneTimer);
  phoneTimer = setInterval(() => (dlg.open ? renderPhone() : clearInterval(phoneTimer)), 5000);
}

async function renderPhone() {
  const p = await api("/api/phone");
  $("#phone-toggle").checked = p.enabled;
  const body = $("#phone-body");
  const devices = p.devices.length ? `
    <h3>Paired devices</h3>
    <ul class="devices">${p.devices.map((d) => `
      <li><span>${esc(d.name)}</span><span class="muted small">last used ${fmtDate(d.last_seen, true)}</span>
      <button type="button" class="link quiet small" data-forget="${d.id}">Remove</button></li>`).join("")}</ul>` : "";
  if (!p.enabled) { body.innerHTML = devices; return; }
  if (p.error || !p.running) {
    body.innerHTML = `<div class="notice error">${esc(p.error || "Starting…")}</div>${devices}`;
    return;
  }
  const minutes = Math.max(1, Math.round((p.code_expires - Date.now() / 1000) / 60));
  body.innerHTML = `
    <div class="phone-setup">
      <div class="qr">${p.qr_svg}</div>
      <ol>
        <li>Open the <b>Camera</b> on your iPhone and point it at this code. Tap the link that appears.</li>
        <li>Follow the steps on the phone: install and trust a certificate (one time only), then open the app.</li>
        <li>In Safari tap <b>Share</b>, then <b>Add to Home Screen</b>.</li>
        <li>If Windows asks whether to allow Lecture Recorder on the network, choose <b>Allow</b> for private networks.</li>
      </ol>
    </div>
    <p class="muted small">This code works for ${minutes} more minute${minutes > 1 ? "s" : ""}. Once paired, a phone stays paired until you remove it.
      Address: <span class="mono">${esc(p.app_url)}</span>${p.ips.length > 1 ? ` (also ${p.ips.slice(1).map(esc).join(", ")})` : ""}</p>
    ${devices}`;
}

$("#phone-toggle").addEventListener("change", async (e) => {
  try {
    $("#phone-body").innerHTML = e.target.checked ? '<p class="muted">Starting…</p>' : "";
    await api("/api/phone", { method: "PUT", json: { enabled: e.target.checked } });
    await renderPhone();
  } catch (err) { toast(err.message, true); }
});

$("#phone-body").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-forget]");
  if (!btn || !confirm("Remove this device? It will need to scan the code again to connect.")) return;
  await api(`/api/phone/devices/${btn.dataset.forget}`, { method: "DELETE" });
  renderPhone();
});

// Settings -------------------------------------------------------------------------------------

async function loadSettings() {
  state.settings = await api("/api/settings");
}

async function openSettings() {
  await loadSettings();
  const s = state.settings;
  const f = $("#form-settings");
  f.api_key.value = "";
  $("#key-hint").innerHTML = s.has_api_key
    ? `A key is saved (${esc(s.api_key_hint)}). Leave blank to keep it. ${s.api_key_hint !== "from environment" ? '<button type="button" class="link" id="remove-key">Remove</button>' : ""}`
    : 'Create one at console.anthropic.com. It is stored only on this computer.';
  f.model.innerHTML = Object.entries(s.models).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  f.model.value = s.model;
  f.whisper_model.innerHTML = s.whisper_models.map((m) => `<option>${m}</option>`).join("");
  f.whisper_model.value = s.whisper_model;
  f.whisper_device.value = s.whisper_device;
  f.language.value = s.language;
  f.segment_seconds.value = s.segment_seconds;
  f.auto_notes.checked = s.auto_notes;
  $("#data-dir").textContent = s.data_dir;
  $("#app-version").textContent = state.engine?.version || "unknown";
  $("#remove-key")?.addEventListener("click", async () => {
    await api("/api/settings", { method: "PUT", json: { api_key: "" } });
    await loadSettings();
    pollStatus();
    openSettings();
  });
  if (!$("#dlg-settings").open) $("#dlg-settings").showModal();
}

$("#form-settings").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "ok") return;
  const f = e.target;
  const body = {
    model: f.model.value,
    whisper_model: f.whisper_model.value,
    whisper_device: f.whisper_device.value,
    language: f.language.value.trim(),
    segment_seconds: +f.segment_seconds.value || 30,
    auto_notes: f.auto_notes.checked,
  };
  if (f.api_key.value.trim()) body.api_key = f.api_key.value.trim();
  try {
    state.settings = await api("/api/settings", { method: "PUT", json: body });
    await pollStatus();
    toast("Settings saved.");
    if (state.current) renderPanel(); else renderEmpty();
  } catch (err) { toast(err.message, true); }
});

$("#btn-new").addEventListener("click", () => actions.new());
$("#btn-import").addEventListener("click", () => actions.import());
$("#btn-settings").addEventListener("click", () => actions.settings());

// Start ------------------------------------------------------------------------------------------

(async function init() {
  await Promise.all([loadSettings(), pollStatus(), loadLectures()]);
  await route();
  if (!state.settings.has_api_key && !localStorage.getItem("seen-settings")) {
    try { localStorage.setItem("seen-settings", "1"); } catch {}
    openSettings();
  }
})();
