"""Short lines the voice agent says in its own voice: acknowledgements, bridges, holds.

They are kept short and varied so repeated interruptions don't sound robotic.
Escalation acknowledgements are synthesized when the session starts, so the
"I don't have that yet" line plays the moment the listener stops talking.
"""

from __future__ import annotations

import random

ESCALATE_ACKS: dict[str, list[str]] = {
    "sources": [
        "Good question. I don't have the sources in front of me yet, let me pull them up.",
        "I don't have the sourcing to hand yet. Give me a second to check.",
    ],
    "timeline": [
        "I don't have the full backstory loaded yet. Let me dig it up.",
        "Good question. I don't know how it got here yet, let me check the earlier reporting.",
    ],
    "deep_dive": [
        "I don't have more on that yet. Let me dig deeper.",
        "Let me dig into that. I don't have the detail in front of me yet.",
    ],
    "perspectives": [
        "I haven't heard what the council makes of this yet. Let me ask around.",
        "Good question. I don't have the other angles in front of me yet, let me check with the council.",
    ],
    "other": [
        "Hmm, I don't know that one yet. Let me check with the research desk.",
        "Good question. I don't have that in front of me yet, give me a moment.",
    ],
}

HOLD_LINES = [
    "Still digging. There's quite a bit of coverage on this.",
    "Nearly there, just pulling it together.",
]

BRIDGES = [
    "Okay, back to the briefing.",
    "Right, picking up where we left off.",
    "So, as I was saying.",
    "Anyway, back to it.",
]

BRIDGES_WITH_TITLE = [
    "Okay, back to {title}.",
    "Right, back to {title}.",
]

PAUSE = "Sure, I'll pause here. Say carry on when you're ready."
SKIP = "Okay, moving on."
STOP = "Okay, I'll stop here."
SLOWER = "Sure, I'll slow down a little."
FASTER = "Sure, I'll pick up the pace."
REPEAT = "Sure."
END = "That's the end of the briefing. If you want to go deeper on anything, just ask."
FAIL = "Sorry, I couldn't get that from the research desk just now. Let's carry on, and you can ask me again later."
FOLLOW_UP_DEFAULT = "Want me to keep digging, or shall we get back to the briefing?"
AFTER_END_FOLLOW_UP = "Anything else you'd like to know?"


def ack_for(needs: list[str], rng: random.Random | None = None) -> str:
    rng = rng or random
    for need in ("perspectives", "sources", "timeline", "deep_dive"):
        if need in needs:
            return rng.choice(ESCALATE_ACKS[need])
    return rng.choice(ESCALATE_ACKS["other"])


def bridge(title: str = "", rng: random.Random | None = None) -> str:
    rng = rng or random
    if title and rng.random() < 0.5:
        return rng.choice(BRIDGES_WITH_TITLE).format(title=title)
    return rng.choice(BRIDGES)


def all_prerender_lines() -> list[str]:
    lines = [line for group in ESCALATE_ACKS.values() for line in group]
    return lines + HOLD_LINES + BRIDGES + [PAUSE, SKIP, SLOWER, FASTER, END, FAIL, FOLLOW_UP_DEFAULT]
