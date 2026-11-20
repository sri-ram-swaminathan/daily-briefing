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

  function offlineAnswer(question) {
    const words = question.toLowerCase().match(/[a-z0-9]{4,}/g) || [];
    const pool = [...(S.data.edition?.stories || [])];
    const scored = pool.map((s) => ({ s, n: words.filter((w) => `${s.headline} ${s.moderator} ${s.topic}`.toLowerCase().includes(w)).length }))
      .filter((x) => x.n > 0).sort((a, b) => b.n - a.n).slice(0, 3);
    if (!scored.length) {
      return { answer: "This demo edition replays recorded answers and can browse the stories the council reviewed. I didn't find one on that. Connect a live server in the footer and I'll search the feeds and the web for you.", citations: [] };
    }
    const lead = scored[0].s;
    return {
      answer: `From the stories the council reviewed: ${lead.moderator || lead.headline}`,
      follow_up: "For a fresh answer with web search, connect a live server in the footer.",
      citations: scored.map(({ s }) => ({ publication: s.publication, headline: s.headline, url: s.url })),
    };
  }

  function renderSuggestions() {
    const reader = S.data.readers?.[S.reader];
    const recorded = S.data.qa?.[S.reader] || [];
    const generic = ["What happened in the US midterms?", "Where did that figure come from?", "What does the council think about the Cop31 talks?"];
    $("#suggestions").innerHTML = (recorded.length ? `<button class="replay" data-replay="1">▶ Replay ${esc(reader?.name)}'s conversation</button>` : "")
      + recorded.map((t, i) => `<button data-rec="${i}">${esc(t.question)}</button>`).join("")
      + (S.api !== null ? generic.map((q) => `<button data-q="${esc(q)}">${esc(q)}</button>`).join("") : "");
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

  // ------------------------------------------------------------------ data desk
  const TOPIC_COLOR = { markets: "sky", equities: "sky", forex: "sky", finance: "sky", weather: "mint", climate: "sage", energy: "butter", politics: "lavender", midterms: "lavender", sports: "peach", world: "blush", general: "blush" };
  const ORIGINAL_TOPICS = new Set(["markets", "equities", "forex", "world"]);

  function renderCoverage() {
    const c = S.data.coverage;
    if (!c) return;
    const max = Math.max(...Object.values(c.by_topic));
    const original = c.feeds.filter((f) => f.original).reduce((n, f) => n + f.articles, 0);
    const extra = c.total - original;
    $("#coverage-total").textContent = extra
      ? `${c.total.toLocaleString("en-US")} articles: ${original.toLocaleString("en-US")} from the Newstex feeds plus ${extra.toLocaleString("en-US")} from ${c.feeds.filter((f) => !f.original).length} extra feeds.`
      : `${c.total.toLocaleString("en-US")} articles from the six Liquid Briefing feeds (Newstex): the data this demo runs on, and nothing else.`;
    $("#coverage-bars").innerHTML = Object.entries(c.by_topic).map(([t, n]) => `
      <div class="bar ${ORIGINAL_TOPICS.has(t) ? "" : "new"}"><span class="lbl">${esc(t)}</span>
        <span class="track"><span class="fill bg-${TOPIC_COLOR[t] || "paper"}" style="width:${Math.max(2, (n / max) * 100)}%"></span></span>
        <span class="n">${n}</span></div>`).join("");
    $("#feeds-table").innerHTML = `<thead><tr><th>Feed</th><th>Outlet</th><th>Topic</th><th>Articles</th></tr></thead><tbody>${c.feeds.map((f) => `
      <tr><td>${esc(f.feed)}${f.original ? "" : " <span class='chip t-climate'>new</span>"}</td><td>${esc(f.publication)}</td><td>${esc(f.topic)}</td><td class="num">${f.articles}</td></tr>`).join("")}</tbody>`;
  }

  function renderLinks() {
    const L = S.data.links || {};
    const block = (label, key) => {
      const d = L[key];
      if (!d) return "";
      const s = d.summary.by_status || {};
      return `<div class="audit"><h4>${label} · ${d.summary.total.toLocaleString("en-US")} links · checked ${esc(d.summary.checked_at)} UTC</h4>
        <div class="tiles">
          <div class="tile bg-sage"><b>${s.ok || 0}</b><span>verified automatically: right article</span></div>
          <div class="tile bg-butter"><b>${s.blocked || 0}</b><span>block automated checks, open in a browser</span></div>
          <div class="tile bg-blush"><b>${(s.broken || 0) + (s.soft_404 || 0) + (s.redirected || 0)}</b><span>broken</span></div>
        </div></div>`;
    };
    $("#link-summary").innerHTML = block("Liquid Briefing dataset (Newstex)", "link_check_liquid_briefing_data") + block("Extra topic feeds", "link_check_news_data")
      + `<p class="finding"><strong>What we found:</strong> every source link works. Invezz looked dead to scripts at first (HTTP 403, even for a made-up URL), but all 406 links opened the right article when checked slowly. Zacks answers automated checks with a bot wall; its links open normally in a browser.</p>`;
  }

  function renderCosts() {
    const c = S.data.costs;
    if (!c) return;
    const f = c.full_dataset, t = c.ten_x, day = c.newsroom_day;
    const usd = (n) => `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
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
    $("#desk-note").textContent = live ? `Live: answers come from the research desk${S.health?.web_search ? " with web search" : ""}, and it remembers this conversation.` : "Demo edition: these are real answers recorded from the research desk on 20 November 2026, including its web searches. Connect a server to ask your own.";
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
    const names = ["council", "edition", "readers", "qa", "coverage", "links", "reels", "costs"];
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
    renderCoverage();
    renderLinks();
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
