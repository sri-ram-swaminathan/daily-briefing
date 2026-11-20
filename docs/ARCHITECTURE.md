# Live news voice agent: architecture

## How the pieces fit

```
 council.py ── persona observations ──▶ council.sqlite3 ◀──────────────┐
                                             │                         │ council perspectives
 briefing.py (summary agent) ── reader's ────┤                         │
   stories as spoken text   ◀─ preferences ──┘                         │
        │                                                              │
        │ council_bridge.build_reader_briefing()                       │
        ▼                                                              │
 Briefing script ──▶ VoiceSession (session.py) ── MainAgentRequest ──▶ Research desk
 (tone + style       - narrates the script       ◀─ spoken segments ── (main_agent.py: BM25 over
  from a preset)     - handles interruptions        + citations         the dataset + council
                     - routes what you said                             reviews + OpenAI)
                     - bridges back
                            │ websocket /ws/session
                     Browser player (/voice): Web Audio + mic VAD (web/app.js)

 app.py: Gradio Blocks (Listen + Council tabs) mounted on the voice agent's FastAPI app → one port
```

* **Summary agent**: `briefing.py`. Its sweep picks the reader's stories and writes them for the
  ear. `voice_agent/council_bridge.py` turns its `(article_id, url, spoken_text)` output into a
  `Briefing`, with citations from the dataset and a delivery preset. Any other agent can push a
  script to `POST /api/briefings`; the format is in `voice_agent/script.py`.
* **Voice agent** (`voice_agent/`). Owns the conversation: plays the script, notices when you
  talk, decides what to do, speaks.
* **Research desk** (the voice agent's main agent). Answers what the voice agent doesn't know.
  The built-in `LocalMainAgent` searches the dataset, adds the News Council's saved observations
  on the articles it found ("what does the council think?"), and has the OpenAI model write the
  spoken answer. It is also served at `POST /api/main-agent`. Any HTTP service implementing the
  contract in `voice_agent/main_agent.py` can replace it via `MAIN_AGENT_URL`.

### Serving

`app.py` builds the Gradio UI and mounts it at `/` on the voice agent's FastAPI app
(`gr.mount_gradio_app`), then runs uvicorn on port 7860. That keeps the Hugging Face Spaces
layout (`app_file: app.py`) while serving the voice agent's websocket and static player from
the same origin. The Listen tab embeds `/voice?briefing=<id>` in an iframe with microphone
permission.

## Why a custom pipeline instead of a hosted conversational agent

A hosted agent generates every turn with an LLM. Here most of the speech is a script that
already exists, with its own tone notes, and has to be read *as written*. So the voice agent
treats the script as data. ElevenLabs does the two things it is best at: natural TTS and fast
STT. The turn-taking logic stays in code that we can test (`tests/test_session.py`).

## How an interruption plays out

```
 listener starts talking
   │  browser VAD onset (80 ms quiet room / 180 ms while narrator speaks)
   ├─▶ browser ducks narrator to ~22% volume ................ instant, local
   ├─▶ speech_start ─▶ server: SUSPECT, stops queueing audio, opens Scribe stream
   │
   │  Scribe partial transcript shows real words ("can you dig…")
   ├─▶ server: confirm ─▶ flush ─▶ browser fades out in 120 ms ... no mid-word chop
   │      (if it was a cough or "mm-hm": unduck, carry on, nothing lost)
   │
 listener stops (700 ms of silence)
   ├─▶ speech_end ─▶ Scribe commit ─▶ final text
   ├─▶ router: patterns first (no model call), the OpenAI model only when ambiguous
   │     control      → pause / resume / skip / repeat / slower / faster / stop
   │     answer       → only if the briefing already said it; then bridge back
   │     escalate     → research flow below
   │
 research flow
   ├─▶ pre-rendered acknowledgement plays at once:
   │     "Good question. I don't have the sources in front of me yet, let me pull them up."
   ├─▶ MainAgentRequest sent in parallel (question, rewritten query, needs, time hint,
   │     current story, what the listener has heard so far, the story's citations, Q&A history)
   ├─▶ hold line if it takes > 5 s ("Still digging…")
   ├─▶ answer spoken with citation chips on screen, then a follow-up offer
   └─▶ 7 s of silence ─▶ "Okay, back to the Intel lawsuit." ─▶ replays the interrupted sentence
```

### Pieces that make it sound natural

| Problem | What we do |
| --- | --- |
| Choppy stop | Duck first, then a 120 ms gain ramp to silence. Never a hard cut. |
| Cough / "mm-hm" stops the briefing | Two-phase barge-in: *suspect* (duck) → *confirm* only on real words. Backchannels release it. |
| Echo of the narrator triggers barge-in | Browser AEC + higher VAD threshold and longer onset while audio plays; mic audio only streams during detected speech. Headphones still work best. |
| Losing your place | Cursor = the sentence being *heard* (playback events from the browser), not the one being generated. Resume replays that sentence from cache. |
| Robotic sentence joins | Each sentence is a separate TTS request with `previous_text`/`next_text` for prosody; short sentences are merged; small pauses between sentences and longer between stories. |
| Dead air while researching | Acknowledgements are pre-synthesised at session start; hold lines on slow answers. |
| Tone | Script tone ("serious, measured") → ElevenLabs stability/style/speed; with `eleven_v3` also an inline `[serious]` audio tag. Answers inherit the briefing's tone and style notes. |
| Too much audio buffered to cancel | At most 2 sentences queued in the browser (flow control via playback acks). |

## Wire protocol

See the docstring at the top of `voice_agent/session.py`. Audio frames are binary:
`uint32 LE sequence number` + PCM16 mono (24 kHz down, 16 kHz up).

## Cost notes

* Narration audio is cached per sentence and shared across listeners on one server: replays after
  interruptions and repeat listens cost nothing.
* STT is billed only for detected speech (VAD gating + 300 ms pre-roll), not the whole session.
* The router skips the LLM for controls, backchannels and obvious research asks; when it is needed, it
  uses a small model (`gpt-4.1-mini` by default). The research desk sends at most ~10 article excerpts per
  question.

## Licence guard rails (dataset)

The dataset licence forbids reading article text aloud. The research desk prompt requires
paraphrase, attributed quotes under 12 words, and article ids for every segment. Invalid ids are
dropped. Without an OpenAI key, the template answer only names outlet, date and headline.

## Files

| File | Role |
| --- | --- |
| `voice_agent/session.py` | State machine, barge-in, flow control, research flow, resume |
| `voice_agent/router.py` | Intent routing (patterns + OpenAI) |
| `voice_agent/main_agent.py` | Main-agent contract, remote client, built-in research desk |
| `voice_agent/retrieval.py` | BM25 index over `liquid-briefing-data/`, time-hint windows |
| `voice_agent/script.py` | Script formats, sentence chunking, narration plan |
| `voice_agent/tts.py` | ElevenLabs streaming TTS, tone → voice settings, audio cache |
| `voice_agent/stt.py` | Scribe realtime (websocket) with batch fallback |
| `voice_agent/llm.py` | OpenAI Responses API JSON calls (strict schemas) |
| `voice_agent/council_bridge.py` | briefing.py output → voice script; council observations for the desk |
| `voice_agent/presets.py` | Named delivery styles |
| `voice_agent/server.py` | FastAPI routes: player page, REST, websocket |
| `app.py` | Gradio UI (Listen + Council), mounts everything on one port |
| `web/` | Player, mic worklet, VAD, UI |
