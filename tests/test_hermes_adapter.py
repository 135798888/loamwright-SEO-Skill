"""Offline tests for hermes_adapter — no network, no real LLM.

Covers the seams that matter (Rule 10/14): least-tool isolation actually blocks,
the write sandbox actually blocks, the schema hook actually reports, the agent
loop nudges on missing outputs, the HTTP client retries transient errors and
parses usage, and the driver services each runner action correctly — including
the real run_pipeline accepting an adapter-created task.
"""
from __future__ import annotations

import json
import shutil
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from hermes_adapter import agent as agent_mod
from hermes_adapter.config import LLMConfig
from hermes_adapter.llm import ChatClient, ChatResult, CostTracker
from hermes_adapter.tools import PLUGIN_ROOT, ToolContext, call_tool

WS_ROOT = PLUGIN_ROOT / "memory" / "workspace"


def _cfg(**kw: Any) -> LLMConfig:
    base = dict(base_url="http://127.0.0.1:9/v1", api_key="k", models={"default": "m-default",
                "writer": "m-writer"}, prices={"m-default": (Decimal(1), Decimal(2))})
    base.update(kw)
    return LLMConfig(**base)


class ScriptedClient:
    """Stands in for ChatClient: returns a scripted sequence of assistant messages."""

    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, model: str, messages: list, tools: list | None, stage: str, **_: Any) -> ChatResult:
        self.calls.append({"model": model, "tools": [t["function"]["name"] for t in tools or []],
                           "messages": json.loads(json.dumps(messages))})
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "done"}
        return ChatResult(message=msg, finish_reason="stop", prompt_tokens=10, completion_tokens=5)


def _tc(name: str, args: dict[str, Any], i: int = 1) -> dict[str, Any]:
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def ws():
    tid = "testhermes_" + "a1b2c3d4"
    d = WS_ROOT / tid
    d.mkdir(parents=True, exist_ok=True)
    yield tid, d
    shutil.rmtree(d, ignore_errors=True)


# ── tools: isolation + sandbox + hooks ──────────────────────────

def test_tool_not_in_whitelist_is_refused(ws):
    tid, d = ws
    ctx = ToolContext(tid, None)
    out = call_tool(ctx, ["Read", "Write"], "Bash", json.dumps({"command": "echo pwned > /tmp/x"}))
    assert "not available to you" in out


def test_writer_agent_definition_has_no_web_or_bash():
    adef = agent_mod.load_agent("writer")
    assert set(adef.tools) == {"Read", "Write"}
    researcher = agent_mod.load_agent("researcher")
    assert "Bash" in researcher.tools and "WebFetch" in researcher.tools


def test_write_outside_memory_and_projects_is_denied(ws):
    tid, d = ws
    ctx = ToolContext(tid, None)
    out = call_tool(ctx, ["Write"], "Write", json.dumps({"file_path": "scripts/evil.py", "content": "x"}))
    assert out.startswith("Error: Write denied")
    assert not (PLUGIN_ROOT / "scripts" / "evil.py").exists()


def test_read_credentials_is_denied(ws, tmp_path):
    tid, d = ws
    ctx = ToolContext(tid, None)
    cred = Path.home() / ".xuanran-seo" / "credentials" / "tavily.key"
    out = call_tool(ctx, ["Read"], "Read", json.dumps({"file_path": str(cred)}))
    assert "Access denied" in out


def test_schema_hook_reports_invalid_state_write(ws):
    tid, d = ws
    ctx = ToolContext(tid, None)
    out = call_tool(ctx, ["Write"], "Write", json.dumps({
        "file_path": f"memory/workspace/{tid}/state.json", "content": json.dumps({"task_id": "BAD-ID"})}))
    assert "SCHEMA VALIDATION FAILED" in out


def test_edit_requires_unique_match(ws):
    tid, d = ws
    (d / "draft.md").write_text("a a", encoding="utf-8")
    ctx = ToolContext(tid, None)
    out = call_tool(ctx, ["Edit"], "Edit", json.dumps({
        "file_path": f"memory/workspace/{tid}/draft.md", "old_string": "a", "new_string": "b"}))
    assert "occurs 2 times" in out
    out = call_tool(ctx, ["Edit"], "Edit", json.dumps({
        "file_path": f"memory/workspace/{tid}/draft.md", "old_string": "a", "new_string": "b",
        "replace_all": True}))
    assert (d / "draft.md").read_text() == "b b"


# ── agent loop ──────────────────────────────────────────────────

def test_agent_loop_writes_then_stops_and_offers_only_declared_tools(ws):
    tid, d = ws
    out = f"memory/workspace/{tid}/sections/00_intro.md"
    client = ScriptedClient([
        {"role": "assistant", "content": None, "tool_calls": [
            _tc("Write", {"file_path": out, "content": "## Intro\n\nBody."})]},
        {"role": "assistant", "content": "Section written."},
    ])
    writer = agent_mod.load_agent("writer")
    res = agent_mod.run_agent(cfg=_cfg(), client=client, role="writer", stage="section-drafter",
                              system_prompt=writer.body, user_prompt="write", tools=writer.tools,
                              max_turns=10, task_id=tid, project_slug=None, expected_outputs=[out])
    assert res.missing_outputs == [] and res.stopped_reason == "done"
    assert set(client.calls[0]["tools"]) == {"Read", "Write"}
    assert client.calls[0]["model"] == "m-writer"
    assert (PLUGIN_ROOT / out).read_text().startswith("## Intro")


def test_agent_is_nudged_when_it_stops_without_outputs(ws):
    tid, d = ws
    out = f"memory/workspace/{tid}/angle.json"
    client = ScriptedClient([
        {"role": "assistant", "content": "I think the angle is X."},           # prose only
        {"role": "assistant", "content": None, "tool_calls": [
            _tc("Write", {"file_path": out, "content": "{}"})]},
        {"role": "assistant", "content": "done"},
    ])
    res = agent_mod.run_inline(cfg=_cfg(), client=client, stage="format-selector",
                               description="", prompt="pick", task_id=tid, project_slug=None,
                               expected_outputs=[out])
    assert res.missing_outputs == []
    nudge = client.calls[1]["messages"][-1]["content"]
    assert "do not exist yet" in nudge


def test_context_compaction_elides_old_tool_output():
    msgs = [{"role": "system", "content": "s"}] + [
        {"role": "tool", "tool_call_id": str(i), "content": "x" * 1000} for i in range(30)]
    agent_mod._compact(msgs, budget=12_000)
    assert msgs[1]["content"].startswith("[elided")
    assert msgs[-1]["content"] == "x" * 1000


# ── HTTP client ─────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self):
        _Handler.hits += 1
        self.rfile.read(int(self.headers.get("content-length", 0)))
        if _Handler.hits == 1:
            self.send_response(429); self.end_headers(); self.wfile.write(b"slow down"); return
        body = {"choices": [{"message": {"role": "assistant", "content": "pong"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}}
        data = json.dumps(body).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def log_message(self, *a):  # silence
        pass


def test_chat_client_retries_429_and_prices_usage(monkeypatch):
    monkeypatch.setattr("hermes_adapter.llm.time.sleep", lambda s: None)
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = _cfg(base_url=f"http://127.0.0.1:{srv.server_port}/v1")
        tracker = CostTracker(cfg, None, None)
        monkeypatch.setattr(tracker, "record", CostTracker.record.__get__(tracker))
        res = ChatClient(cfg, tracker).chat(model="m-default", messages=[], tools=None, stage="t")
        assert res.message["content"] == "pong" and _Handler.hits == 2
        assert tracker.usage.usd == Decimal("2.0000")   # 1M in × $1 + 0.5M out × $2
    finally:
        srv.shutdown()


# ── driver ──────────────────────────────────────────────────────

def _make_state(d: Path, tid: str) -> None:
    (d / "state.json").write_text(json.dumps({
        "task_id": tid, "project_slug": None, "phase": "research", "current_stage": "research",
        "created_at": "2026-10-07T00:00:00Z", "brief": {"primary_keyword": "kw", "keywords": ["kw"]}}))


def test_driver_dispatches_then_reports_complete(ws, monkeypatch):
    from hermes_adapter import driver as drv
    tid, d = ws
    _make_state(d, tid)
    calls: list[Any] = []
    seq = iter([
        {"action": "DISPATCH_LLM", "stage": "format-selector", "subagent_type": "",
         "dispatch_prompt": "p", "expected_outputs": ["angle.json"]},
        {"action": "COMPLETE"},
    ])

    def fake_drive(task_id, completed_llm=None):
        calls.append(completed_llm)
        return next(seq)

    def fake_inline(**kw):
        (d / "angle.json").write_text("{}")
        return agent_mod.AgentResult("ok", 1, [], [], "done")

    monkeypatch.setattr(drv, "run_inline", fake_inline)
    rep = drv.Driver(_cfg(), tid, pipeline_drive=fake_drive).run()
    assert rep.status == "complete"
    assert calls == [None, "format-selector"]     # verified via --completed-llm, never self-marked
    assert (d / "hermes-run-report.json").exists()


def test_driver_redispatches_when_outputs_missing_then_gives_up(ws, monkeypatch):
    from hermes_adapter import driver as drv
    tid, d = ws
    _make_state(d, tid)
    resp = {"action": "DISPATCH_LLM", "stage": "meta-builder", "subagent_type": "",
            "dispatch_prompt": "p", "expected_outputs": ["meta.json"]}
    prompts: list[str] = []

    def fake_inline(**kw):
        prompts.append(kw["prompt"])
        return agent_mod.AgentResult("", 1, [], [str(d / "meta.json")], "done")

    monkeypatch.setattr(drv, "run_inline", fake_inline)
    d_ = drv.Driver(_cfg(llm_stage_retries=1), tid, pipeline_drive=lambda *a, **k: dict(resp))
    try:
        d_.run(max_iterations=10)
        raise AssertionError("expected _Fatal")
    except drv._Fatal:
        pass
    assert len(prompts) == 2
    assert "PREVIOUS ATTEMPT FAILED" in prompts[1] and "meta.json" in prompts[1]


def test_driver_gate_failed_runs_repair_then_stops_after_limit(ws, monkeypatch):
    from hermes_adapter import driver as drv
    tid, d = ws
    _make_state(d, tid)
    repairs: list[str] = []
    monkeypatch.setattr(drv.Driver, "_generic_repair", lambda self, s, g, t: repairs.append(s))
    rep = drv.Driver(_cfg(repair_rounds=2), tid, pipeline_drive=lambda *a, **k: {
        "action": "GATE_FAILED", "stage": "render-lint", "gate": "L12 em-dash"}).run()
    assert repairs == ["render-lint", "render-lint"]
    assert rep.status == "failed" and "render-lint" in rep.detail


def test_driver_budget_exceeded_stops_cleanly(ws, monkeypatch):
    from hermes_adapter import driver as drv
    from hermes_adapter.llm import BudgetExceeded
    tid, d = ws
    _make_state(d, tid)

    def boom(**kw):
        raise BudgetExceeded("limit")
    monkeypatch.setattr(drv, "run_inline", boom)
    rep = drv.Driver(_cfg(), tid, pipeline_drive=lambda *a, **k: {
        "action": "DISPATCH_LLM", "stage": "meta-builder", "subagent_type": "",
        "expected_outputs": []}).run()
    assert rep.status == "budget_exceeded"


def test_image_slot_mapping_prefers_outline_slots():
    outline = {"image_slots": [{"slot_id": "mold-shop", "after_section_index": 2}],
               "sections": [{"index": 1, "image_slot": True}]}
    prompts = [{"slot_id": "cover", "is_featured": True}, {"slot_id": "mold-shop", "alt_text_seed": "x"}]
    from hermes_adapter.driver import _image_slots_by_section
    m = _image_slots_by_section(outline, prompts)
    assert list(m) == [2] and m[2]["slot_id"] == "mold-shop"
    m2 = _image_slots_by_section({"sections": [{"index": 1, "image_slot": True}]}, prompts)
    assert m2[1]["slot_id"] == "mold-shop"


def test_real_runner_accepts_adapter_created_task(monkeypatch):
    """Seam test: adapter task creation → the ORIGINAL run_pipeline hands back research."""
    from hermes_adapter.bootstrap_project import install
    from hermes_adapter.task import create_task
    from scripts.pipeline import run_pipeline
    tpl = PLUGIN_ROOT / "hermes_adapter" / "templates" / "clawclipfactory"
    slug, _ = install(tpl, allow_todo=True, force=True)
    monkeypatch.setenv("XS_ACTIVE_PROJECT", slug)
    tid = create_task(project_slug=slug, keyword="custom claw clips wholesale", image_count=3)
    try:
        r = run_pipeline.drive(tid)
        assert r["action"] == "DISPATCH_LLM" and r["stage"] == "research"
        assert r["subagent_type"].endswith("researcher")
        assert "custom claw clips wholesale" in r["dispatch_prompt"]
    finally:
        shutil.rmtree(WS_ROOT / tid, ignore_errors=True)


def test_queue_pops_first_real_keyword(tmp_path):
    from hermes_adapter.run_article import _pop_queue
    q = tmp_path / "kw.txt"
    q.write_text("# comment\n\ncustom claw clips wholesale\nacetate claw clips\n", encoding="utf-8")
    assert _pop_queue(q) == "custom claw clips wholesale"
    assert q.read_text(encoding="utf-8") == "# comment\n\nacetate claw clips\n"
    assert _pop_queue(q) == "acetate claw clips"
    assert _pop_queue(q) is None


# ── full loop over real HTTP + the ORIGINAL runner/orchestrator ─────────

class _FakeResearcher(BaseHTTPRequestHandler):
    """Plays the model: first turn → Write a valid research.json; next turn → stop."""

    def do_POST(self):
        import re as _re
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        msgs = body["messages"]
        tid = _re.search(r"Task id: (\S+)", msgs[0]["content"]).group(1)
        if any(m["role"] == "tool" for m in msgs):
            msg = {"role": "assistant", "content": "research.json written"}
        else:
            research = {
                "primary_keyword": "custom claw clips wholesale", "intent": "commercial",
                "competitor_titles": [{"title": f"Competitor {i}", "url": f"https://example{i}.com/a"}
                                      for i in range(3)],
                "serp_features": ["paa_box", "related_searches", "image_pack"],
                "paa": ["What is the MOQ for custom claw clips?"],
            }
            msg = {"role": "assistant", "content": None, "tool_calls": [
                _tc("Write", {"file_path": f"memory/workspace/{tid}/research.json",
                              "content": json.dumps(research)})]}
        out = json.dumps({"choices": [{"message": msg, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 100, "completion_tokens": 50}}).encode()
        self.send_response(200); self.send_header("content-length", str(len(out)))
        self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_full_loop_research_stage_verified_by_real_orchestrator(monkeypatch):
    from hermes_adapter import driver as drv
    from hermes_adapter.bootstrap_project import install
    from hermes_adapter.task import create_task
    tpl = PLUGIN_ROOT / "hermes_adapter" / "templates" / "clawclipfactory"
    slug, _ = install(tpl, allow_todo=True, force=True)
    monkeypatch.setenv("XS_ACTIVE_PROJECT", slug)
    srv = HTTPServer(("127.0.0.1", 0), _FakeResearcher)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    tid = create_task(project_slug=slug, keyword="custom claw clips wholesale", image_count=3)
    try:
        cfg = _cfg(base_url=f"http://127.0.0.1:{srv.server_port}/v1")
        d = drv.Driver(cfg, tid)
        d.run(max_iterations=2)       # 1: research dispatched; 2: runner verifies it + advances
        runner_events = [e for e in d.report.events if e["kind"] == "runner"]
        assert runner_events[0]["stage"] == "research"
        assert runner_events[1]["action"] == "DISPATCH_LLM"
        assert runner_events[1]["stage"] != "research"          # orchestrator accepted it
        state = json.loads((WS_ROOT / tid / "state.json").read_text())
        done = {h["stage"] for h in state.get("stage_history", []) if h.get("status") == "completed"}
        assert "research" in done
        research_done = next(e for e in d.report.events
                             if e["kind"] == "dispatch_done" and e["stage"] == "research")
        assert research_done["turns"] == 2 and research_done["missing"] == []
        print("next stage handed back by the real runner:", runner_events[1]["stage"])
    finally:
        srv.shutdown()
        shutil.rmtree(WS_ROOT / tid, ignore_errors=True)


# ── OpenAI reasoning models (gpt-5 / o-series) parameter quirks ──────────

class _StrictReasoningAPI(BaseHTTPRequestHandler):
    """Mimics OpenAI's 400s for reasoning models: no max_tokens, default temperature only."""
    bodies: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _StrictReasoningAPI.bodies.append(body)
        err = None
        if "max_tokens" in body:
            err = ("Unsupported parameter: 'max_tokens' is not supported with this model. "
                   "Use 'max_completion_tokens' instead.")
        elif "temperature" in body:
            err = ("Unsupported value: 'temperature' does not support 0.4 with this model. "
                   "Only the default (1) value is supported.")
        if err:
            out = json.dumps({"error": {"message": err, "type": "invalid_request_error"}}).encode()
            self.send_response(400)
        else:
            out = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"},
                                           "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
            self.send_response(200)
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def test_reasoning_model_param_quirks_self_correct(monkeypatch):
    monkeypatch.setattr(ChatClient, "_learned_token_param", {})
    monkeypatch.setattr(ChatClient, "_learned_drop", {})
    _StrictReasoningAPI.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), _StrictReasoningAPI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cfg = _cfg(base_url=f"http://127.0.0.1:{srv.server_port}/v1", temperature=0.4)
        client = ChatClient(cfg)
        # a relay alias the name heuristic can't recognise → must learn from the 400
        res = client.chat(model="my-relay-reasoner", messages=[], tools=None, stage="t")
        assert res.message["content"] == "ok"
        last = _StrictReasoningAPI.bodies[-1]
        assert "max_completion_tokens" in last and "max_tokens" not in last and "temperature" not in last
        n = len(_StrictReasoningAPI.bodies)
        client.chat(model="my-relay-reasoner", messages=[], tools=None, stage="t")
        assert len(_StrictReasoningAPI.bodies) == n + 1      # learned: no wasted 400s next time
    finally:
        srv.shutdown()


def test_token_param_picked_by_model_name():
    c = ChatClient(_cfg())
    assert c._token_param("gpt-5") == "max_completion_tokens"
    assert c._token_param("openai/gpt-5-mini") == "max_completion_tokens"
    assert c._token_param("o4-mini") == "max_completion_tokens"
    assert c._token_param("gpt-4.1") == "max_tokens"
    assert ChatClient(_cfg(token_param="max_tokens"))._token_param("gpt-5") == "max_tokens"


# ── second opinion (cross-model) ─────────────────────────────────────────

def _verdict_json(passes: list[bool]) -> str:
    return json.dumps({"criteria": [{"name": f"c{i}", "pass": p, "reason": "r",
                                     "quotes": ["q"], "fix": "f"} for i, p in enumerate(passes)],
                       "summary": "s"})


def test_parse_verdict_tolerant_and_strict():
    from hermes_adapter.second_opinion import parse_verdict
    assert parse_verdict("```json\n" + _verdict_json([True]) + "\n```")["criteria"][0]["pass"] is True
    assert parse_verdict("Sure! " + _verdict_json([False, True]))["criteria"][0]["pass"] is False
    assert parse_verdict('{"criteria":[{"name":"x","pass":"yes"}]}') is None   # not a real boolean
    assert parse_verdict("no json here") is None


def _so_driver(ws_dir, tid, mode, replies, monkeypatch, rounds=2):
    from hermes_adapter import driver as drv
    _make_state(ws_dir, tid)
    (ws_dir / "draft.md").write_text("## Intro\n\nOur MOQ is 50 pcs.\n", encoding="utf-8")
    cfg = _cfg(second_opinion_mode=mode, second_opinion_model="gemini-x",
               second_opinion_rounds=rounds)
    seq = iter([
        {"action": "DISPATCH_LLM", "stage": "independent-reviewer", "subagent_type": "reviewer",
         "dispatch_prompt": "p", "expected_outputs": []},
        {"action": "DISPATCH_LLM", "stage": "image-visual-qa", "subagent_type": "image-visual-qa",
         "dispatch_prompt": "p", "expected_outputs": []},
        {"action": "COMPLETE"},
    ])
    d = drv.Driver(cfg, tid, pipeline_drive=lambda *a, **k: next(seq))
    d.client = ScriptedClient([{"role": "assistant", "content": r} for r in replies])
    monkeypatch.setattr(drv, "run_subagent",
                        lambda **kw: agent_mod.AgentResult("ok", 1, [], [], "done"))
    repairs: list[str] = []
    monkeypatch.setattr(drv.Driver, "_generic_repair", lambda self, s, g, t: repairs.append(g))
    return d, repairs


def test_second_opinion_advisory_records_but_never_blocks(ws, monkeypatch):
    tid, d = ws
    drv_, repairs = _so_driver(d, tid, "advisory", [_verdict_json([False, True])], monkeypatch)
    rep = drv_.run()
    assert rep.status == "complete" and rep.second_opinion == "FAIL" and repairs == []
    saved = json.loads((d / "second-opinion.json").read_text())
    assert saved["verdict"] == "FAIL" and saved["model"] == "gemini-x" and saved["failed"] == ["c0"]
    # ran exactly once, and only after the reviewer — the judge saw the draft
    assert "Our MOQ is 50 pcs." in drv_.client.calls[0]["messages"][1]["content"]
    assert len(drv_.client.calls) == 1
    # …and it ran BEFORE the next stage (image-visual-qa → pre-publish → publish) was dispatched
    kinds = [(e["kind"], e.get("stage")) for e in drv_.report.events]
    assert kinds.index(("second_opinion", None)) < kinds.index(("dispatch", "image-visual-qa"))


def test_second_opinion_block_repairs_then_passes(ws, monkeypatch):
    tid, d = ws
    drv_, repairs = _so_driver(d, tid, "block",
                               [_verdict_json([False]), _verdict_json([True])], monkeypatch)
    rep = drv_.run()
    assert rep.status == "complete" and rep.second_opinion == "PASS"
    assert len(repairs) == 1 and "c0" in repairs[0]


def test_second_opinion_block_stops_before_publish_when_still_failing(ws, monkeypatch):
    tid, d = ws
    drv_, repairs = _so_driver(d, tid, "block",
                               [_verdict_json([False]), _verdict_json([False])], monkeypatch)
    rep = drv_.run()
    assert rep.status == "failed" and rep.stage == "second-opinion"
    assert len(repairs) == 1                     # repair between rounds, not after the last


def test_second_opinion_unreadable_verdict_is_not_a_pass_in_block_mode(ws, monkeypatch):
    tid, d = ws
    drv_, _ = _so_driver(d, tid, "block", ["I think it's fine."] * 4, monkeypatch, rounds=1)
    rep = drv_.run()
    assert rep.status == "failed" and rep.second_opinion == "ERROR"


def test_second_opinion_off_never_calls_judge(ws, monkeypatch):
    tid, d = ws
    drv_, _ = _so_driver(d, tid, "off", [], monkeypatch)
    rep = drv_.run()
    assert rep.status == "complete" and rep.second_opinion is None and drv_.client.calls == []


def test_second_opinion_config_validation(tmp_path, monkeypatch):
    from hermes_adapter.config import ConfigError, load_config
    p = tmp_path / "llm.yaml"
    p.write_text("base_url: http://x/v1\napi_key: k\nmodels: {default: m}\n"
                 "second_opinion: {mode: block}\n", encoding="utf-8")
    monkeypatch.setenv("LW_LLM_CONFIG", str(p))
    with pytest.raises(ConfigError):
        load_config()
    p.write_text("base_url: http://x/v1\napi_key: k\nmodels: {default: m}\n"
                 "second_opinion: {mode: advisory, model: gemini-3.1-pro-low}\n", encoding="utf-8")
    cfg = load_config()
    assert cfg.second_opinion_mode == "advisory" and cfg.second_opinion_model == "gemini-3.1-pro-low"
