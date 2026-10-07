"""Minimal OpenAI-compatible chat-completions client (works with relays / 中转站).

Only the subset the agent loop needs: messages + tools (function calling),
usage accounting, and retry on transient failures. Uses httpx (repo convention)
instead of the openai SDK so odd relay URL layouts and headers are easy to handle.
"""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from hermes_adapter.config import LLMConfig

_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}


class LLMError(RuntimeError):
    pass


class BudgetExceeded(LLMError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    usd: Decimal = Decimal(0)
    unpriced_models: set[str] = field(default_factory=set)


class CostTracker:
    """Per-article LLM spend, shared by every agent of one task (thread-safe).

    Also mirrors each call into the plugin's cost ledger so the existing daily /
    weekly / monthly caps in ~/.xuanran-seo/config.yaml keep working.
    """

    def __init__(self, cfg: LLMConfig, task_id: str | None, project_slug: str | None):
        self.cfg = cfg
        self.task_id = task_id
        self.project_slug = project_slug
        self.usage = Usage()
        self._lock = threading.Lock()

    def check_budget(self) -> None:
        with self._lock:
            if self.usage.usd >= self.cfg.max_llm_usd_per_article:
                raise BudgetExceeded(
                    f"LLM spend for this article reached ${self.usage.usd} "
                    f"(limit max_llm_usd_per_article=${self.cfg.max_llm_usd_per_article})."
                )

    def record(self, model: str, prompt_tokens: int, completion_tokens: int, stage: str) -> Decimal:
        price = self.cfg.price_for(model)
        cost = Decimal(0)
        if price:
            cost = (Decimal(prompt_tokens) / Decimal(1_000_000)) * price[0] + \
                   (Decimal(completion_tokens) / Decimal(1_000_000)) * price[1]
            cost = cost.quantize(Decimal("0.0001"))
        with self._lock:
            self.usage.prompt_tokens += prompt_tokens
            self.usage.completion_tokens += completion_tokens
            self.usage.calls += 1
            self.usage.usd += cost
            if not price:
                self.usage.unpriced_models.add(model)
        try:
            from scripts._core import cost_ledger
            cost_ledger.log(
                cost, model=model, endpoint="chat.completions(hermes_adapter)",
                task_id=self.task_id, project_slug=self.project_slug,
                extra={"stage": stage, "in_tokens": prompt_tokens, "out_tokens": completion_tokens,
                       "priced": bool(price)},
            )
        except Exception:  # noqa: BLE001 — accounting must never break a billed call
            pass
        return cost


@dataclass
class ChatResult:
    message: dict[str, Any]
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int


class ChatClient:
    def __init__(self, cfg: LLMConfig, tracker: CostTracker | None = None):
        self.cfg = cfg
        self.tracker = tracker

    def _url(self) -> str:
        base = self.cfg.base_url
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def chat(self, *, model: str, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None, stage: str,
             max_attempts: int = 6) -> ChatResult:
        if self.tracker:
            self.tracker.check_budget()
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self.cfg.max_output_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        headers = {"Authorization": f"Bearer {self.cfg.api_key}",
                   "Content-Type": "application/json", **self.cfg.extra_headers}

        last_err = ""
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=self.cfg.timeout_seconds) as client:
                    r = client.post(self._url(), headers=headers, content=json.dumps(body))
                if r.status_code in _TRANSIENT_STATUS:
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    raise _Retry()
                if r.status_code >= 400:
                    raise LLMError(f"HTTP {r.status_code} from LLM endpoint: {r.text[:800]}")
                data = r.json()
                if "error" in data and not data.get("choices"):
                    msg = json.dumps(data["error"])[:600]
                    # Relays often wrap upstream overloads as 200 + error body.
                    if any(s in msg.lower() for s in ("overload", "rate", "timeout", "busy", "capacity")):
                        last_err = msg
                        raise _Retry()
                    raise LLMError(f"LLM endpoint returned error: {msg}")
                choices = data.get("choices") or []
                if not choices:
                    last_err = f"no choices in response: {str(data)[:300]}"
                    raise _Retry()
                choice = choices[0]
                msg = choice.get("message") or {}
                usage = data.get("usage") or {}
                pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                if self.tracker:
                    self.tracker.record(model, pt, ct, stage)
                return ChatResult(message=msg, finish_reason=str(choice.get("finish_reason") or ""),
                                  prompt_tokens=pt, completion_tokens=ct)
            except (_Retry, httpx.TransportError, httpx.TimeoutException, json.JSONDecodeError) as e:
                if not isinstance(e, _Retry):
                    last_err = f"{type(e).__name__}: {e}"
                if attempt == max_attempts:
                    break
                time.sleep(min(60.0, (2 ** attempt) + random.random() * 2))
        raise LLMError(f"LLM call failed after {max_attempts} attempts: {last_err}")


class _Retry(Exception):
    pass
