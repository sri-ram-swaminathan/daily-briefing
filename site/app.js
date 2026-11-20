/* The News Council website.
 *
 * Demo edition (GitHub Pages): everything comes from data/*.json, exported from the real
 * pipeline by tools/build_site_data.py, including recorded research-desk answers and audio.
 * Live edition: when a News Council server answers /api/health (same origin, ?api=URL, or the
 * footer form), questions go to /api/ask, pasted stories to /api/council, and "Talk to it live"
 * opens the interruptible voice briefing.
 */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
  };
  const fmtDate = (iso) => {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  };
  const toast = (msg) => {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => (t.hidden = true), 3200);
  };

  const S = {
    data: {},
    reader: store.get("nc-reader") || "max",
    api: null, // base URL when live, "" for same origin
    story: null,
    topic: "all",
    session: null,
  };

  // ------------------------------------------------------------------ data
  async function loadJSON(name) {
    try {
      const r = await fetch(`data/${name}.json`, { cache: "no-cache" });
      return r.ok ? await r.json() : null;
    } catch { return null; }
  }

  // The hosted live server (Hugging Face Space). Pages and the custom domain use it when it's awake.
  const DEFAULT_API = "https://srilearns-daily-briefing.hf.space";

  async function detectApi(timeoutMs = 2500) {
    const params = new URLSearchParams(location.search);
    const candidates = [];
    if (params.get("api")) candidates.push(params.get("api"));
    if (store.get("nc-api")) candidates.push(store.get("nc-api"));
    if (!/github\.io$/.test(location.hostname) && location.protocol.startsWith("http")) candidates.push("");
    if (location.origin !== DEFAULT_API) candidates.push(DEFAULT_API);
    for (const base of candidates) {
      const url = base.replace(/\/$/, "");
      try {
        const ctl = new AbortController();
        const t = setTimeout(() => ctl.abort(), url === DEFAULT_API ? timeoutMs * 2 : timeoutMs);
        const r = await fetch(`${url}/api/health`, { signal: ctl.signal });
        clearTimeout(t);
        if (r.ok) { const h = await r.json(); if (h.ok) return { base: url, health: h }; }
      } catch { /* try next */ }
    }
    return null;
  }

  const persona = (id) => (S.data.council || []).find((p) => p.id === id);
  const avatar = (id) => `assets/people/${id}.png`;

  // ------------------------------------------------------------------ briefing
  const PRESETS = ["Coffee with a friend", "Late-night radio", "The storyteller", "Dry wit", "3-minute sprint", "Calm explainer"];

  function renderReaderSwitch() {
    const readers = S.data.readers || {};
    if (S.api !== null && !readers.you) {  // live mode: build a briefing from your own interests
      readers.you = { name: "You", role: "Your own interests", avatar: "assets/you.svg", tagline: "Tell the agent what you follow and what to skip.",
        interests: store.get("nc-you") || "", stories: [], audio: [], pending: true };
    }
    $("#reader-switch").innerHTML = Object.entries(readers).map(([id, r]) => `
      <button class="reader-tab" role="tab" data-id="${esc(id)}" aria-selected="${id === S.reader}">
        <img src="${esc(r.avatar)}" alt="">
        <span>${esc(r.name)}<small>${esc(r.role)}</small></span>
      </button>`).join("");
    $("#reader-switch").onclick = (e) => {
      const b = e.target.closest(".reader-tab");
      if (b) selectReader(b.dataset.id);
    };
  }

  function selectReader(id) {
    if (!S.data.readers?.[id]) return;
    S.reader = id;
    store.set("nc-reader", id);
    player.stop();
    document.querySelectorAll(".reader-tab").forEach((b) => b.setAttribute("aria-selected", b.dataset.id === id));
    renderReader();
    renderSuggestions();
  }

  function reactedFaces(storyId) {
    const s = (S.data.edition?.stories || []).find((x) => x.id === storyId);
    if (!s) return "";
    return `<span class="faces">${s.reactions.slice(0, 5).map((r) => `<img src="${avatar(r.reviewer)}" alt="">`).join("")}</span>`;
  }

  function renderYouForm() {
    const r = S.data.readers.you;
    const card = $("#reader-card");
    card.className = "reader-card you";
    card.innerHTML = `
      <img src="assets/you.svg" alt="">
      <h3>Your briefing</h3>
      <p class="role">Built live from the dataset, for you</p>
      <form id="you-form" class="you-form">
        <label class="smallcaps" for="you-interests">What you follow, and what to skip</label>
        <textarea id="you-interests" rows="6" required placeholder="I follow European energy prices and what they mean for my bills. Skip crypto and celebrity news.">${esc(r.interests)}</textarea>
        <label class="smallcaps" for="you-preset">Delivery</label>
        <select id="you-preset">${PRESETS.map((p) => `<option>${esc(p)}</option>`).join("")}</select>
        <button class="solid-btn" type="submit">Build my briefing</button>
      </form>`;
    $("#you-form").onsubmit = async (e) => {
      e.preventDefault();
      const interests = $("#you-interests").value.trim();
      const btn = $("#you-form button");
      store.set("nc-you", interests);
      btn.disabled = true;
      btn.textContent = "Reading the council's notes…";
      try {
        const res = await fetch(`${S.api}/api/reader-briefing`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ user: "you", interests, preset: $("#you-preset").value, claims: true }) });
        const d = await res.json();
        if (!res.ok) throw new Error(d.detail || res.statusText);
        Object.assign(r, { interests, pending: false, briefingId: d.id, stories: d.stories });
        renderReader();
        toast(`${d.stories.length} stories picked for you.`);
      } catch (err) {
        toast(`Couldn't build it: ${err.message}`);
        btn.disabled = false;
        btn.textContent = "Build my briefing";
      }
    };
    $("#stories").innerHTML = `<li class="empty">Your stories appear here. Then press play, or “Talk to it live” to interrupt it with questions.</li>`;
    $("#player-now").textContent = "Your briefing";
  }

  function renderReader() {
    const r = S.data.readers[S.reader];
    if (r.pending) return renderYouForm();
    const card = $("#reader-card");
    card.className = `reader-card ${S.reader}`;
    card.innerHTML = `
      <img src="${esc(r.avatar)}" alt="Portrait of ${esc(r.name)}">
      <h3>${esc(r.name)}, ${esc(r.age)}</h3>
      <p class="role">${esc(r.role)}</p>
      <blockquote>${esc(r.tagline)}</blockquote>
      <p class="smallcaps">What ${esc(r.name === "You" ? "you" : r.name)} told the agent</p>
      <p class="interests">${esc(r.interests)}</p>
      ${S.reader === "you" ? `<button class="ghost-btn" id="you-edit">Change my interests</button>` : ""}
      <p class="sourced">Built only from the Liquid Briefing dataset (Newstex feeds). Every sentence links to its source.</p>`;
    const reviewed = new Set((S.data.edition?.stories || []).map((s) => s.id));
    $("#stories").innerHTML = r.stories.map((s, i) => `
      <li class="story" data-i="${i + 1}">
        <div>
          <h3><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.headline)}</a></h3>
          <p class="claims">${claimsHtml(s, i)}</p>
          <div class="evidence" id="ev-${i}" hidden></div>
          <div class="meta">
            <span class="chip t-${esc(s.topic)}">${esc(s.topic)}</span>
            <a class="src-link" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.publication)} · ${fmtDate(s.published_at)} ↗</a>
            ${reviewed.has(s.id) ? `<button class="eyes-btn" data-story="${esc(s.id)}">${reactedFaces(s.id)} Through other eyes</button>` : ""}
          </div>
        </div>
      </li>`).join("") || `<li class="empty">No stories matched yet.</li>`;
    $("#stories").onclick = (e) => {
      const mark = e.target.closest(".cite-mark");
      if (mark) return showEvidence(Number(mark.dataset.story), Number(mark.dataset.claim));
      const b = e.target.closest(".eyes-btn");
      if (!b) return;
      showStory(b.dataset.story);
      $("#council").scrollIntoView();
    };
    $("#player-now").textContent = `${r.name === "You" ? "Your" : `${r.name}'s`} briefing · ${r.stories.length} stories`;
    $("#you-edit")?.addEventListener("click", () => { r.pending = true; renderReader(); });
    if (!$("#compare").hidden) renderCompare();
    updateLiveVoice();
  }

  // Sentence-level sourcing: each sentence ends in a marker; the marker opens the article's own words.
  function claimsHtml(s, i) {
    if (!s.claims?.length) return `${esc(s.summary)} <a class="cite-mark" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.publication)}</a>`;
    return s.claims.map((c, j) => {
      const label = c.kind === "council" ? (persona(c.reviewer)?.name || "Council") : c.kind === "context" ? "why it matters" : `${j + 1}`;
      return `<span class="claim k-${esc(c.kind)}">${esc(c.text)}</span><button class="cite-mark k-${esc(c.kind)}" data-story="${i}" data-claim="${j}" aria-label="Source for sentence ${j + 1}">${esc(label)}</button>`;
    }).join(" ");
  }

  function showEvidence(i, j) {
    const s = S.data.readers[S.reader].stories[i];
    const c = s?.claims?.[j];
    const box = $(`#ev-${i}`);
    if (!c || !box) return;
    const same = !box.hidden && box.dataset.claim === String(j);
    document.querySelectorAll(".evidence").forEach((b) => (b.hidden = true));
    document.querySelectorAll(".claim.on").forEach((x) => x.classList.remove("on"));
    if (same) return;
    box.dataset.claim = String(j);
    const who = c.kind === "council"
      ? `<img src="${avatar(c.reviewer)}" alt=""><span><b>${esc(persona(c.reviewer)?.name || c.reviewer)}</b>, a council member, read this in the article:</span>`
      : c.kind === "context" ? `<span><b>Interpretation</b>: the briefing's own why-it-matters line, based on this article:</span>`
      : `<span><b>Reported fact</b>, from the article:</span>`;
    box.innerHTML = `<div class="ev-head">${who}</div>
      ${c.quote ? `<blockquote>“${esc(c.quote)}”</blockquote>` : `<p class="hint">No single passage to quote; the claim summarises the article as a whole.</p>`}
      <div class="ev-foot"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.publication)} · ${esc(s.headline)} ↗</a>
      <span class="checks">${c.verified ? "✓ quote found verbatim in the article" : "Quote not verified"}${c.kind !== "council" ? ` · ${c.numbers_ok ? "✓ figures match the article" : "⚠ a figure was not found in the article"}` : ""}</span></div>`;
    box.hidden = false;
    box.closest(".story").querySelectorAll(".claim")[j]?.classList.add("on");
  }

  function renderCompare() {
    const readers = Object.values(S.data.readers || {});
    const counts = {};
    readers.forEach((r) => r.stories.forEach((s) => (counts[s.id] = (counts[s.id] || 0) + 1)));
    $("#compare").innerHTML = readers.map((r) => `
      <div>
        <h4><img src="${esc(r.avatar)}" alt="">${esc(r.name)}'s morning</h4>
        <ol>${r.stories.map((s) => `<li class="${counts[s.id] > 1 ? "shared" : ""}">${esc(s.headline)} <span class="chip t-${esc(s.topic)}">${esc(s.topic)}</span></li>`).join("")}</ol>
      </div>`).join("");
  }

  // ------------------------------------------------------------------ audio player
  const player = (() => {
    const audio = new Audio();
    let idx = 0; // 0 = intro, n = story n
    let playing = false;
    const el = $("#player");

    function tracks() { return S.data.readers?.[S.reader]?.audio || []; }
    function stories() { return S.data.readers?.[S.reader]?.stories || []; }
    function highlight() {
      document.querySelectorAll(".story").forEach((li) => li.classList.toggle("now", Number(li.dataset.i) === idx && playing));
      const s = stories()[idx - 1];
      $("#player-now").textContent = idx === 0 ? `Intro · ${S.data.readers[S.reader].name}'s briefing` : `${idx}/${stories().length} · ${s?.headline || ""}`;
      const src = $("#player-src");
      src.hidden = !(s && playing);
      if (s) { src.href = s.url; src.textContent = `Source: ${s.publication} ↗`; }
    }
    function speakFallback(text, onend) {
      if (!("speechSynthesis" in window)) return onend();
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.02;
      u.onend = onend;
      speechSynthesis.cancel();
      speechSynthesis.speak(u);
    }
    function playAt(i) {
      const list = tracks();
      const total = stories().length;
      if (i < 0) i = 0;
      if (i > total) { stop(); toast("That's the briefing. Ask the desk about any of it."); return; }
      idx = i;
      playing = true;
      el.classList.add("playing");
      highlight();
      document.querySelector(`.story[data-i="${idx}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      if (list[idx]) {
        audio.src = list[idx];
        audio.play().catch(() => { playing = false; el.classList.remove("playing"); });
      } else {
        const text = idx === 0 ? `Morning, ${S.data.readers[S.reader].name}.` : stories()[idx - 1].summary;
        speakFallback(text, () => playing && playAt(idx + 1));
      }
    }
    function stop() {
      playing = false;
      audio.pause();
      if ("speechSynthesis" in window) speechSynthesis.cancel();
      el.classList.remove("playing");
      $("#progress-bar").style.width = "0";
      highlight();
    }
    audio.addEventListener("ended", () => playing && playAt(idx + 1));
    audio.addEventListener("timeupdate", () => {
      const total = stories().length + 1;
      const frac = (idx + (audio.duration ? audio.currentTime / audio.duration : 0)) / total;
      $("#progress-bar").style.width = `${Math.min(100, frac * 100)}%`;
    });
    $("#play-btn").onclick = () => {
      if (playing) { audio.pause(); speechSynthesis?.pause?.(); playing = false; el.classList.remove("playing"); highlight(); }
      else if (audio.src && !audio.ended && audio.currentTime > 0) { audio.play(); playing = true; el.classList.add("playing"); highlight(); }
      else playAt(idx || 0);
    };
    $("#next-btn").onclick = () => playAt(idx + 1);
    $("#prev-btn").onclick = () => playAt(audio.currentTime > 3 ? idx : idx - 1);
    document.addEventListener("keydown", (e) => {
      if (e.target.matches("input, textarea, select")) return;
      if (e.key === "ArrowRight" && playing) playAt(idx + 1);
      if (e.key === "ArrowLeft" && playing) playAt(idx - 1);
    });
    return { stop: () => { stop(); idx = 0; audio.removeAttribute("src"); }, play: () => playAt(0) };
  })();

  // ------------------------------------------------------------------ council
  function renderPortraits() {
    $("#portraits").innerHTML = (S.data.council || []).map((p) => `
      <div class="portrait">
        <div class="disc bg-${esc(p.color)}"><img src="${avatar(p.id)}" alt="Portrait of ${esc(p.name)}"></div>
        <h3>${esc(p.name)}</h3>
        <p class="role">${esc(p.age)} · ${esc(p.role)}${p.pronouns ? ` · ${esc(p.pronouns)}` : ""}</p>
        <p class="lens">“${esc(p.lens)}”</p>
        <p class="blind">Blind spot: ${esc(p.blind_spot)}</p>
      </div>`).join("");
  }

  function renderStoryPicker() {
    const stories = S.data.edition?.stories || [];
    const topics = ["all", ...new Set(stories.map((s) => s.topic))];
    $("#topic-chips").innerHTML = topics.map((t) => `<button aria-pressed="${t === S.topic}" data-t="${esc(t)}">${esc(t)}</button>`).join("");
    $("#topic-chips").onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      S.topic = b.dataset.t;
      renderStoryPicker();
      const first = stories.find((s) => S.topic === "all" || s.topic === S.topic);
      if (first) showStory(first.id);
    };
    const shown = stories.filter((s) => S.topic === "all" || s.topic === S.topic);
    $("#story-select").innerHTML = shown.map((s) => `<option value="${esc(s.id)}">${esc(s.headline)} · ${esc(s.publication)}</option>`).join("");
    $("#story-select").onchange = (e) => showStory(e.target.value);
    if (S.story && shown.some((s) => s.id === S.story)) $("#story-select").value = S.story;
  }

  function renderReactions(story) {
    const council = S.data.council || [];
    const byName = Object.fromEntries((story.reactions || []).map((r) => [r.reviewer, r]));
    const cards = council.map((p) => {
      const r = byName[p.id];
      if (!r || !r.observation) {
        return `<div class="reaction silent"><img class="face" src="${avatar(p.id)}" alt=""><div><h4>${esc(p.name)}<small>${esc(p.role)}</small></h4><p>Didn't find anything in this story that speaks to them.</p></div></div>`;
      }
      return `<div class="reaction bg-${esc(p.color)}">
        <img class="face" src="${avatar(p.id)}" alt="Portrait of ${esc(p.name)}">
        <div><h4>${esc(p.name)}<small>${esc(p.role)}</small></h4>
          <p>${esc(r.observation)}</p>
          ${r.quote ? `<q>${esc(r.quote)}</q>` : ""}
        </div></div>`;
    }).join("");
    $("#reactions").innerHTML = `
      <p class="kicker">${esc(story.topic || "")}</p>
      <h3 class="story-title">${story.url ? `<a href="${esc(story.url)}" target="_blank" rel="noopener">${esc(story.headline || "Your story")}</a>` : esc(story.headline || "Your story")}</h3>
      <div class="meta">${story.publication ? `<span>${esc(story.publication)} · ${fmtDate(story.published_at)}</span>` : ""}${story.url ? `<a href="${esc(story.url)}" target="_blank" rel="noopener">Read the original ↗</a>` : ""}</div>
      ${story.moderator ? `<div class="moderator"><img src="assets/people/moderator.png" alt="The moderator"><div><span class="smallcaps">The moderator sums up</span><p>${esc(story.moderator)}</p></div></div>` : "<div style='height:18px'></div>"}
      <div class="reaction-grid">${cards}</div>`;
  }

  function showStory(id) {
    const s = (S.data.edition?.stories || []).find((x) => x.id === id);
    if (!s) return;
    S.story = id;
    if (S.topic !== "all" && s.topic !== S.topic) { S.topic = "all"; renderStoryPicker(); }
    $("#story-select").value = id;
    renderReactions(s);
  }

  async function pasteCouncil() {
    const text = $("#paste-text").value.trim();
    if (!text) return toast("Paste a story first.");
    const btn = $("#paste-btn");
    btn.disabled = true;
    btn.textContent = "The council is reading…";
    try {
      const r = await fetch(`${S.api}/api/council`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
      const d = await r.json();
      renderReactions({ headline: text.split(/\n/)[0].slice(0, 120), topic: "your story", reactions: d.reactions, moderator: d.summary });
      $("#reactions").scrollIntoView({ behavior: "smooth" });
    } catch (err) {
      toast(`The council couldn't meet: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Convene the council";
    }
  }

  // ------------------------------------------------------------------ ask the desk
  const deskAvatar = "assets/people/moderator.png";

  function citesHtml(cites) {
    return (cites || []).length ? `<div class="cites">${cites.map((c) => `
      <a class="cite ${c.web ? "web" : ""}" href="${esc(c.url)}" target="_blank" rel="noopener" title="${esc(c.headline)}">
        <span class="src">${esc(c.publication)}${c.web ? " · web" : ""}</span><span class="ttl">${esc(c.headline)}</span></a>`).join("")}</div>` : "";
  }

  function addMsg(kind, html) {
    const li = document.createElement("li");
    li.className = `msg ${kind}`;
    li.innerHTML = kind === "desk" ? `<img src="${deskAvatar}" alt=""><div class="bubble">${html}</div>` : html;
    $("#chat-log").appendChild(li);
    $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
    return li;
  }

  const wait = (ms) => new Promise((r) => setTimeout(r, ms));

  async function deskAnswer(question, recorded) {
    addMsg("you", esc(question));
    const typing = addMsg("desk", `<div class="typing"><span></span><span></span><span></span></div>`);
    let ans;
    if (S.api !== null && !recorded) {
      try {
        const r = await fetch(`${S.api}/api/ask`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, session_id: S.session }) });
        if (!r.ok) throw new Error(r.statusText);
        const d = await r.json();
        S.session = d.session_id;
        ans = { answer: d.answer, follow_up: d.follow_up, citations: d.citations };
      } catch (err) {
        ans = { answer: `I couldn't reach the research desk (${err.message}).`, citations: [] };
      }
    } else if (recorded) {
      await wait(900);
      ans = recorded;
    } else {
      await wait(500);
      ans = offlineAnswer(question);
    }
    typing.querySelector(".bubble").innerHTML = `<p>${esc(ans.answer)}</p>${ans.follow_up ? `<p class="follow">${esc(ans.follow_up)}</p>` : ""}${citesHtml(ans.citations)}`;
    $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  }

  // Demo edition (no server): answer from today's stories on this page, honestly and with sources.
  const STOP = new Set(("what whats happened happen happening with about tell more does did that this from where which there their "
    + "they them have been will would could should think thinks council people story stories news today latest mean means "
    + "explain deeper give show know want into going over after before also just really much many some your mine "
    + "please could anything something who why how are the and was for its has had not but you can get now "
    + "out one won any all did does wont isnt arent did dont").split(" "));
  const terms = (text) => (String(text).toLowerCase().match(/[a-z0-9$%.]{3,}/g) || [])
    .map((w) => w.replace(/[.]+$/, "")).filter((w) => w.length > 2 && !STOP.has(w));
  const hit = (word, bag) => bag.some((b) => b === word || (word.length > 4 && (b.startsWith(word) || word.startsWith(b))));

  function storyPool() {
    const byId = {};
    (S.data.edition?.stories || []).forEach((s) => (byId[s.id] = { ...s }));
    Object.values(S.data.readers || {}).forEach((r) => (r.stories || []).forEach((s) => {
      byId[s.id] = { ...(byId[s.id] || {}), ...s, moderator: byId[s.id]?.moderator, reactions: byId[s.id]?.reactions };
    }));
    return Object.values(byId).filter((s) => s.headline);
  }

  function offlineAnswer(question) {
    const q = terms(question);
    if (!q.length) return { answer: "Ask me about a story on this page, for example: “What happened with Intel?”", citations: [] };
    const scored = storyPool().map((s) => {
      const head = terms(s.headline), body = terms(`${s.summary || ""} ${s.moderator || ""} ${s.topic || ""}`);
      const headHits = q.filter((w) => hit(w, head)).length;
      return { s, headHits, score: headHits * 3 + q.filter((w) => hit(w, body)).length };
    }).filter((x) => x.headHits > 0 || x.score >= 3).sort((a, b) => b.score - a.score);
    if (!scored.length) {
      return { answer: "That isn't in today's stories on this page. This is the demo edition: it answers from the stories shown here and replays recorded conversations. The live desk searches all 1,740 articles in the dataset, then the web.", citations: [] };
    }
    const lead = scored[0].s;
    const wantsCouncil = /council|think|react|opinion|people|perspective/i.test(question) && (lead.moderator || lead.reactions?.length);
    const voices = (lead.reactions || []).slice(0, 3)
      .map((r) => `${persona(r.reviewer)?.name || r.reviewer}: ${r.observation.split(/(?<=[.!?])\s/)[0]}`).join(" ");
    const answer = wantsCouncil
      ? `The council read “${lead.headline}”. ${lead.moderator || voices}`
      : `${lead.publication} reported this on ${fmtDate(lead.published_at)}: ${lead.summary || lead.moderator || lead.headline}`;
    return {
      answer,
      follow_up: scored.length > 1 ? `Related on this page: ${scored.slice(1, 3).map((x) => x.s.headline).join("; ")}.` : "",
      citations: scored.slice(0, 3).map(({ s }) => ({ publication: s.publication, headline: s.headline, url: s.url })),
    };
  }

  function demoQuestions() {
    const pool = storyPool();
    const out = [];
    if (pool.some((s) => /intel/i.test(s.headline))) out.push("What happened with Intel?");
    if (pool.some((s) => /crypto|bitcoin/i.test(s.headline) && (s.moderator || s.reactions?.length))) out.push("What does the council think about the crypto crash?");
    if (pool.some((s) => /unemployment/i.test(s.headline))) out.push("Is UK unemployment going up or down?");
    return out;
  }

  function renderSuggestions() {
    const reader = S.data.readers?.[S.reader];
    const recorded = S.data.qa?.[S.reader] || [];
    const generic = ["What happened in the US midterms?", "What happened with Intel?", "Why is gold falling?"];
    $("#suggestions").innerHTML = (recorded.length ? `<button class="replay" data-replay="1">▶ Replay ${esc(reader?.name)}'s conversation</button>` : "")
      + recorded.map((t, i) => `<button data-rec="${i}">${esc(t.question)}</button>`).join("")
      + (S.api !== null ? generic : demoQuestions()).map((q) => `<button data-q="${esc(q)}">${esc(q)}</button>`).join("");
    $("#suggestions").onclick = async (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.replay) {
        $("#chat-log").innerHTML = "";
        for (const t of recorded) await deskAnswer(t.question, t);
      } else if (b.dataset.rec) {
        const t = recorded[Number(b.dataset.rec)];
        deskAnswer(t.question, S.api === null ? t : null);
      } else if (b.dataset.q) deskAnswer(b.dataset.q);
    };
  }

  // ------------------------------------------------------------------ reels
  function renderReels() {
    const reels = S.data.reels || [];
    if (!reels.length) {
      $("#reels-grid").innerHTML = `<p class="empty">No reels exported yet. Run <code>uv run reels.py --query "…" --audience student</code>.</p>`;
      return;
    }
    $("#reels-grid").innerHTML = reels.map((r) => `
      <figure class="reel">
        <div class="phone"><video src="${esc(r.video)}" poster="${esc(r.poster)}" controls playsinline preload="none"></video></div>
        <figcaption>
          <span class="chip t-${esc(r.article.topic)}">For ${r.audience === "finance" ? "Max · markets" : r.audience === "student" ? "Lena · student" : esc(r.audience)}</span>
          <h3>${esc(r.hook)}</h3>
          <p>${esc(r.caption)}</p>
          <p class="tags">${(r.hashtags || []).map((h) => `#${esc(h)}`).join(" ")}</p>
          <p><a href="${esc(r.article.url)}" target="_blank" rel="noopener">Source: ${esc(r.article.publication)} ↗</a> · ${esc(r.video_model || "")}</p>
        </figcaption>
      </figure>`).join("");
  }

  // ------------------------------------------------------------------ newsroom flywheel
  // A loop, told one step at a time: the wheel highlights the step, the card tells its story with a
  // real example from this demo, and a tag says honestly what is built today and what comes next.
  function flywheelSteps() {
    const reviewed = (S.data.edition?.stories || []).length;
    const lenaQ = S.data.qa?.lena?.[0];
    return [
      { time: "05:00", title: "Feeds land", short: "Feeds", color: "sky", status: "Working today",
        body: "Newstex delivers the overnight news. Every article is indexed and duplicates across outlets and languages are merged.",
        gives: "1,740 articles from six outlets",
        example: "Invezz, ActionForex, IBTimes UK and Zacks, in English, German and Dutch." },
      { time: "05:20", title: "The council reads", short: "Council", color: "lavender", status: "Working today",
        body: "Seven people from very different lives read every story once and react, each backed by a verbatim quote. One read serves every listener, so the cost doesn't grow with the audience.",
        gives: "Seven perspectives on every story",
        example: `${reviewed || "Twenty"} of today's stories, reviewed by all seven and quote-checked against the article.` },
      { time: "06:00", title: "Briefings go out", short: "Briefings", color: "sage", status: "Working today",
        body: "Each listener's agent picks what matters to them, says why in their terms, links every sentence to its source and voices it. The top stories are also cut into reels for social.",
        gives: "A personal, sourced audio briefing",
        example: "Max and Lena both get eurozone inflation: for Max it's the ECB and bond markets, for Lena it's heating bills." },
      { time: "07:30", title: "Listeners talk back", short: "Talk back", color: "peach", status: "Working today",
        body: "They interrupt, ask where a number came from, say “go deeper”, skip or replay. The desk answers from the dataset first, then the web, and remembers the conversation.",
        gives: "Questions, skips and replays",
        example: lenaQ ? `Lena asked: “${lenaQ.question}” The desk answered from ${lenaQ.citations?.[0]?.publication || "the dataset"}.` : "Listeners ask for sources and context, mid-story." },
      { time: "All day", title: "Signals pile up", short: "Signals", color: "butter", status: "Partly built",
        body: "Every question, skip, replay and “new to me” rating says what people want explained, what they ignore, and which angles opened their eyes.",
        gives: "A live map of what the audience needs",
        example: "Already recorded: which council angle each listener heard, and whether they rated it new to them." },
      { time: "18:00", title: "The newsroom learns", short: "Learn", color: "blush", status: "Next",
        body: "Unanswered questions become tomorrow's explainers, skips and ratings sharpen each listener's picks, and the angles people value guide the council. Better coverage brings more listening, and the wheel turns faster.",
        gives: "Better coverage tomorrow, so back to step 1",
        example: "A question the desk couldn't answer from the dataset is a story the newsroom hasn't written yet." },
    ];
  }

  const FW = { i: 0, playing: true, timer: null, visible: false };

  function renderFlywheel() {
    const steps = flywheelSteps();
    const svg = $("#fw-svg");
    if (!svg) return;
    const cx = 300, cy = 300, R = 205, r = 50;
    const pos = (k) => {
      const a = (-90 + k * (360 / steps.length)) * Math.PI / 180;
      return [cx + R * Math.cos(a), cy + R * Math.sin(a), a];
    };
    // arcs between nodes, trimmed so arrowheads stop at the next disc
    const gap = (r + 10) / R;
    const arcs = steps.map((_, k) => {
      const a0 = pos(k)[2] + gap, a1 = pos(k + 1)[2] - gap;
      const p0 = [cx + R * Math.cos(a0), cy + R * Math.sin(a0)], p1 = [cx + R * Math.cos(a1), cy + R * Math.sin(a1)];
      return `<path class="fw-arc" data-k="${k}" d="M${p0[0].toFixed(1)} ${p0[1].toFixed(1)} A${R} ${R} 0 0 1 ${p1[0].toFixed(1)} ${p1[1].toFixed(1)}" marker-end="url(#fw-head)"/>`;
    }).join("");
    const nodes = steps.map((s, k) => {
      const [x, y, a] = pos(k);
      const lx = cx + (R + r + 26) * Math.cos(a), ly = cy + (R + r + 26) * Math.sin(a);
      const anchor = Math.abs(Math.cos(a)) < 0.2 ? "middle" : Math.cos(a) > 0 ? "start" : "end";
      return `<g class="fw-node" data-k="${k}" role="button" tabindex="0" aria-label="Step ${k + 1}: ${esc(s.title)}">
        <circle class="fw-disc fill-${s.color}" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${r}"/>
        <text class="fw-num" x="${x.toFixed(1)}" y="${(y + 2).toFixed(1)}">${k + 1}</text>
        <text class="fw-time" x="${x.toFixed(1)}" y="${(y + 24).toFixed(1)}">${esc(s.time)}</text>
        <text class="fw-label" x="${lx.toFixed(1)}" y="${(ly + 6).toFixed(1)}" text-anchor="${anchor}">${esc(s.short)}</text>
      </g>`;
    }).join("");
    svg.innerHTML = `
      <defs><marker id="fw-head" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0 0 10 5 0 10z" class="fw-head"/></marker></defs>
      <circle class="fw-track" cx="${cx}" cy="${cy}" r="${R}"/>
      ${arcs}
      <circle class="fw-runner" r="7"><animateMotion dur="14s" repeatCount="indefinite"
        path="M${cx} ${cy - R} A${R} ${R} 0 1 1 ${cx - 0.01} ${cy - R}"/></circle>
      <g class="fw-center">
        <circle cx="${cx}" cy="${cy}" r="118" class="fw-hub"/>
        <text x="${cx}" y="${cy - 42}" class="fw-hub-kicker">THE FLYWHEEL</text>
        <text x="${cx}" y="${cy - 8}" class="fw-hub-line">More listening</text>
        <text x="${cx}" y="${cy + 20}" class="fw-hub-line">→ sharper signals</text>
        <text x="${cx}" y="${cy + 48}" class="fw-hub-line">→ better briefings</text>
      </g>
      ${nodes}`;
    $("#fw-dots").innerHTML = steps.map((s, k) => `<button class="fw-dot" data-k="${k}" aria-label="Step ${k + 1}: ${esc(s.title)}"></button>`).join("");
    const go = (k, user) => {
      FW.i = (k + steps.length) % steps.length;
      if (user) setPlaying(false);
      showFlywheelStep(steps);
    };
    svg.onclick = (e) => { const g = e.target.closest(".fw-node"); if (g) go(Number(g.dataset.k), true); };
    svg.onkeydown = (e) => { const g = e.target.closest(".fw-node"); if (g && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); go(Number(g.dataset.k), true); } };
    $("#fw-dots").onclick = (e) => { const b = e.target.closest(".fw-dot"); if (b) go(Number(b.dataset.k), true); };
    $("#fw-prev").onclick = () => go(FW.i - 1, true);
    $("#fw-next").onclick = () => go(FW.i + 1, true);
    $("#fw-play").onclick = () => setPlaying(!FW.playing);
    const tick = () => { if (FW.playing && FW.visible) go(FW.i + 1, false); };
    clearInterval(FW.timer);
    FW.timer = setInterval(tick, 5200);
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) setPlaying(false);
    new IntersectionObserver((en) => en.forEach((x) => (FW.visible = x.isIntersecting)), { threshold: 0.35 }).observe($("#flywheel"));
    showFlywheelStep(steps);
  }

  function setPlaying(on) {
    FW.playing = on;
    $("#fw-play").textContent = on ? "Pause" : "Play";
  }

  function showFlywheelStep(steps) {
    const s = steps[FW.i], next = steps[(FW.i + 1) % steps.length];
    document.querySelectorAll(".fw-node").forEach((g) => g.classList.toggle("on", Number(g.dataset.k) === FW.i));
    document.querySelectorAll(".fw-arc").forEach((p) => p.classList.toggle("on", Number(p.dataset.k) === FW.i));
    document.querySelectorAll(".fw-dot").forEach((d) => d.setAttribute("aria-current", Number(d.dataset.k) === FW.i ? "step" : "false"));
    const tag = s.status === "Working today" ? "ok" : s.status === "Partly built" ? "part" : "next";
    const card = $("#fw-card");
    card.className = `fw-card bg-${s.color}`;
    card.innerHTML = `
      <div class="fw-meta"><span class="fw-step">Step ${FW.i + 1} of ${steps.length} · ${esc(s.time)}</span><span class="fw-tag t-${tag}">${esc(s.status)}</span></div>
      <h3>${esc(s.title)}</h3>
      <p>${esc(s.body)}</p>
      <p class="fw-example"><span class="smallcaps">From this demo</span>${esc(s.example)}</p>
      <p class="fw-gives"><b>Produces:</b> ${esc(s.gives)} <span class="fw-arrow">→ feeds “${esc(next.title)}”</span></p>`;
  }

  function renderCosts() {
    const c = S.data.costs;
    if (!c) return;
    const f = c.full_dataset, t = c.ten_x, day = c.newsroom_day;
    const usd = (n) => `$${(Math.round(n * 100 + 1e-6) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    $("#cost-grid").innerHTML = `
      <div class="cost bg-sage"><span class="smallcaps">Full dataset</span><b>${usd(f.council_as_built)}</b><span>Council reads all ${f.articles.toLocaleString("en-US")} articles once (${usd(f.council_optimised)} with triage and caching).</span></div>
      <div class="cost bg-sky"><span class="smallcaps">At 10x the volume</span><b>${usd(t.council_as_built)}</b><span>${t.articles.toLocaleString("en-US")} articles; linear, ${usd(t.council_optimised)} optimised.</span></div>
      <div class="cost bg-peach"><span class="smallcaps">Per reader, per day</span><b>${usd(f.per_reader.total)}</b><span>Story picks, ElevenLabs narration and three follow-up questions with web search.</span></div>
      <div class="cost bg-lavender"><span class="smallcaps">One reel</span><b>${usd(c.reel.total)}</b><span>${c.reel.sora_seconds} seconds of Sora 2, script and voice-over.</span></div>`;
  }

  // ------------------------------------------------------------------ live mode
  function updateLiveVoice() {
    const a = $("#live-voice");
    a.hidden = S.api === null;
    a.onclick = async (e) => {
      e.preventDefault();
      const r = S.data.readers[S.reader];
      if (r.pending) return toast("Build your briefing first.");
      a.textContent = "Building…";
      try {
        let id = r.briefingId;
        if (!id) {
          const res = await fetch(`${S.api}/api/reader-briefing`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user: S.reader, interests: r.interests }) });
          const d = await res.json();
          if (!res.ok) throw new Error(d.detail || res.statusText);
          id = r.briefingId = d.id;
        }
        window.open(`${S.api}/voice?briefing=${encodeURIComponent(id)}`, "_blank", "noopener");
      } catch (err) { toast(`Couldn't start the live briefing: ${err.message}`); }
      a.textContent = "Talk to it live ↗";
    };
  }

  function setMode(live) {
    const pill = $("#mode-pill");
    pill.className = `pill ${live ? "pill-live" : "pill-demo"}`;
    pill.textContent = live ? "Live edition" : "Demo edition";
    $("#paste-text").disabled = !live;
    $("#paste-btn").disabled = !live;
    $("#paste-hint").textContent = live ? "Paste a story, a press release or your own summary. Seven people will react, then the moderator sums up." : "Connect a News Council server (see the footer) to hear the council react to text you paste. In this demo edition, pick a story above.";
    $("#desk-note").textContent = live ? `Live: answers come from the research desk${S.health?.web_search ? " with web search" : ""}, and it remembers this conversation.` : "Demo edition: the replays are real answers recorded from the research desk, web searches included. Typed questions are answered from the stories on this page; the live desk searches the whole dataset and the web.";
    $("#api-status").textContent = live ? `Connected to ${S.api || location.origin}.` : "Run `uv run app.py` and open it, or paste its URL here.";
    if (live) renderReaderSwitch();
    renderSuggestions();
    updateLiveVoice();
  }

  $("#api-form").onsubmit = async (e) => {
    e.preventDefault();
    const url = $("#api-input").value.trim().replace(/\/$/, "");
    store.set("nc-api", url || null);
    const found = await detectApi();
    S.api = found ? found.base : null;
    S.health = found?.health;
    setMode(!!found);
    toast(found ? "Connected to the live desk." : "Couldn't reach that server.");
  };
  // Hand-in checklist: each button demonstrates one requirement.
  document.querySelector(".handin").onclick = (e) => {
    const go = e.target.closest("[data-go]")?.dataset.go;
    if (!go) return;
    if (go === "play") {
      if (S.reader !== "max") selectReader("max");
      $("#briefing").scrollIntoView();
      player.play();
    } else if (go === "compare") {
      const box = $("#compare");
      if (box.hidden) $("#compare-btn").click();
      box.scrollIntoView({ block: "center" });
    } else if (go === "evidence") {
      $("#stories .story")?.scrollIntoView({ block: "center" });
      showEvidence(0, 0);
    }
  };
  $("#paste-btn").onclick = pasteCouncil;
  $("#chat-form").onsubmit = (e) => {
    e.preventDefault();
    const q = $("#chat-input").value.trim();
    if (!q) return;
    $("#chat-input").value = "";
    deskAnswer(q);
  };
  $("#compare-btn").onclick = () => {
    const box = $("#compare");
    box.hidden = !box.hidden;
    $("#compare-btn").textContent = box.hidden ? "Show Max and Lena side by side" : "Hide the comparison";
    if (!box.hidden) renderCompare();
  };

  // highlight the section in view
  const navLinks = [...document.querySelectorAll(".nav a")];
  const io = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (en.isIntersecting) navLinks.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === `#${en.target.id}`));
  }), { rootMargin: "-45% 0px -50% 0px" });
  document.querySelectorAll("main section[id]").forEach((s) => io.observe(s));

  // ------------------------------------------------------------------ boot
  (async () => {
    const names = ["council", "edition", "readers", "qa", "reels", "costs"];
    const results = await Promise.all(names.map(loadJSON));
    names.forEach((n, i) => (S.data[n] = results[i]));
    if (!S.data.readers?.[S.reader]) S.reader = Object.keys(S.data.readers || {})[0];
    renderReaderSwitch();
    if (S.reader) renderReader();
    renderPortraits();
    renderStoryPicker();
    const first = S.data.readers?.[S.reader]?.stories.find((s) => (S.data.edition?.stories || []).some((e) => e.id === s.id));
    showStory(first?.id || S.data.edition?.stories?.[0]?.id);
    renderReels();
    renderFlywheel();
    renderCosts();
    setMode(false);
    addMsg("desk", `<p>Morning. I'm the research desk. Pick a question, or ask me about any story: “where's that from?”, “go deeper”, “what does the council think?”</p>`).classList.add("hello");
    const found = await detectApi();
    if (found) { S.api = found.base; S.health = found.health; setMode(true); return; }
    // A sleeping Space takes up to a minute to wake: keep the demo edition and switch when it answers.
    if (location.origin === DEFAULT_API) return;
    try {
      const ctl = new AbortController();
      setTimeout(() => ctl.abort(), 90000);
      const r = await fetch(`${DEFAULT_API}/api/health`, { signal: ctl.signal });
      const h = r.ok ? await r.json() : null;
      if (h?.ok && S.api === null) { S.api = DEFAULT_API; S.health = h; setMode(true); toast("The live desk is awake: ask it anything."); }
    } catch { /* stay in the demo edition */ }
  })();
})();
