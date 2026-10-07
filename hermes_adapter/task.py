"""Create a fresh article workspace (memory/workspace/{task_id}/state.json).

This is the step the seo-blog SKILL.md performs before handing control to
run_pipeline ("Create workspace + state.json", then the MANDATORY
local_intent_runner Bash invocation). Done in code here so it is identical on
every run.
"""
from __future__ import annotations

import json
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from hermes_adapter.tools import PLUGIN_ROOT
from scripts._core import file_bus


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_task(*, project_slug: str, keyword: str, secondary: list[str] | None = None,
                locale: str = "en-US", word_count: int | None = None,
                image_count: int | None = None, template_id: str | None = None,
                intent: str | None = None, extra_brief: dict[str, Any] | None = None) -> str:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("keyword is required")
    if not (PLUGIN_ROOT / "projects" / project_slug / "business-context.json").exists():
        raise FileNotFoundError(
            f"projects/{project_slug}/business-context.json not found — bootstrap the project first "
            f"(python -m hermes_adapter.bootstrap_project …)."
        )
    # NOT file_bus.new_task_id(): it returns "YYYYMMDD-xxxxxxxx", whose hyphen fails
    # schemas/state.schema.json's task_id pattern ^[a-z0-9_]{8,32}$ (write_state would
    # raise). Under Claude Code the LLM invented ids itself, so the mismatch never bit.
    task_id = datetime.now(timezone.utc).strftime("%Y%m%d") + "_" + secrets.token_hex(4)
    brief: dict[str, Any] = {
        "keywords": [keyword] + [k.strip() for k in (secondary or []) if k.strip()],
        "primary_keyword": keyword,
        "target_market_locale": locale,
    }
    if word_count:
        brief["word_count_target"] = int(word_count)
    if image_count is not None:
        brief["image_count"] = int(image_count)
    if template_id:
        brief["template_id"] = template_id
    if intent:
        brief["intent_override"] = intent
    if extra_brief:
        brief.update(extra_brief)

    state = {
        "task_id": task_id,
        "project_slug": project_slug,
        "command": "article",
        "created_at": _now(),
        "updated_at": _now(),
        "phase": "research",
        "current_stage": "research",
        "brief": brief,
        "stage_history": [],
    }
    file_bus.write_state(task_id, state)

    # MANDATORY per skills/seo-blog/SKILL.md step 5 — the only sanctioned way to set
    # brief.local_mode / brief.location_anchor.
    proc = subprocess.run(
        [sys.executable, "-m", "scripts._core.local_intent_runner", "--task-id", task_id,
         "--project-slug", project_slug, "--keyword", keyword, "--json"],
        cwd=str(PLUGIN_ROOT), capture_output=True, text=True,
    )
    if proc.returncode not in (0,):
        sys.stderr.write(f"⚠ local_intent_runner exit {proc.returncode}: "
                         f"{(proc.stderr or proc.stdout)[-400:]}\n")
    return task_id


def read_state(task_id: str) -> dict[str, Any]:
    return json.loads((PLUGIN_ROOT / "memory" / "workspace" / task_id / "state.json")
                      .read_text(encoding="utf-8"))
