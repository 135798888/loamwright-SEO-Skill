"""Cross-model "second opinion" on the finished draft (original design: subskills/
cross-cutting/second-opinion + scripts/_core/llm_judge.py, which nothing ever invoked).

Runs right after the pipeline's own independent reviewer (quality gate 4) has
accepted the draft and BEFORE pre-publish/publish, using a model from a different
family (e.g. Gemini via the same OpenAI-compatible relay) so that "GPT writes, GPT
grades" self-bias gets an outside check.

It only looks at what the reviewer is weakest at — a short list of pass/fail
criteria — and writes memory/workspace/{task}/second-opinion.json.

Modes (llm.yaml → second_opinion.mode):
  off       never runs
  advisory  runs, records, reports (Telegram/report); never blocks       ← default when enabled
  block     a FAIL triggers a surgical repair + re-judge (max_rounds); still failing
            → the article stops before publish. A judge that cannot produce a verdict
            counts as FAIL in block mode (an unreadable verdict is not a pass — Rule 14).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_adapter.config import LLMConfig
from hermes_adapter.llm import ChatClient, LLMError

DEFAULT_CRITERIA = [
    "FACTS: No statement about the manufacturer (MOQ, lead times, capacity, materials, "
    "certifications, years in business, clients) that is absent from or contradicts the "
    "COMPANY FACTS block. No statistic that looks invented or is stated without a source.",
    "BUYER VALUE: Useful to a B2B wholesale buyer / brand owner / importer — concrete "
    "specifications, costs, MOQ/lead-time logic, sourcing risks, decision criteria — not "
    "generic consumer-level filler.",
    "KEYWORD INTENT: The article actually answers the search intent of the primary keyword "
    "within the first sections, not only in passing.",
    "NATURAL WRITING: Reads like an experienced industry writer, not template AI prose "
    "(repetitive structure, empty superlatives, hedging, filler transitions).",
]

_SYSTEM = """You are an independent senior editor at a B2B trade publication, giving a SECOND
OPINION on an article that another AI system wrote and already reviewed. You are deliberately
from a different model family: be skeptical, do not assume earlier checks were right.

Judge ONLY the criteria you are given. For each one decide pass or fail. Fail only for real,
specific problems you can quote; do not fail for taste. For every fail give the exact quote and
a concrete fix.

Return ONLY a JSON object, no prose, no code fences:
{"criteria":[{"name":"<short name>","pass":true|false,"reason":"...","quotes":["..."],
"fix":"..."}],"summary":"<2 sentences>"}"""


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            return text[end + 4:]
    return text


def parse_verdict(raw: str) -> dict[str, Any] | None:
    """Tolerant JSON extraction (code fences, leading prose). None if unusable."""
    if not raw:
        return None
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None
    crit = data.get("criteria")
    if not isinstance(crit, list) or not crit:
        return None
    clean = []
    for c in crit:
        if not isinstance(c, dict) or not isinstance(c.get("pass"), bool):
            return None                       # a criterion without a real boolean is no verdict
        clean.append({"name": str(c.get("name", "")), "pass": c["pass"],
                      "reason": str(c.get("reason", "")), "quotes": c.get("quotes") or [],
                      "fix": str(c.get("fix", ""))})
    return {"criteria": clean, "summary": str(data.get("summary", ""))}


def judge(cfg: LLMConfig, client: ChatClient, ws: Path, *, keyword: str,
          company: dict[str, Any] | None, attempts: int = 2) -> dict[str, Any]:
    draft_path = ws / "draft.md"
    draft = _strip_frontmatter(draft_path.read_text(encoding="utf-8"))
    criteria = cfg.second_opinion_criteria or DEFAULT_CRITERIA
    user = (
        f"PRIMARY KEYWORD: {keyword}\n\n"
        f"COMPANY FACTS (the only allowed source for claims about the manufacturer):\n"
        f"{json.dumps(company or {}, ensure_ascii=False, indent=2)}\n\n"
        "CRITERIA:\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(criteria)) +
        "\n\nARTICLE (markdown; [claim:…] / [IMAGE-SLOT-…] tokens are pipeline markers, ignore "
        "them):\n\n" + draft
    )
    model = cfg.second_opinion_model or cfg.model_for("default")
    verdict: dict[str, Any] | None = None
    raw = ""
    err = ""
    for _ in range(attempts):
        try:
            res = client.chat(model=model, stage="second-opinion", tools=None,
                              messages=[{"role": "system", "content": _SYSTEM},
                                        {"role": "user", "content": user}])
            raw = res.message.get("content") or ""
            verdict = parse_verdict(raw)
            if verdict:
                break
            err = "judge reply was not a valid verdict JSON"
        except LLMError as e:
            err = str(e)
    out: dict[str, Any] = {
        "_generated_by": "hermes-second-opinion",
        "model": model,
        "mode": cfg.second_opinion_mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "draft_sha256": hashlib.sha256(draft_path.read_bytes()).hexdigest(),
    }
    if verdict is None:
        out.update({"verdict": "ERROR", "error": err, "raw_tail": raw[-800:]})
    else:
        failed = [c for c in verdict["criteria"] if not c["pass"]]
        out.update({"verdict": "FAIL" if failed else "PASS", **verdict,
                    "failed": [c["name"] for c in failed]})
    (ws / "second-opinion.json").write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    return out


def repair_brief(result: dict[str, Any]) -> str:
    lines = [f"A second-opinion editor ({result.get('model')}) failed these criteria:"]
    for c in result.get("criteria", []):
        if not c.get("pass"):
            lines.append(f"- {c['name']}: {c['reason']}")
            for q in c.get("quotes", [])[:5]:
                lines.append(f"    quote: {q}")
            if c.get("fix"):
                lines.append(f"    suggested fix: {c['fix']}")
    lines.append("Fix exactly these problems in draft.md. If a claim about the company is not in "
                 "business-context.json :: company, DELETE it rather than rewording it.")
    return "\n".join(lines)
