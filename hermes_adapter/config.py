"""Adapter configuration: which OpenAI-compatible endpoint, which model per role.

Lives at ``~/.xuanran-seo/llm.yaml`` (outside the repo, next to the plugin's own
config.yaml and credentials). Every field can be overridden by environment
variables so a Hermes/cron invocation can pin things without editing the file:

    LW_LLM_CONFIG      path to an alternative llm.yaml
    LW_LLM_BASE_URL    e.g. https://relay.example.com/v1
    LW_LLM_API_KEY     the key (preferred over putting it in the yaml)
    LW_LLM_MODEL       default model for every role without an override

See ``hermes_adapter/llm.example.yaml`` for an annotated template.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path.home() / ".xuanran-seo" / "llm.yaml"

# Role names are the agent names under agents/*.md (writer, fact-checker, ...),
# plus "inline" for stages with no subagent (format-selector, outline-architect,
# meta-builder, citation-capsule-builder) and "repair" for gate repairs.


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    models: dict[str, str] = field(default_factory=dict)
    stage_models: dict[str, str] = field(default_factory=dict)
    prices: dict[str, tuple[Decimal, Decimal]] = field(default_factory=dict)
    timeout_seconds: float = 600.0
    max_output_tokens: int = 16000
    temperature: float | None = None
    vision: bool = True
    # "auto" | "max_tokens" | "max_completion_tokens". OpenAI reasoning models (gpt-5*, o-series)
    # reject max_tokens; auto picks by model name and self-corrects on the endpoint's 400.
    token_param: str = "auto"
    reasoning_effort: str | None = None    # e.g. "low" | "medium" | "high" (reasoning models only)
    # cross-model second opinion after the independent reviewer (see second_opinion.py)
    second_opinion_mode: str = "off"        # off | advisory | block
    second_opinion_model: str | None = None
    second_opinion_criteria: list[str] = field(default_factory=list)
    second_opinion_rounds: int = 2
    extra_headers: dict[str, str] = field(default_factory=dict)
    # relay overloads (502 server_is_overloaded) can last many minutes: keep retrying for up
    # to this long, and after a few failures switch to the model's configured fallback.
    overload_wait_minutes: float = 20.0
    fallback_models: dict[str, str] = field(default_factory=dict)
    # limits
    max_llm_usd_per_article: Decimal = Decimal(15)
    writer_parallelism: int = 4
    max_turns_cap: int = 120
    repair_rounds: int = 3
    llm_stage_retries: int = 2
    context_char_budget: int = 600_000
    tool_output_char_cap: int = 40_000
    # notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    def model_for(self, role: str, stage: str | None = None) -> str:
        if stage and stage in self.stage_models:
            return self.stage_models[stage]
        if role in self.models:
            return self.models[role]
        return self.models["default"]

    def price_for(self, model: str) -> tuple[Decimal, Decimal] | None:
        return self.prices.get(model)


class ConfigError(RuntimeError):
    pass


def _env_or(cfg_val: Any, env_name: str) -> Any:
    v = os.environ.get(env_name, "").strip()
    return v if v else cfg_val


def load_config(path: Path | None = None) -> LLMConfig:
    path = Path(os.environ.get("LW_LLM_CONFIG", "") or path or DEFAULT_CONFIG_PATH)
    raw: dict[str, Any] = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    base_url = _env_or(raw.get("base_url", ""), "LW_LLM_BASE_URL")
    api_key = os.environ.get("LW_LLM_API_KEY", "").strip()
    if not api_key:
        key_env = raw.get("api_key_env")
        if key_env:
            api_key = os.environ.get(str(key_env), "").strip()
    if not api_key:
        api_key = str(raw.get("api_key", "") or "").strip()

    if not base_url:
        raise ConfigError(
            f"No LLM base_url configured. Set LW_LLM_BASE_URL or base_url in {path} "
            "(see hermes_adapter/llm.example.yaml)."
        )
    if not api_key:
        raise ConfigError(
            f"No LLM API key configured. Set LW_LLM_API_KEY, or api_key_env/api_key in {path}."
        )

    models = {str(k): str(v) for k, v in (raw.get("models") or {}).items() if v}
    env_model = os.environ.get("LW_LLM_MODEL", "").strip()
    if env_model:
        models["default"] = env_model
    if "default" not in models:
        raise ConfigError(f"models.default is required in {path} (or set LW_LLM_MODEL).")

    prices: dict[str, tuple[Decimal, Decimal]] = {}
    for m, pair in (raw.get("prices") or {}).items():
        try:
            prices[str(m)] = (Decimal(str(pair[0])), Decimal(str(pair[1])))
        except (TypeError, IndexError, ValueError) as e:
            raise ConfigError(f"prices.{m} must be [input_usd_per_1M, output_usd_per_1M]: {e}") from e

    limits = raw.get("limits") or {}
    so = raw.get("second_opinion") or {}
    so_mode = str(so.get("mode", "off")).lower()
    if so_mode not in ("off", "advisory", "block"):
        raise ConfigError(f"second_opinion.mode must be off | advisory | block (got {so_mode!r})")
    if so_mode != "off" and not so.get("model"):
        raise ConfigError("second_opinion.model is required when second_opinion.mode is not off")
    tg = raw.get("telegram") or {}
    tg_token = os.environ.get(str(tg.get("bot_token_env") or "TG_BOT_TOKEN"), "").strip() \
        or str(tg.get("bot_token", "") or "")
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip() or str(tg.get("chat_id", "") or "")

    return LLMConfig(
        base_url=str(base_url).rstrip("/"),
        api_key=api_key,
        models=models,
        stage_models={str(k): str(v) for k, v in (raw.get("stage_models") or {}).items() if v},
        prices=prices,
        timeout_seconds=float(raw.get("timeout_seconds", 600)),
        max_output_tokens=int(raw.get("max_output_tokens", 16000)),
        temperature=(float(raw["temperature"]) if raw.get("temperature") is not None else None),
        vision=bool(raw.get("vision", True)),
        token_param=str(raw.get("token_param", "auto")),
        reasoning_effort=(str(raw["reasoning_effort"]) if raw.get("reasoning_effort") else None),
        second_opinion_mode=so_mode,
        second_opinion_model=(str(so["model"]) if so.get("model") else None),
        second_opinion_criteria=[str(c) for c in (so.get("criteria") or [])],
        second_opinion_rounds=int(so.get("max_rounds", 2)),
        extra_headers={str(k): str(v) for k, v in (raw.get("extra_headers") or {}).items()},
        overload_wait_minutes=float(raw.get("overload_wait_minutes", 20)),
        fallback_models={str(k): str(v) for k, v in (raw.get("fallback_models") or {}).items() if v},
        max_llm_usd_per_article=Decimal(str(limits.get("max_llm_usd_per_article", "15"))),
        writer_parallelism=int(limits.get("writer_parallelism", 4)),
        max_turns_cap=int(limits.get("max_turns_cap", 120)),
        repair_rounds=int(limits.get("repair_rounds", 3)),
        llm_stage_retries=int(limits.get("llm_stage_retries", 2)),
        context_char_budget=int(limits.get("context_char_budget", 600_000)),
        tool_output_char_cap=int(limits.get("tool_output_char_cap", 40_000)),
        telegram_bot_token=tg_token,
        telegram_chat_id=tg_chat,
    )
