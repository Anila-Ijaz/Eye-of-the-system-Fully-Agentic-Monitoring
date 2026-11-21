"""L5 Choosers: deterministic FallbackChooser and cached LLMChooser."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from agent.llm_client import ChatClient
from agent.menu import MenuItem
from agent.state import InvestigationState

CacheMode = Literal["replay", "record", "live"]

_SYSTEM_PROMPT = (
    "You select the next investigation step from a numbered menu.\n"
    "Reply with ONE JSON object ONLY, no extra prose if possible.\n"
    "Required keys: \"menu_number\" (int from menu, or null if stopping), "
    "\"decision\" (\"continue\" or \"stop\"), \"reason\" (short string, max 160 chars).\n"
    "Example: {\"menu_number\": 1, \"decision\": \"continue\", \"reason\": \"Decompose orders to isolate driver\"}"
)


@dataclass(frozen=True)
class Choice:
    """Selected next step or stop decision."""

    item_id: str | None
    reason: str
    decision: Literal["continue", "stop"]
    stop_reason: str = ""
    used_fallback: bool = False
    fallback_reason: str = ""


class Chooser(Protocol):
    """Protocol for L5 step selection."""

    def choose(self, state: InvestigationState, menu: list[MenuItem]) -> Choice: ...


class FallbackChooser:
    """Deterministic chooser picking max ``expected_gain / estimated_cost``."""

    def __init__(self, min_gain: float = 0.05) -> None:
        self.min_gain = float(min_gain)

    def choose(self, state: InvestigationState, menu: list[MenuItem]) -> Choice:
        del state
        if not menu:
            return Choice(None, "Menu is empty", "stop", stop_reason="menu_empty")

        viable = [m for m in menu if m.expected_gain >= self.min_gain]
        if not viable:
            return Choice(
                None,
                f"All remaining items have expected_gain < {self.min_gain}",
                "stop",
                stop_reason="min_gain_reached",
            )

        best = max(
            viable,
            key=lambda m: (m.expected_gain / max(m.estimated_cost, 1e-9), -m.number),
        )
        ratio = best.expected_gain / max(best.estimated_cost, 1e-9)
        return Choice(
            item_id=best.id,
            reason=f"highest expected gain per cost ({best.expected_gain:.3f} / {best.estimated_cost:.2f} = {ratio:.3f})",
            decision="continue",
        )


def _format_menu_prompt(state: InvestigationState, menu: list[MenuItem]) -> str:
    lines = [state.compact_summary(), "Menu:"]
    for item in menu:
        lines.append(
            f"{item.number}. [{item.kind}] {item.description} "
            f"(gain={item.expected_gain:.2f}, cost={item.estimated_cost:.2f})"
        )
    return "\n".join(lines)


def _cache_key(model: str, system: str, user: str) -> str:
    payload = f"{model}\n---\n{system}\n---\n{user}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class LLMChooser:
    """Chooser backed by ChatClient with file-based caching and safe fallback."""

    def __init__(
        self,
        client: ChatClient | None = None,
        fallback: FallbackChooser | None = None,
        cache_dir: str | Path | None = None,
        mode: CacheMode = "live",
        model_name: str | None = None,
    ) -> None:
        self.client = client
        self.fallback = fallback or FallbackChooser()
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if mode not in {"replay", "record", "live"}:
            raise ValueError(f"Invalid cache mode: {mode!r}")
        self.mode: CacheMode = mode
        self.model_name = model_name or (client.model if client is not None else "unknown")

    def _fallback_choice(self, state: InvestigationState, menu: list[MenuItem], why: str) -> Choice:
        fb = self.fallback.choose(state, menu)
        return Choice(
            item_id=fb.item_id,
            reason=fb.reason,
            decision=fb.decision,
            stop_reason=fb.stop_reason,
            used_fallback=True,
            fallback_reason=f"fallback_used: {why}",
        )

    def _cache_path(self, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{key}.json"

    def choose(self, state: InvestigationState, menu: list[MenuItem]) -> Choice:
        if not menu:
            return Choice(None, "Menu is empty", "stop", stop_reason="menu_empty")

        user_prompt = _format_menu_prompt(state, menu)
        key = _cache_key(self.model_name, _SYSTEM_PROMPT, user_prompt)
        cache_file = self._cache_path(key)

        raw_reply: dict[str, Any] | None = None
        if self.mode == "replay":
            if cache_file is None or not cache_file.exists():
                raise KeyError(f"Missing LLM cache entry for key {key} in replay mode")
            raw_reply = json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            if self.client is None:
                try:
                    self.client = ChatClient()
                    self.model_name = self.client.model
                except Exception as exc:
                    return self._fallback_choice(state, menu, str(exc))
            try:
                raw_reply = self.client.complete_json(_SYSTEM_PROMPT, user_prompt)
            except Exception as exc:
                return self._fallback_choice(state, menu, f"LLM error: {exc}")

            if self.mode == "record" and cache_file is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(raw_reply, indent=2), encoding="utf-8")

        return self._validate_reply(raw_reply, state, menu)

    def _validate_reply(
        self, reply: dict[str, Any], state: InvestigationState, menu: list[MenuItem]
    ) -> Choice:
        if not isinstance(reply, dict):
            return self._fallback_choice(state, menu, "LLM reply is not a JSON object")

        decision = reply.get("decision")
        if decision not in {"continue", "stop"}:
            return self._fallback_choice(state, menu, f"invalid decision: {decision!r}")

        reason = reply.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return self._fallback_choice(state, menu, "empty or non-string reason")
        clean_reason = reason.strip()[:200]

        if decision == "stop":
            return Choice(None, clean_reason, "stop", stop_reason="chooser_stopped")

        menu_num = reply.get("menu_number")
        if isinstance(menu_num, bool) or not isinstance(menu_num, int):
            return self._fallback_choice(state, menu, f"non-integer menu_number: {menu_num!r}")

        by_num = {item.number: item for item in menu}
        if menu_num not in by_num:
            return self._fallback_choice(state, menu, f"off-menu number: {menu_num}")

        return Choice(item_id=by_num[menu_num].id, reason=clean_reason, decision="continue")
