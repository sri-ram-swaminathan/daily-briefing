"""Thin async wrapper around the OpenAI Responses API for JSON-shaped answers.

Same provider and conventions as ``council.py`` / ``briefing.py``: the
Responses API, ``OPENAI_MODEL`` (default ``gpt-4.1-mini``) and ``store=False``.
The router and the research desk pass plain JSON schemas, so they use
``text.format = json_schema`` with ``strict`` instead of Pydantic models; that
keeps the turn-taking modules importable without the SDK.

The OpenAI SDK is imported lazily so the rest of the package (and its tests)
runs without it.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class OpenAIJSON:
    def __init__(self, model: str) -> None:
        import openai  # lazy

        self._openai = openai
        self._client = openai.AsyncOpenAI(max_retries=1)
        self.model = model

    async def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 4000,
        timeout: float = 30.0,
        name: str = "reply",
        web_search: bool | str = False,
    ) -> dict[str, Any]:
        """One request -> one JSON object matching ``schema``.

        With ``web_search`` the model may call OpenAI's hosted web search tool first.
        The URLs it actually cited come back under the extra key ``_web_citations``
        ([{"url", "title"}]), read from the response annotations rather than from the
        model's own text, so a listener is never sent to a made-up link.
        """
        extra: dict[str, Any] = {}
        if web_search:
            extra["tools"] = [{"type": "web_search"}]
            extra["include"] = ["web_search_call.action.sources"]
            if web_search == "required":  # dig-deeper turns must bring new facts, not rephrase old ones
                extra["tool_choice"] = "required"
        try:
            resp = await self._client.with_options(timeout=timeout).responses.create(
                model=self.model,
                instructions=system,
                input=user,
                max_output_tokens=max_tokens,
                text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
                store=False,
                **extra,
            )
        except self._openai.OpenAIError as exc:
            raise LLMError(f"OpenAI request failed: {exc}") from exc

        if resp.status != "completed":
            raise LLMError(f"incomplete response ({resp.status})")
        for item in resp.output:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", "") == "refusal":
                    raise LLMError("request declined")
        text = resp.output_text
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"invalid JSON from model: {text[:200]!r}") from exc
        if web_search:
            seen: dict[str, dict[str, str]] = {}
            for item in resp.output:
                for part in getattr(item, "content", None) or []:
                    for ann in getattr(part, "annotations", None) or []:
                        if getattr(ann, "type", "") == "url_citation" and ann.url not in seen:
                            seen[ann.url] = {"url": ann.url, "title": getattr(ann, "title", "") or ""}
            if not seen:  # no inline citations: fall back to the pages the search actually returned
                for item in resp.output:
                    if getattr(item, "type", "") == "web_search_call":
                        for src in getattr(getattr(item, "action", None), "sources", None) or []:
                            url = getattr(src, "url", None) or (src.get("url") if isinstance(src, dict) else None)
                            if url and url not in seen and len(seen) < 4:
                                seen[url] = {"url": url, "title": ""}
            data["_web_citations"] = list(seen.values())
        return data
