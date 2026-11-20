// Live Briefing client: gapless playback, voice-activity barge-in, push-to-talk.
//
// Audio out: the server streams PCM16 per sentence ("chunk"). Each binary frame starts
// with a uint32 sequence number. We schedule buffers back to back on one GainNode,
// which lets us duck (listener starts talking) and fade out (interruption confirmed)
// smoothly instead of chopping mid-syllable.
//
// Audio in: an AudioWorklet gives 16 kHz PCM16 frames + RMS. A small energy VAD with an
// adaptive noise floor decides when the listener is speaking. While the narrator is
// talking the VAD needs a louder, longer onset, which keeps echo from triggering it.

const $ = (sel) => document.querySelector(sel);
const STATE_LABELS = {
  idle: "Tap to start",
  narrating: "Speaking. Interrupt any time",
  suspect: "Listening…",
  listening: "Listening…",
  thinking: "Thinking…",
  researching: "Checking with the research desk…",
  answering: "Answering",
  awaiting_follow_up: "Ask a follow-up, or I'll carry on",
  paused: "Paused. Say “carry on” or press Resume",
  ended: "Briefing finished. Ask anything",
};

// ---------------------------------------------------------------------------
// Player
// ---------------------------------------------------------------------------
class Player {
  constructor(onEvent) {
    this.onEvent = onEvent; // (event, seq, meta)
    this.ctx = null;
    this.gain = null;
    this.chunks = new Map(); // seq -> {meta, sources[], start, end, ended:boolean, timers[]}
    this.minSeq = 0;
    this.maxSeq = 0;
    this.nextTime = 0;
    this.ducked = false;
  }

  async init() {
    if (this.ctx) return;
    try {
      this.ctx = new AudioContext({ sampleRate: 24000, latencyHint: "interactive" });
    } catch {
      this.ctx = new AudioContext({ latencyHint: "interactive" });
    }
    this.gain = this.ctx.createGain();
    this.gain.connect(this.ctx.destination);
    await this.ctx.resume();
  }

  get speaking() {
    return !!this.ctx && this.nextTime > this.ctx.currentTime + 0.02;
  }

  chunkStart(meta) {
    if (meta.seq < this.minSeq) return;
    this.maxSeq = Math.max(this.maxSeq, meta.seq);
    this.chunks.set(meta.seq, { meta, sources: [], start: null, end: 0, done: false, timers: [] });
  }

  audio(seq, int16) {
    const c = this.chunks.get(seq);
    if (!c || seq < this.minSeq || !int16.length) return;
    const rate = c.meta.sample_rate || 24000;
    const buf = this.ctx.createBuffer(1, int16.length, rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < int16.length; i++) ch[i] = int16[i] / 32768;
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    const now = this.ctx.currentTime;
    if (this.nextTime < now + 0.03) this.nextTime = now + 0.08; // small jitter cushion after underrun
    src.start(this.nextTime);
    if (c.start === null) {
      c.start = this.nextTime;
      c.timers.push(setTimeout(() => this.onEvent("started", seq, c.meta), Math.max(0, (c.start - now) * 1000)));
    }
    this.nextTime += buf.duration;
    c.end = this.nextTime;
    c.sources.push(src);
  }

  chunkEnd(seq) {
    const c = this.chunks.get(seq);
    if (!c || seq < this.minSeq) return;
    c.done = true;
    const now = this.ctx.currentTime;
    if (c.start === null) {
      // no audio (synthesis failed): still report so the server's flow control moves on
      this.onEvent("started", seq, c.meta);
      this.onEvent("ended", seq, c.meta);
      this.chunks.delete(seq);
      return;
    }
    // breathing room after the sentence, part of the chunk's time on air
    this.nextTime = Math.max(this.nextTime, c.end) + (c.meta.pause_after_ms || 0) / 1000;
    const endAt = this.nextTime;
    c.timers.push(setTimeout(() => {
      this.onEvent("ended", seq, c.meta);
      this.chunks.delete(seq);
    }, Math.max(0, (endAt - now) * 1000)));
  }

  ramp(value, seconds) {
    if (!this.ctx) return;
    const g = this.gain.gain;
    const t = this.ctx.currentTime;
    g.cancelScheduledValues(t);
    g.setValueAtTime(g.value, t);
    g.linearRampToValueAtTime(value, t + seconds);
  }

  duck() { this.ducked = true; this.ramp(0.22, 0.08); }
  unduck() { this.ducked = false; this.ramp(1, 0.25); }

  // Fade out and drop everything queued.
  flush() {
    if (!this.ctx) return;
    this.minSeq = this.maxSeq + 1;
    const old = [...this.chunks.values()];
    this.chunks.clear();
    this.ramp(0, 0.12);
    const stopAt = this.ctx.currentTime + 0.13;
    for (const c of old) {
      c.timers.forEach(clearTimeout);
      c.sources.forEach((s) => { try { s.stop(stopAt); } catch {} });
    }
    this.nextTime = 0;
    setTimeout(() => { this.ducked = false; this.ramp(1, 0.05); }, 160);
  }
}

// ---------------------------------------------------------------------------
// Mic + VAD
// ---------------------------------------------------------------------------
class Mic {
  constructor({ onStart, onFrame, onEnd, isAgentSpeaking }) {
    Object.assign(this, { onStart, onFrame, onEnd, isAgentSpeaking });
    this.enabled = true; // hands-free VAD
    this.forced = false; // push-to-talk held
    this.inSpeech = false;
    this.noise = 0.005;
    this.above = 0;
    this.silentMs = 0;
    this.preroll = [];
    this.level = 0;
  }

  async init() {
    if (this.ctx) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.ctx = new AudioContext({ latencyHint: "interactive" });
    await this.ctx.audioWorklet.addModule("/voice/static/mic-worklet.js");
    const src = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "mic-processor");
    this.node.port.onmessage = (e) => this.frame(e.data.pcm, e.data.rms);
    src.connect(this.node);
  }

  frame(pcm, rms) {
    this.level = rms;
    if (this.forced) { this.onFrame(pcm); return; }
    if (!this.inSpeech) {
      this.preroll.push(pcm);
      if (this.preroll.length > 15) this.preroll.shift(); // 300 ms
    }
    if (!this.enabled) return;

    const agent = this.isAgentSpeaking();
    const threshold = agent ? Math.max(this.noise * 5, 0.03) : Math.max(this.noise * 3, 0.012);
    const onsetFrames = agent ? 9 : 4; // 180 ms vs 80 ms
    if (!this.inSpeech) {
      // track the noise floor only while nobody is talking
      if (rms < threshold) this.noise = 0.97 * this.noise + 0.03 * rms;
      this.above = rms > threshold ? this.above + 1 : 0;
      if (this.above >= onsetFrames) {
        this.inSpeech = true;
        this.silentMs = 0;
        this.onStart();
        this.preroll.forEach((p) => this.onFrame(p));
        this.preroll = [];
      }
    } else {
      this.onFrame(pcm);
      this.silentMs = rms < threshold * 0.7 ? this.silentMs + 20 : 0;
      if (this.silentMs >= 700) {
        this.inSpeech = false;
        this.above = 0;
        this.onEnd();
      }
    }
  }

  press() {
    this.forced = true;
    this.inSpeech = false;
    this.preroll.forEach((p) => this.onFrame(p));
    this.preroll = [];
  }

  release() { this.forced = false; }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------
let ws = null;
let briefing = null;
let started = false;
let currentSegment = null;
let partialEl = null;
const citationsSeen = new Map();
const player = new Player(onPlayback);
const mic = new Mic({
  onStart: () => { player.duck(); send({ type: "speech_start" }); },
  onFrame: (pcm) => { if (ws?.readyState === 1) ws.send(pcm); },
  onEnd: () => send({ type: "speech_end" }),
  isAgentSpeaking: () => player.speaking,
});

function send(obj) { if (ws?.readyState === 1) ws.send(JSON.stringify(obj)); }

function onPlayback(event, seq, meta) {
  send({ type: "playback", event, seq });
  if (event === "started") showLine(meta);
}

function setState(state) {
  $("#orb").dataset.state = state;
  $("#state-label").textContent = STATE_LABELS[state] || state;
}

function esc(s) { return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function chipsFor(cites) {
  if (!cites?.length) return "";
  return `<span class="chips">${cites.map((c) =>
    `<a class="chip" href="${esc(c.url || "#")}" target="_blank" rel="noopener" title="${esc(c.headline || "")}">${esc(c.publication || "source")}</a>`).join("")}</span>`;
}

function showLine(meta) {
  const cap = $("#caption");
  cap.textContent = meta.text;
  cap.className = "caption" + (["answer", "ack", "hold"].includes(meta.kind) ? " answer" : meta.kind === "bridge" ? " muted" : "");
  const log = $("#log");
  if (["script", "intro", "outro"].includes(meta.kind) && meta.segment_id !== currentSegment) {
    currentSegment = meta.segment_id;
    const seg = briefing?.segments.find((s) => s.id === meta.segment_id);
    if (seg?.title) log.insertAdjacentHTML("beforeend", `<div class="story-h">${esc(seg.title)}</div>`);
    if (seg) renderSources(seg.citations);
  }
  log.querySelectorAll(".line.current").forEach((el) => el.classList.remove("current"));
  log.insertAdjacentHTML("beforeend", `<p class="line current ${meta.kind}">${esc(meta.text)}${chipsFor(meta.citations)}</p>`);
  log.scrollTop = log.scrollHeight;
}

function renderSources(cites) {
  const box = $("#sources");
  if (!cites?.length) return;
  box.innerHTML = cites.map((c) => `
    <a class="src" href="${esc(c.url)}" target="_blank" rel="noopener">
      <div class="pub">${esc(c.publication || "")}${c.published_at ? " · " + esc(c.published_at.slice(0, 10)) : ""}</div>
      <div class="h">${esc(c.headline || c.url)}</div>
    </a>`).join("");
}

function userSaid(text, final) {
  const log = $("#log");
  if (!partialEl) {
    partialEl = document.createElement("div");
    partialEl.className = "you partial";
    log.appendChild(partialEl);
  }
  partialEl.textContent = text;
  if (final) { partialEl.classList.remove("partial"); partialEl = null; }
  log.scrollTop = log.scrollHeight;
}

function research(ev) {
  const box = $("#research");
  box.querySelector(".muted")?.remove();
  let el = box.querySelector(`[data-id="${ev.request_id}"]`);
  if (!el) {
    el = document.createElement("div");
    el.dataset.id = ev.request_id;
    box.prepend(el);
  }
  el.className = `req ${ev.status}`;
  const q = ev.question ?? el.dataset.q ?? "";
  el.dataset.q = q;
  const status = ev.status === "pending" ? `asked: ${esc(ev.query || q)}`
    : ev.status === "done" ? `${ev.citations?.length || 0} source(s)` : `failed: ${esc(ev.error || "")}`;
  el.innerHTML = `<div class="q">“${esc(q)}”</div><div class="status">${status}</div>${ev.citations ? chipsFor(ev.citations) : ""}`;
  if (ev.citations?.length) renderSources(ev.citations);
}

async function handleMessage(e) {
  if (typeof e.data !== "string") {
    const buf = await e.data.arrayBuffer();
    const seq = new DataView(buf).getUint32(0, true);
    player.audio(seq, new Int16Array(buf, 4));
    return;
  }
  const ev = JSON.parse(e.data);
  switch (ev.type) {
    case "ready":
      briefing = ev.briefing;
      $("#briefing-title").textContent = briefing.title;
      $("#briefing-date").textContent = (briefing.date || "").slice(0, 10);
      $("#briefing-listener").textContent = briefing.listener || "";
      if (ev.stt === "NullSTT") $("#hint").textContent = "Voice input is off (no ElevenLabs key on the server). Type questions below.";
      break;
    case "state": setState(ev.state); break;
    case "chunk_start": player.chunkStart(ev); break;
    case "chunk_end": player.chunkEnd(ev.seq); break;
    case "flush": player.flush(); break;
    case "unduck": player.unduck(); break;
    case "transcript": userSaid(ev.text, ev.final); break;
    case "research": research(ev); break;
    case "error": console.warn("server:", ev.message); break;
  }
}

function connect(briefingId) {
  if (ws) ws.close();
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/session?briefing=${encodeURIComponent(briefingId)}`);
  ws.binaryType = "blob";
  ws.onmessage = handleMessage;
  ws.onclose = () => setState("idle");
  started = false;
  currentSegment = null;
  $("#log").innerHTML = "";
  $("#caption").textContent = "";
}

async function ensureAudio() {
  await player.init();
  try { await mic.init(); } catch (err) {
    console.warn("mic unavailable", err);
    $("#hint").textContent = "Microphone unavailable. You can still type questions.";
  }
}

async function main() {
  const list = await (await fetch("/api/briefings")).json();
  const sel = $("#briefing-select");
  sel.innerHTML = list.map((b) => `<option value="${esc(b.id)}">${esc(b.title)}</option>`).join("");
  sel.onchange = () => connect(sel.value);
  // The Gradio "Listen" tab embeds this page as /voice?briefing=<id>.
  const wanted = new URLSearchParams(location.search).get("briefing");
  const first = list.find((b) => b.id === wanted) || list[0];
  if (first) { sel.value = first.id; connect(first.id); }

  $("#orb").onclick = async () => {
    await ensureAudio();
    if (!started) { started = true; send({ type: "start" }); }
    else if ($("#orb").dataset.state === "paused") send({ type: "control", action: "resume" });
    else send({ type: "control", action: "pause" });
  };
  document.querySelectorAll("[data-control]").forEach((b) => {
    b.onclick = async () => { await ensureAudio(); send({ type: "control", action: b.dataset.control }); };
  });
  $("#handsfree").onchange = (e) => { mic.enabled = e.target.checked; };

  $("#ask").onsubmit = async (e) => {
    e.preventDefault();
    const input = $("#ask-input");
    const text = input.value.trim();
    if (!text) return;
    await ensureAudio();
    started = true;
    send({ type: "text", text });
    input.value = "";
  };

  // push-to-talk: button or space bar (when not typing)
  const ptt = $("#ptt");
  const down = async () => {
    await ensureAudio();
    if (mic.forced) return;
    ptt.classList.add("active");
    started = true;
    send({ type: "interrupt" });
    send({ type: "speech_start" });
    mic.press();
  };
  const up = () => {
    if (!mic.forced) return;
    ptt.classList.remove("active");
    mic.release();
    send({ type: "speech_end" });
  };
  ptt.addEventListener("pointerdown", down);
  ptt.addEventListener("pointerup", up);
  ptt.addEventListener("pointerleave", up);
  window.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !e.repeat && document.activeElement?.tagName !== "INPUT") { e.preventDefault(); down(); }
  });
  window.addEventListener("keyup", (e) => { if (e.code === "Space") up(); });

  // orb pulses with the listener's voice level
  const tick = () => {
    const lvl = mic.inSpeech || mic.forced ? 1 + Math.min(0.25, mic.level * 6) : 1;
    $("#orb").style.setProperty("--level", lvl.toFixed(3));
    requestAnimationFrame(tick);
  };
  tick();
}

main();
