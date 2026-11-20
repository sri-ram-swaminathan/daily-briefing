"""Named delivery styles for a briefing: tone (voice settings) + style notes.

``tone`` steers the ElevenLabs voice settings (see ``tts.voice_settings_for``)
and, with ``eleven_v3``, an inline audio tag. ``style`` is passed to the
research desk so its spoken answers match the briefing.
"""

from __future__ import annotations

from .script import Delivery

PRESETS: dict[str, dict] = {
    "Coffee with a friend": {
        "tone": "warm, relaxed, conversational",
        "style": "Talk to one person over breakfast. Short sentences, the odd casual aside, and say why it "
                 "matters to a normal person. Never sensational.",
        "pace": 1.0,
    },
    "Late-night radio": {
        "tone": "soothing, gentle, calm",
        "style": "Unhurried. Let sentences breathe. No hype words. Paint the picture before the numbers.",
        "pace": 0.94,
    },
    "The storyteller": {
        "tone": "curious, warm, measured",
        "style": "Open with a hook or a small scene, then the facts. End with what to watch next.",
        "pace": 0.98,
    },
    "Dry wit": {
        "tone": "calm, playful, measured",
        "style": "Deliver the facts straight, with a light ironic aside now and then. Never mock people or "
                 "tragedies; the wit is for corporate drama and absurd numbers.",
        "pace": 1.0,
    },
    "3-minute sprint": {
        "tone": "brisk, friendly, upbeat",
        "style": "Headline, one key fact, why it matters. At most three sentences per story.",
        "pace": 1.06,
    },
    "Calm explainer": {
        "tone": "calm, reassuring, curious",
        "style": "Assume a smart listener with no background. Define one term per story in plain words and "
                 "use one everyday analogy.",
        "pace": 0.97,
    },
}

DEFAULT_PRESET = "Coffee with a friend"


def delivery_for(name: str | None) -> Delivery:
    p = PRESETS.get(name or "", PRESETS[DEFAULT_PRESET])
    return Delivery(tone=p["tone"], style=p["style"], pace=p["pace"])
