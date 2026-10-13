"""Task 20261012_a7fc4ce4 (2026-10-12): the fact-checker hit the $40 daily cost cap and an
HTTP 429, wrote FIX_REQUIRED with issue type "verification_infrastructure_blocked", and the
driver spent three repair rounds editing a draft that had nothing wrong with it."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = PLUGIN_ROOT / "memory" / "workspace"
TPL = PLUGIN_ROOT / "hermes_adapter" / "templates" / "clawclipfactory"

BLOCKED = {"verdict": "FIX_REQUIRED", "issues": [{
    "severity": "high", "type": "verification_infrastructure_blocked",
    "description": "the next Bash command was refused by the daily cost guard ($42.9749 used "
                   "against the $40.00 cap). The fresh WebFetch returned local_rate_limited HTTP 429."}]}
CONTENT = {"verdict": "FIX_REQUIRED", "issues": [{
    "severity": "high", "type": "unsupported_claim",
    "description": "The 40% market share figure has no source."}]}


@pytest.fixture()
def task():
    from hermes_adapter.bootstrap_project import install
    from hermes_adapter.task import create_task
    slug, _ = install(TPL, allow_todo=True, force=True)
    tid = create_task(project_slug=slug, keyword="claw clip sizes", image_count=2)
    yield tid, WS_ROOT / tid
    shutil.rmtree(WS_ROOT / tid, ignore_errors=True)


def _run(task, monkeypatch, verdict):
    from hermes_adapter import driver as drv
    from tests.test_hermes_adapter import _cfg
    tid, ws = task
    (ws / "draft.md").write_text("# x\n\nbody\n")
    (ws / "fact-check.json").write_text(json.dumps(verdict))
    repairs: list[str] = []
    monkeypatch.setattr(drv.Driver, "_generic_repair", lambda self, st, g, t: repairs.append(st))
    monkeypatch.setattr(drv.Driver, "_dispatch", lambda self, r: True)
    err = {"action": "ERROR", "stage": "fact-check-and-citation",
           "detail": "LLM stage 'fact-check-and-citation' did not produce valid outputs: "
                     "['fact-check verdict is FIX_REQUIRED']"}
    seq = iter([err, err, err, err, err, {"action": "COMPLETE"}])
    rep = drv.Driver(_cfg(second_opinion_mode="off"), tid,
                     pipeline_drive=lambda *a, **k: next(seq)).run()
    return rep, repairs


def test_tool_blocked_verdict_stops_without_repair_rounds(task, monkeypatch):
    rep, repairs = _run(task, monkeypatch, BLOCKED)
    assert repairs == []
    assert rep.status == "failed" and "not a content problem" in rep.detail
    assert "daily cost guard" in rep.detail


def test_genuine_content_verdict_still_gets_repaired(task, monkeypatch):
    _, repairs = _run(task, monkeypatch, CONTENT)
    assert repairs and set(repairs) == {"fact-check-and-citation"}


def test_new_run_refuses_to_start_over_the_daily_cap(monkeypatch):
    from hermes_adapter import run_article as ra
    monkeypatch.setattr(ra, "_daily_budget_left", lambda: (42.97, 40.0))
    assert "daily cost cap reached" in ra._preflight("clawclipfactory", 2)
    monkeypatch.setattr(ra, "_daily_budget_left", lambda: (12.0, 40.0))
    assert ra._preflight("clawclipfactory", 2) is None
