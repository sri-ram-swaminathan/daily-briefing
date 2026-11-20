"""Live news voice agent: interruptible, NotebookLM-style spoken briefings.

Layers (see docs/ARCHITECTURE.md):

    briefing.py (summary agent) --script--> VoiceSession <--ws--> browser (mic + speaker)
                                  |   ^
                    route(utterance)  | spoken segments
                                  v   |
                              MainAgent (research desk: dataset + council reviews)

Everything in this package except ``server.py``, ``llm.py`` and the OpenAI
paths of ``router.py`` / ``main_agent.py`` is stdlib-only so the turn-taking
logic can be tested without network access or API keys.
"""
