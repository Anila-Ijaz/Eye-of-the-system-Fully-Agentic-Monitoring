"""Minimal OpenAI-compatible ChatClient for L5 menu selection."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


@dataclass
class LLMUsageStats:
    """Per-run usage counters for ChatClient."""

    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


def extract_first_json_object(raw: str) -> dict[str, Any]:
    """Strip code fences/reasoning text and parse the first JSON object."""
    text = raw.strip()
    if not text:
        raise ValueError("LLM returned empty response content")

    candidates = [text]
    for match in _CODE_FENCE_RE.finditer(text):
        candidates.insert(0, match.group(1).strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    raise ValueError(f"Could not extract valid JSON object from LLM response: {raw!r}")


class ChatClient:
    """Generic OpenAI-compatible chat client with a single ``complete_json`` method."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        resolved_url = base_url if base_url is not None else os.environ.get("LLM_BASE_URL", "")
        resolved_model = model if model is not None else os.environ.get("LLM_MODEL", "")
        resolved_key = api_key if api_key is not None else os.environ.get("LLM_API_KEY", "")

        missing = [
            name
            for name, val in (
                ("LLM_BASE_URL", resolved_url),
                ("LLM_MODEL", resolved_model),
                ("LLM_API_KEY", resolved_key),
            )
            if not val or not val.strip()
        ]
        if missing:
            raise ValueError(
                f"Missing required LLM environment variable(s): {', '.join(missing)}"
            )

        self.base_url = resolved_url.strip()
        self.model = resolved_model.strip()
        self.timeout = float(timeout)
        self.usage = LLMUsageStats()
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=resolved_key.strip(),
            timeout=self.timeout,
            max_retries=1,
        )

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """Send a chat completion request and return the parsed JSON object."""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            max_tokens=400,
        )
        self.usage.calls += 1
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage.tokens_in += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.usage.tokens_out += int(getattr(usage, "completion_tokens", 0) or 0)

        content = response.choices[0].message.content if response.choices else ""
        if not content:
            raise ValueError("LLM returned empty response content")
        return extract_first_json_object(content)
