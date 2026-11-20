"""Decide what to do with something the listener said.

Fast path first: playback controls, backchannels ("mm-hm") and obvious research
requests ("dig deeper", "cite your sources", "how did this develop") are
recognised with patterns, with no model call, so the voice agent reacts within a
few hundred milliseconds. Only ambiguous utterances go to the OpenAI model.

Intents
-------
control      pause / resume / skip / repeat / slower / faster / stop
backchannel  "yeah", "uh-huh": the listener is following. Carry on.
answer       answerable *strictly* from what the briefing already said
escalate     anything else: the voice agent says it doesn't have that yet
             and hands the question to the main agent (research desk)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class Route:
    intent: str  # control | backchannel | answer | escalate
    action: str = ""  # for control
    answer: str = ""  # for answer
    query: str = ""  # standalone research question for the main agent
    needs: list[str] = field(default_factory=list)  # deep_dive, sources, timeline, background, related, perspectives
    time_hint: str = ""
    source: str = "heuristic"  # heuristic | llm


@dataclass
class RouteContext:
    briefing_title: str
    current_segment_title: str
    current_segment_text: str
    spoken_so_far: str  # what the listener actually heard of the current segment
    previous_exchange: str = ""  # last Q/A in this conversation, for "go deeper on that"
    language: str = "en"


_BACKCHANNEL = re.compile(
    r"^(yeah|yep|yes|uh[- ]?huh|mm+[- ]?hm+|mm+|hmm+|okay|ok|right|sure|cool|nice|wow|got it|i see|interesting|ah|oh)"
    r"[\s,.!]*$",
    re.I,
)

_CONTROLS: list[tuple[str, re.Pattern[str]]] = [
    ("stop", re.compile(r"^(stop|stop (it|the briefing|talking)|that'?s enough|end( the)? briefing|quit)\W*$", re.I)),
    ("pause", re.compile(r"\b(pause|hold on|hang on|one (sec|second|moment)|give me a (sec|second|minute)|wait a (sec|second|minute))\b", re.I)),
    ("resume", re.compile(r"^(ok(ay)?,? )?(go on|carry on|continue|keep going|resume|go ahead|back to (the )?(briefing|news|story)|let'?s (go back|continue|move on)|where were we|you were saying|anyway)\W*$", re.I)),
    ("skip", re.compile(r"\b(skip( this| that| it)?|next (story|one|item|topic)|move on|not interested)\b", re.I)),
    ("repeat", re.compile(r"\b(repeat( that)?|say (that|it) again|come again|what did you (just )?say|pardon|sorry\??$|didn'?t (catch|hear) that)\b", re.I)),
    ("slower", re.compile(r"\b(slow (it )?down|slower|too fast)\b", re.I)),
    ("faster", re.compile(r"\b(speed (it )?up|faster|too slow)\b", re.I)),
]

_RESEARCH_CUES: list[tuple[str, re.Pattern[str]]] = [
    ("sources", re.compile(r"\b(sources?|cite|citations?|where (did|does|is) (that|this|it) (come|from)|who (said|reported|says)|according to whom|link)\b", re.I)),
    ("timeline", re.compile(r"\b(how did (this|that|it) (develop|start|unfold|happen|get here|come about)|timeline|history|so far|how we got here|what led|leading up|developments?|evolv)\b", re.I)),
    ("timeline", re.compile(r"\b(last|past|previous|next) (week|month|few days)|yesterday|earlier this week|on (monday|tuesday|wednesday|thursday|friday|saturday|sunday)|take me back|go back to\b", re.I)),
    ("deep_dive", re.compile(r"\b(dig|deeper|in depth|more (detail|about|on)|tell me more|elaborate|unpack|break (it|that) down|explain|what does (that|this) mean)\b", re.I)),
    ("background", re.compile(r"\b(who is|who's|what is|what's|background|context|why (is|did|does|are|was))\b", re.I)),
    ("perspectives", re.compile(r"\b(what do (people|others|you all|the council) (think|say|make of)|other (side|angles?|views?|perspectives?)|perspectives?|who('s| is) (affected|hurt|hit)|how does this affect|the council)\b", re.I)),
    ("related", re.compile(r"\b(related|compare|similar|other (companies|stocks|outlets)|what else)\b", re.I)),
]

_TIME_HINT = re.compile(
    r"\b((last|past|previous|next|this) (week|month|few days)|yesterday|today|earlier this week|"
    r"on (monday|tuesday|wednesday|thursday|friday|saturday|sunday)|\d{1,2} (january|february|march|april|may|june|"
    r"july|august|september|october|november|december))\b",
    re.I,
)


def heuristic_route(text: str) -> Route | None:
    """Return a confident route, or None if the utterance needs a model to judge."""
    t = text.strip()
    if not t:
        return Route(intent="backchannel")
    if _BACKCHANNEL.match(t):
        return Route(intent="backchannel")
    # Research cues beat controls: "go back to last week" is research, not "resume".
    needs = sorted({need for need, pat in _RESEARCH_CUES if pat.search(t)})
    time_hint = (m.group(0) if (m := _TIME_HINT.search(t)) else "")
    if needs and not re.search(r"^(ok(ay)?,? )?(go on|carry on|continue|keep going|resume)\W*$", t, re.I):
        # "what's" / "who is" alone are often answerable from the briefing; let the model decide.
        if needs == ["background"]:
            return None
        return Route(intent="escalate", query=t, needs=needs, time_hint=time_hint)
    for action, pat in _CONTROLS:
        if pat.search(t):
            return Route(intent="control", action=action)
    return None


ROUTER_SYSTEM = """You are the turn router for a live spoken news briefing. The listener just interrupted the narrator.
Classify what they said and return JSON.

intent:
- "control": a playback command. action is one of pause, resume, skip, repeat, slower, faster, stop.
- "backchannel": filler or acknowledgement ("yeah", "mm", "right"); nothing to answer.
- "answer": a question whose answer is stated explicitly in BRIEFING CONTEXT. Put a one- or two-sentence spoken
  answer in "answer", in plain conversational English, using only facts from the context. No markdown.
- "escalate": anything else, including any request for more depth, sources, citations, history, timelines,
  other dates, related stories, or facts not stated in the context. When in doubt, escalate.

For "escalate", write "query" as a standalone research question that names the story, company or people
(resolve "this", "it", "that" from the context), and list "needs" from: deep_dive, sources, timeline, perspectives,
background, related, other. Put any time reference the listener used ("last week", "since Monday") in time_hint.
Return empty strings or empty lists for fields that do not apply."""

ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["control", "backchannel", "answer", "escalate"]},
        "action": {"type": "string", "enum": ["", "pause", "resume", "skip", "repeat", "slower", "faster", "stop"]},
        "answer": {"type": "string"},
        "query": {"type": "string"},
        "needs": {
            "type": "array",
            "items": {"type": "string", "enum": ["deep_dive", "sources", "timeline", "background", "related", "perspectives", "other"]},
        },
        "time_hint": {"type": "string"},
    },
    "required": ["intent", "action", "answer", "query", "needs", "time_hint"],
    "additionalProperties": False,
}


class Router:
    def __init__(self, llm: Any | None = None) -> None:
        self.llm = llm  # OpenAIJSON or None

    async def route(self, text: str, ctx: RouteContext) -> Route:
        quick = heuristic_route(text)
        if quick is not None:
            return quick
        if self.llm is None:
            return Route(intent="escalate", query=text, needs=["other"],
                         time_hint=(m.group(0) if (m := _TIME_HINT.search(text)) else ""))
        user = (
            f"BRIEFING: {ctx.briefing_title}\n"
            f"CURRENT STORY: {ctx.current_segment_title}\n"
            f"BRIEFING CONTEXT (what the narrator has said in this story):\n{ctx.spoken_so_far or ctx.current_segment_text}\n\n"
            f"PREVIOUS EXCHANGE:\n{ctx.previous_exchange or '(none)'}\n\n"
            f"LISTENER SAID: {text}"
        )
        try:
            raw = await self.llm.complete(system=ROUTER_SYSTEM, user=user, schema=ROUTER_SCHEMA,
                                          max_tokens=400, timeout=8.0, name="route")
        except Exception as exc:
            log.warning("router LLM failed (%s); escalating", exc)
            return Route(intent="escalate", query=text, needs=["other"])
        route = Route(
            intent=raw.get("intent", "escalate"),
            action=raw.get("action", ""),
            answer=raw.get("answer", ""),
            query=raw.get("query") or text,
            needs=raw.get("needs") or [],
            time_hint=raw.get("time_hint", ""),
            source="llm",
        )
        if route.intent == "answer" and not route.answer.strip():
            route.intent = "escalate"
        if route.intent == "control" and not route.action:
            route.intent = "escalate"
        return route
