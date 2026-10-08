"""Drive ONE article workspace from creation to a verified WordPress draft.

This is the piece that replaces "Claude Code reading skills/seo-blog/SKILL.md and
following The Loop". The deterministic engine is untouched:

    scripts/pipeline/run_pipeline.drive()   ← runs every BASH/BACKGROUND/CHECK stage,
                                               verifies + records, enforces ordering,
                                               stops for LLM stages and failed gates
    hermes_adapter (this file)              ← services exactly those stops:
        DISPATCH_LLM  → isolated subagent (agents/<name>.md, its tool whitelist) or an
                        inline stage; section-drafter fans out one writer per H2
        GATE_FAILED   → bounded repair round, then let the runner re-run the gate
        ERROR (LLM)   → content-gate repair and/or re-dispatch with the error attached
        WAIT / LOCKED → back off and re-invoke
        COMPLETE      → done (draft created + live checks passed)

Nothing here can mark a stage complete: only orchestrator.verify_stage() does that.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from hermes_adapter.agent import (
    REPAIR_TOOLS,
    AgentResult,
    load_agent,
    run_agent,
    run_inline,
    run_subagent,
)
from hermes_adapter.config import LLMConfig
from hermes_adapter.llm import BudgetExceeded, ChatClient, CostTracker, LLMError
from hermes_adapter.tools import PLUGIN_ROOT

WS_ROOT = PLUGIN_ROOT / "memory" / "workspace"

# Gate → which agent repairs it. Anything not listed gets the generic repair agent,
# which edits draft.md surgically (repair-orchestrator Level 1).
_CTA_GATES = {"cta-diversity-check", "cta-tone-check"}
# LLM stages whose verify can fail on a CONTENT verdict (low review score,
# FIX_REQUIRED/BLOCK_PUBLISH fact-check). Re-dispatching alone would just re-judge the
# same draft, so the draft is repaired first.
_CONTENT_GATED_LLM = {"independent-reviewer": "review.json",
                      "fact-check-and-citation": "fact-check.json"}


@dataclass
class RunReport:
    task_id: str
    project_slug: str | None
    keyword: str
    status: str = "running"            # complete | failed | budget_exceeded
    detail: str = ""
    stage: str | None = None
    post_id: int | None = None
    preview_url: str | None = None
    llm_usd: str = "0"
    llm_calls: int = 0
    unpriced_models: list[str] = field(default_factory=list)
    second_opinion: str | None = None      # PASS | FAIL | ERROR (None = not run)
    second_opinion_failed: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "events"} | {
            "event_count": len(self.events)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Driver:
    def __init__(self, cfg: LLMConfig, task_id: str, *,
                 on_event: Callable[[dict[str, Any]], None] | None = None,
                 pipeline_drive: Callable[..., dict[str, Any]] | None = None):
        self.cfg = cfg
        self.task_id = task_id
        self.ws = WS_ROOT / task_id
        state = json.loads((self.ws / "state.json").read_text(encoding="utf-8"))
        self.slug = state.get("project_slug")
        self.brief = state.get("brief") or {}
        self.tracker = CostTracker(cfg, task_id, self.slug)
        self.client = ChatClient(cfg, self.tracker)
        self.on_event = on_event
        if pipeline_drive is None:
            from scripts.pipeline import run_pipeline
            pipeline_drive = run_pipeline.drive
        self._drive = pipeline_drive
        self.report = RunReport(task_id, self.slug, self.brief.get("primary_keyword", ""),
                                started_at=_now())
        self.llm_attempts: dict[str, int] = defaultdict(int)
        self.gate_repairs: dict[str, int] = defaultdict(int)
        self.bash_errors: dict[str, int] = defaultdict(int)
        self.retry_notes: dict[str, str] = {}
        self.second_opinion_done = False

    # ── logging ─────────────────────────────────────────────────
    def event(self, kind: str, **data: Any) -> None:
        ev = {"ts": _now(), "kind": kind, **data}
        self.report.events.append(ev)
        try:
            with (self.ws / "hermes-run.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001
                pass

    # ── main loop ───────────────────────────────────────────────
    def run(self, max_iterations: int = 500, max_wait_s: int = 3600) -> RunReport:
        completed_llm: str | None = None
        waited = 0
        try:
            for _ in range(max_iterations):
                just_completed = completed_llm
                r = self._drive(self.task_id, completed_llm=completed_llm)
                completed_llm = None
                action = r.get("action")
                stage = r.get("stage")
                self.event("runner", action=action, stage=stage,
                           steps=[s.get("stage") for s in r.get("steps_run") or []])

                # Second opinion: right after the runner ACCEPTED the independent reviewer
                # (anything but an ERROR on that stage), before pre-publish / publish.
                if (just_completed == "independent-reviewer" and not self.second_opinion_done
                        and not (action == "ERROR" and stage == "independent-reviewer")):
                    self.second_opinion_done = True
                    stop = self._second_opinion()
                    if stop:
                        return self._finish("failed", stop, "second-opinion")

                if action == "COMPLETE":
                    return self._finish("complete", "pipeline complete (draft + live checks passed)")

                if action == "DISPATCH_LLM":
                    if self._dispatch(r):
                        completed_llm = stage
                    continue

                if action == "GATE_FAILED":
                    self.gate_repairs[stage] += 1
                    if self.gate_repairs[stage] > self.cfg.repair_rounds:
                        return self._finish("failed", f"gate {stage} still failing after "
                                            f"{self.cfg.repair_rounds} repair rounds: {r.get('gate')}",
                                            stage)
                    self._repair_gate(stage, r)
                    continue

                if action == "ERROR":
                    if not self._handle_error(r):
                        return self._finish("failed", str(r.get("detail"))[:1500], stage)
                    continue

                if action in ("WAIT", "LOCKED"):
                    delay = 20 if action == "WAIT" else 15
                    waited += delay
                    if waited > max_wait_s:
                        return self._finish("failed", f"timed out waiting on {stage}: "
                                            f"{r.get('reason') or r.get('detail')}", stage)
                    time.sleep(delay)
                    continue

                if action == "BLOCKED":
                    return self._finish("failed", f"stage {stage} blocked — missing inputs "
                                        f"{r.get('missing_inputs')}: {r.get('reason')}", stage)

                return self._finish("failed", f"unexpected runner response: {json.dumps(r)[:800]}", stage)
            return self._finish("failed", f"exceeded {max_iterations} driver iterations")
        except BudgetExceeded as e:
            return self._finish("budget_exceeded", str(e))
        except LLMError as e:
            return self._finish("failed", f"LLM endpoint error: {e}")

    def _second_opinion(self) -> str | None:
        """Run the cross-model check. Returns a stop reason (block mode) or None."""
        mode = self.cfg.second_opinion_mode
        if mode == "off":
            return None
        from hermes_adapter import second_opinion as so
        bc_path = PLUGIN_ROOT / "projects" / (self.slug or "") / "business-context.json"
        company = (json.loads(bc_path.read_text(encoding="utf-8")).get("company")
                   if bc_path.exists() else None)
        rounds = max(1, self.cfg.second_opinion_rounds) if mode == "block" else 1
        res: dict[str, Any] = {}
        for rnd in range(1, rounds + 1):
            res = so.judge(self.cfg, self.client, self.ws,
                           keyword=self.brief.get("primary_keyword", ""), company=company)
            self.report.second_opinion = res.get("verdict")
            self.report.second_opinion_failed = res.get("failed") or []
            self.event("second_opinion", mode=mode, round=rnd, verdict=res.get("verdict"),
                       failed=res.get("failed"), error=res.get("error"))
            if res.get("verdict") == "PASS" or mode == "advisory":
                return None
            if res.get("verdict") == "FAIL" and rnd < rounds:
                self._generic_repair("second-opinion", so.repair_brief(res), "")
        why = res.get("error") or ", ".join(res.get("failed") or [])
        return (f"second opinion ({res.get('model')}) still {res.get('verdict')} after "
                f"{rounds} round(s): {why}. See second-opinion.json.")

    def _finish(self, status: str, detail: str, stage: str | None = None) -> RunReport:
        rep = self.report
        rep.status, rep.detail, rep.stage = status, detail, stage
        rep.finished_at = _now()
        rep.llm_usd = str(self.tracker.usage.usd)
        rep.llm_calls = self.tracker.usage.calls
        rep.unpriced_models = sorted(self.tracker.usage.unpriced_models)
        pub = self._read_json("publish-result.json") or self._read_json("publish-log.json") or {}
        rep.post_id = pub.get("post_id") or pub.get("id")
        rep.preview_url = pub.get("post_url") or pub.get("preview_url")
        self.event("finish", status=status, detail=detail, stage=stage)
        try:
            (self.ws / "hermes-run-report.json").write_text(
                json.dumps(rep.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        return rep

    def _read_json(self, name: str) -> dict[str, Any] | None:
        p = self.ws / name
        if not p.exists():
            return None
        try:
            from scripts._core import file_bus
            d = file_bus.tolerant_json_load(p)
            return d if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001
            return None

    # ── LLM stages ──────────────────────────────────────────────
    def _expected_paths(self, r: dict[str, Any]) -> list[str]:
        out = []
        for o in r.get("expected_outputs") or []:
            if any(ch in o for ch in "*?{") or o.endswith("/"):
                continue  # directory / pattern outputs are checked by verify_stage
            out.append(str(self.ws / o) if not o.startswith(("/", "memory/", "projects/")) else o)
        return out

    def _dispatch(self, r: dict[str, Any]) -> bool:
        stage = r["stage"]
        self.llm_attempts[stage] += 1
        if self.llm_attempts[stage] > self.cfg.llm_stage_retries + 1:
            raise _Fatal(f"LLM stage {stage} failed {self.llm_attempts[stage] - 1} times")
        prompt = r.get("dispatch_prompt") or ""
        note = self.retry_notes.pop(stage, "")
        if note:
            prompt += ("\n\n## PREVIOUS ATTEMPT FAILED VERIFICATION\n"
                       f"{note}\nFix exactly this. Write the required outputs to disk.")
        expected = self._expected_paths(r)
        self.event("dispatch", stage=stage, subagent=r.get("subagent_type") or "(inline)",
                   attempt=self.llm_attempts[stage])
        t0 = time.time()
        if stage == "section-drafter":
            res = self.run_section_writers()
        elif r.get("subagent_type"):
            res = run_subagent(cfg=self.cfg, client=self.client, subagent_type=r["subagent_type"],
                               stage=stage, prompt=prompt, task_id=self.task_id,
                               project_slug=self.slug, expected_outputs=expected)
        else:
            res = run_inline(cfg=self.cfg, client=self.client, stage=stage,
                             description=r.get("description") or "", prompt=prompt,
                             task_id=self.task_id, project_slug=self.slug,
                             expected_outputs=expected)
        self.event("dispatch_done", stage=stage, turns=res.turns, stopped=res.stopped_reason,
                   missing=res.missing_outputs, secs=round(time.time() - t0, 1),
                   llm_usd=str(self.tracker.usage.usd))
        if res.missing_outputs:
            self.retry_notes[stage] = ("These required outputs were not written: "
                                       + ", ".join(res.missing_outputs))
            return False  # runner will hand the same stage back; we re-dispatch with the note
        return True

    # ── section-drafter fan-out ─────────────────────────────────
    def _section_jobs(self, only_indices: set[int] | None = None) -> list[dict[str, Any]]:
        outline = self._read_json("outline.json") or {}
        angle = self._read_json("angle.json") or {}
        sections = [s for s in outline.get("sections") or [] if isinstance(s, dict)]
        bc_path = PLUGIN_ROOT / "projects" / (self.slug or "") / "business-context.json"
        bc = json.loads(bc_path.read_text(encoding="utf-8")) if bc_path.exists() else {}
        slot_map = _image_slots_by_section(outline, self._load_image_prompts())
        voice = (bc.get("voice_default") or {}).get("pair")
        jobs = []
        for s in sections:
            idx = s.get("index")
            if str(s.get("h2", "")).strip().lower() == "references" or s.get("is_references_block"):
                continue
            if only_indices is not None and idx not in only_indices:
                continue
            others = [f"- [{o.get('index')}] {o.get('h2')}: {o.get('section_intent', '')}"
                      for o in sections if o is not s]
            slug = re.sub(r"[^a-z0-9]+", "_", str(s.get("h2", "")).lower()).strip("_")[:40] or "section"
            jobs.append({
                "index": idx,
                "out": f"memory/workspace/{self.task_id}/sections/{int(idx):02d}_{slug}.md",
                "payload": {
                    "section_spec": {k: v for k, v in s.items() if k != "anchor_id"},
                    "title": angle.get("title"), "hook": angle.get("hook") or angle.get("thesis"),
                    "format_id": angle.get("format_id"), "modifiers": angle.get("modifiers") or [],
                    "primary_keyword": self.brief.get("primary_keyword"),
                    "secondary_keywords": (self.brief.get("keywords") or [])[1:],
                    "context_summary": "\n".join(others)[:6000],
                    "image_slot_info": slot_map.get(idx),
                    "local_mode": self.brief.get("local_mode", False),
                    "location_anchor": self.brief.get("location_anchor"),
                    "company_facts": bc.get("company"),
                    "voice_pair": voice,
                },
            })
        return jobs

    def _load_image_prompts(self) -> list[dict[str, Any]]:
        p = self.ws / "image-prompts.json"
        if not p.exists():
            return []
        try:
            from scripts._core.image_prompts import load_image_prompts
            return load_image_prompts(p)
        except Exception:  # noqa: BLE001
            return []

    def run_section_writers(self, only_indices: set[int] | None = None,
                            note: str = "") -> AgentResult:
        jobs = self._section_jobs(only_indices)
        writer = load_agent("writer")
        fmt = (jobs[0]["payload"].get("format_id") if jobs else None) or "listicle"
        refs = ["references/style/markdown-authoring-conventions.md",
                "references/style/visual-design-components.md",
                "references/style/banned-words.md", "references/style/ai-tells-43.md",
                "references/style/em-dash-prohibition.md",
                "references/seo/citation-capsules-princeton.md", f"templates/{fmt}.md"]
        ws_rel = f"memory/workspace/{self.task_id}"

        def one(job: dict[str, Any]) -> AgentResult:
            prompt = (
                "Write ONE section of the article, per your agent instructions.\n\n"
                "## Inputs (JSON)\n```json\n"
                + json.dumps(job["payload"], ensure_ascii=False, indent=2) + "\n```\n\n"
                "## Research (read via Read; use ONLY what is relevant to your H2)\n"
                f"- {ws_rel}/research.json (main research; long — read in chunks with offset/limit)\n"
                f"- {ws_rel}/research-brief.json (if it exists: per-H2 filtered quotes & stats)\n"
                f"- {ws_rel}/angle.json\n\n"
                "## References to load FIRST via Read\n" + "\n".join(f"- {r}" for r in refs) + "\n\n"
                "Company self-facts (tenure, capacity, MOQ, certifications, team) may ONLY come from "
                "company_facts above. Never invent experience, clients or numbers.\n\n"
                f"## Output\nWrite exactly this file (Write tool): {job['out']}\n"
                "Plain markdown starting with the `## H2` heading (no {#anchor}, no frontmatter). "
                "Write the full draft FIRST, then refine by re-writing the same file if needed."
                + (f"\n\n## Fix required from previous attempt\n{note}" if note else "")
            )
            return run_agent(cfg=self.cfg, client=self.client, role="writer",
                             stage="section-drafter", system_prompt=writer.body,
                             user_prompt=prompt, tools=writer.tools, max_turns=writer.max_turns,
                             task_id=self.task_id, project_slug=self.slug,
                             expected_outputs=[job["out"]])

        par = max(1, self.cfg.writer_parallelism)
        self.event("writers_start", sections=[j["index"] for j in jobs], parallel=par)
        results: list[AgentResult] = []
        with ThreadPoolExecutor(max_workers=par) as pool:
            futures = [pool.submit(one, j) for j in jobs]
            for f in futures:
                try:
                    results.append(f.result())
                except (BudgetExceeded, LLMError):
                    raise
                except Exception as e:  # noqa: BLE001
                    results.append(AgentResult(f"writer crashed: {e}", 0, [], ["?"], "error"))
        missing = [m for r in results for m in r.missing_outputs]
        return AgentResult(final_text=f"{len(jobs)} writers finished",
                           turns=sum(r.turns for r in results),
                           files_written=[w for r in results for w in r.files_written],
                           missing_outputs=missing, stopped_reason="done")

    # ── gate repair ─────────────────────────────────────────────
    def _repair_gate(self, stage: str, r: dict[str, Any]) -> None:
        gate = str(r.get("gate") or "")
        self.event("repair", stage=stage, round=self.gate_repairs[stage], gate=gate[:500])
        if stage == "section-completeness-check":
            res = self._read_json("section-completeness.json") or {}
            missing = res.get("missing_indices") or []
            if missing:
                self.run_section_writers({int(i) for i in missing},
                                         note="This section was missing from the draft.")
                return
        if stage in _CTA_GATES:
            res = run_subagent(cfg=self.cfg, client=self.client, subagent_type="cta-writer",
                               stage=f"repair:{stage}", task_id=self.task_id,
                               project_slug=self.slug,
                               expected_outputs=[str(self.ws / "cta-draft.json")],
                               prompt=(f"The CTA gate '{stage}' rejected the current CTA draft:\n{gate}\n\n"
                                       f"Read memory/workspace/{self.task_id}/cta-brief.json, "
                                       f"memory/workspace/{self.task_id}/cta-draft.json and the gate "
                                       f"result in memory/workspace/{self.task_id}/. Rewrite "
                                       "cta-draft.json so it passes, keeping its schema and "
                                       "_generated_by:'cta-writer-subagent'."))
            self.event("repair_done", stage=stage, turns=res.turns)
            return
        self._generic_repair(stage, gate, r.get("stdout_tail") or "")

    def _generic_repair(self, stage: str, gate: str, tail: str) -> None:
        ws_rel = f"memory/workspace/{self.task_id}"
        prompt = f"""A quality gate failed. Repair the article SURGICALLY so the gate passes
(repair-orchestrator Level 1: smallest edit that fixes each defect).

- Failed stage: {stage}
- Gate report: {gate}
- Runner output tail: {tail[-1500:]}
- Workspace: {ws_rel}/ (the gate's JSON result file is there — read it for the full defect list)

Rules:
1. Edit {ws_rel}/draft.md (and only other files the defect is actually in). Read
   subskills/cross-cutting/repair-orchestrator/SKILL.md and the lint script under scripts/ that
   produced the report if you need to understand a defect class.
2. Never rename H2 headings, never remove [claim:…] markers, [IMAGE-SLOT-…] tokens, the CTA
   module blocks ('### Your next step'-class H3 + paragraph), or the References section.
3. Never write or edit provenance-gated subagent artifacts (fact-check.json, humanizer-report.json,
   review.json, geo-audit.json, visual-design-report.json, cta-draft.json, image-qa-report.json,
   internal-link-report.json).
4. Zero em-dashes (U+2014) in anything you write. Keep numbers consistent across the article.
5. When done, you may re-run the failing check yourself with Bash to confirm, then stop. The
   pipeline will re-run the gate."""
        res = run_agent(cfg=self.cfg, client=self.client, role="repair", stage=f"repair:{stage}",
                        system_prompt="You repair SEO article drafts so deterministic quality gates pass.",
                        user_prompt=prompt, tools=REPAIR_TOOLS, max_turns=60,
                        task_id=self.task_id, project_slug=self.slug)
        self.event("repair_done", stage=stage, turns=res.turns)

    # ── errors ──────────────────────────────────────────────────
    def _handle_error(self, r: dict[str, Any]) -> bool:
        stage = r.get("stage") or ""
        detail = str(r.get("detail") or "")
        from scripts.pipeline import orchestrator as orch
        stage_def = next((s for s in orch.STAGES if s.name == stage), None)
        if stage_def is not None and stage_def.executor == "LLM":
            if self.llm_attempts[stage] > self.cfg.llm_stage_retries:
                return False
            if stage in _CONTENT_GATED_LLM and (self.ws / _CONTENT_GATED_LLM[stage]).exists():
                self.gate_repairs[stage] += 1
                if self.gate_repairs[stage] > self.cfg.repair_rounds:
                    return False
                self._generic_repair(stage, f"{detail}\n(see {_CONTENT_GATED_LLM[stage]} for the "
                                     "verdict/score and the specific problems to fix)", "")
            self.retry_notes[stage] = detail[:2000]
            self.event("llm_retry", stage=stage, detail=detail[:500])
            return True  # runner hands the stage back as DISPATCH_LLM
        # BASH stage crashed: retry once (transient network/API), then give up with detail.
        self.bash_errors[stage] += 1
        if self.bash_errors[stage] > 1:
            return False
        self.event("bash_retry", stage=stage, detail=detail[:500],
                   stderr=str(r.get("stderr_tail") or "")[-500:])
        time.sleep(10)
        return True


class _Fatal(RuntimeError):
    pass


def _image_slots_by_section(outline: dict[str, Any], prompts: list[dict[str, Any]]) -> dict[Any, Any]:
    """section index → {slot_id, position, description, is_featured} for writers.

    Prefers outline.image_slots[].after_section_index (what image_placeholder_check
    validates against); falls back to pairing sections with image_slot=true, in order,
    with the non-cover prompt slots.
    """
    by_id = {p.get("slot_id"): p for p in prompts if isinstance(p, dict)}
    out: dict[Any, Any] = {}
    for slot in outline.get("image_slots") or []:
        if not isinstance(slot, dict) or not slot.get("slot_id"):
            continue
        idx = slot.get("after_section_index", slot.get("section_index"))
        if idx is None:
            m = re.search(r"(\d+)", str(slot.get("position", "")))
            idx = int(m.group(1)) if m else None
        if idx is None:
            continue
        p = by_id.get(slot["slot_id"], {})
        out[idx] = {"slot_id": slot["slot_id"], "position": slot.get("position", "after_first_paragraph"),
                    "description": slot.get("description") or p.get("alt_text_seed") or p.get("purpose", ""),
                    "is_featured": bool(p.get("is_featured"))}
    if out:
        return out
    try:
        from scripts._core.image_prompts import is_cover_slot
    except Exception:  # noqa: BLE001
        def is_cover_slot(s: str | None) -> bool:  # type: ignore[misc]
            return s == "cover"
    inline = [p for p in prompts if isinstance(p, dict) and p.get("slot_id")
              and not p.get("is_featured") and not is_cover_slot(p.get("slot_id"))]
    flagged = [s.get("index") for s in outline.get("sections") or [] if s.get("image_slot")]
    for idx, p in zip(flagged, inline):
        out[idx] = {"slot_id": p["slot_id"], "position": "after_first_paragraph",
                    "description": p.get("alt_text_seed") or p.get("purpose", ""), "is_featured": False}
    return out


def drive_task(cfg: LLMConfig, task_id: str,
               on_event: Callable[[dict[str, Any]], None] | None = None) -> RunReport:
    d = Driver(cfg, task_id, on_event=on_event)
    try:
        return d.run()
    except _Fatal as e:
        return d._finish("failed", str(e))
    except Exception as e:  # noqa: BLE001 — always leave a report behind
        return d._finish("failed", f"driver crashed: {type(e).__name__}: {e}\n"
                         f"{traceback.format_exc()[-1500:]}")
