"""Regression tests for the post-289 quality fixes (clawclipfactory, 2026-10-09).

Each test drives the real code path that produced the defect a reader saw:
four summary blocks before the content, disclaimers about the factory, a prompt
shown as a caption, AI-drawn claw clips, keyword stuffing, repeated advice,
a robotic per-section capsule and a duplicate of existing posts.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = PLUGIN_ROOT / "memory" / "workspace"
TPL = PLUGIN_ROOT / "hermes_adapter" / "templates" / "clawclipfactory"


@pytest.fixture()
def project():
    from hermes_adapter.bootstrap_project import install
    slug, _ = install(TPL, allow_todo=True, force=True)
    yield slug
    for extra in ("photo-library.json", "photos"):
        p = PLUGIN_ROOT / "projects" / slug / extra
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()


@pytest.fixture()
def task(project):
    from hermes_adapter.task import create_task
    tid = create_task(project_slug=project, keyword="custom claw clips wholesale", image_count=2)
    yield tid, WS_ROOT / tid
    shutil.rmtree(WS_ROOT / tid, ignore_errors=True)


# ── every agent gets the brief + verified facts ─────────────────────────

def test_every_agent_preamble_carries_brief_and_only_filled_facts(project):
    from hermes_adapter.agent import _env_preamble
    text = _env_preamble("t1", project, ["Read"])
    assert "Never write" in text and "Disclaimers about ourselves" in text
    assert "1,000 pieces per style" in text            # a website fact
    assert "about 7 days" in text
    assert "TODO" not in text                           # unfilled facts never reach a writer
    assert _env_preamble("t1", None, ["Read"]).count("editorial brief") == 0


# ── layout: one TL;DR, no Abstract / Key Takeaways / in-article ToC ──────

def _write_minimal_article(ws: Path) -> None:
    (ws / "angle.json").write_text(json.dumps({"format_id": "buyers-guide", "title": "Custom claw clips",
                                               "slug_draft": "custom-claw-clips"}))
    (ws / "outline.json").write_text(json.dumps({
        "tldr_seed": "Our standard MOQ is 1,000 pieces per style.",
        "abstract_seed": "This guide explains quotes.",
        "takeaways_seeds": ["Match scope first", "Approve samples"],
        "sections": [{"index": 0, "h2": "How we quote"}, {"index": 1, "h2": "Abstract"}],
        "faq": {"seed_questions": []}}))
    sec = ws / "sections"
    sec.mkdir(exist_ok=True)
    (sec / "00_how-we-quote.md").write_text("## How we quote\n\nWe price each style and color.\n")
    (sec / "01_abstract.md").write_text("## Abstract\n\nA writer-made abstract.\n")


def test_assembly_follows_project_layout(task):
    from scripts.build.assemble import assemble
    tid, ws = task
    _write_minimal_article(ws)
    draft = assemble(tid).read_text(encoding="utf-8")
    assert "## TL;DR" in draft
    assert "## Abstract" not in draft and "writer-made abstract" not in draft
    assert "## Key Takeaways" not in draft
    assert "Table of Contents" not in draft and "_(auto-generated)_" not in draft
    assert "## How we quote" in draft


def test_assembly_default_layout_unchanged_for_other_projects(task):
    from scripts.build.assemble import assemble
    tid, ws = task
    _write_minimal_article(ws)
    st = json.loads((ws / "state.json").read_text())
    st["project_slug"] = "no-such-project"
    (ws / "state.json").write_text(json.dumps(st))
    draft = assemble(tid).read_text(encoding="utf-8")
    for h in ("## TL;DR", "## Abstract", "## Key Takeaways", "## Table of Contents"):
        assert h in draft


# ── editorial gate ──────────────────────────────────────────────────────

POST_289 = """---
title: x
---
# Custom Claw Clips Wholesale

## When to delay your purchase

ClawClipFactory is the manufacturer publishing this guide. Manufacturer descriptions establish the
stated production scope, not independent performance evidence. For custom claw clips wholesale, an
acceptance specification should name the material. Custom claw clips wholesale buyers compare quotes.
Custom claw clips wholesale orders need samples. Custom claw clips wholesale pricing varies.
Custom claw clips wholesale terms differ. Custom claw clips wholesale scope matters. Custom claw clips wholesale again.

Approve appearance and function separately against written buyer agreed criteria before production.
Buyers should approve appearance and function separately against written agreed criteria before production.

## References

1. Something about not independent testing (2020).
"""


def test_editorial_check_catches_post_289_and_passes_clean_text(project):
    from hermes_adapter.editorial_check import check, project_config
    cfg = project_config(project)
    res = check(POST_289, "custom claw clips wholesale", cfg)
    rules = {v["rule"] for v in res["violations"]}
    assert {"banned_pattern", "banned_h2", "keyword_stuffing"} <= rules
    assert res["near_duplicate_pairs"] >= 1
    assert all("References" not in v["excerpt"] for v in res["violations"])
    clean = ("# Custom claw clips\n\n## What to send us for an accurate quote\n\n"
             "Our standard MOQ is 1,000 pieces per style. Send a drawing or sample and the colors you need.\n")
    assert check(clean, "custom claw clips wholesale", cfg)["passed"] is True


def test_driver_runs_editorial_gate_after_review_and_repairs(task, monkeypatch):
    from hermes_adapter import driver as drv
    from tests.test_hermes_adapter import _cfg
    tid, ws = task
    (ws / "draft.md").write_text(POST_289, encoding="utf-8")
    repairs: list[str] = []

    def fake_repair(self, stage, gate, tail):
        repairs.append(stage)
        (ws / "draft.md").write_text("# x\n\n## What to send us\n\nOur MOQ is 1,000 pieces per style.\n")
    monkeypatch.setattr(drv.Driver, "_generic_repair", fake_repair)
    seq = iter([{"action": "DISPATCH_LLM", "stage": "independent-reviewer", "subagent_type": "",
                 "expected_outputs": []}, {"action": "COMPLETE"}])
    monkeypatch.setattr(drv.Driver, "_dispatch", lambda self, r: True)
    d = drv.Driver(_cfg(second_opinion_mode="off"), tid, pipeline_drive=lambda *a, **k: next(seq))
    rep = d.run()
    assert repairs == ["editorial-check"]
    assert rep.status == "complete"
    assert json.loads((ws / "editorial-check.json").read_text())["passed"] is True


def test_project_skips_only_optional_stages(task, monkeypatch):
    from hermes_adapter import driver as drv
    from scripts.pipeline import orchestrator as orch
    from tests.test_hermes_adapter import _cfg
    tid, ws = task
    skipped: list[str] = []
    monkeypatch.setattr(orch, "skip_stage", lambda t, s, why: skipped.append(s) or {"ok": True})
    d = drv.Driver(_cfg(), tid, pipeline_drive=lambda *a, **k: {})
    assert d._project_skips({"stage": "citation-capsule-builder", "is_mandatory": False}) is True
    assert d._project_skips({"stage": "citation-capsule-builder", "is_mandatory": True}) is False
    assert d._project_skips({"stage": "section-drafter", "is_mandatory": False}) is False
    assert skipped == ["citation-capsule-builder"]


# ── captions ────────────────────────────────────────────────────────────

def test_prompt_text_never_becomes_a_caption():
    from scripts._core.caption_guard import reader_caption
    from scripts.wordpress.wp_publisher import _wrap_images_in_figures
    leaked = ("Realistic photograph, natural soft daylight, neutral off-white background, "
              "true-to-life plastic and acetate textures, sharp focus on the hair claw clips, no text, no logos")
    assert reader_caption(leaked) == ""
    assert reader_caption("CL-002 matte claw clips after spring assembly.") == \
        "CL-002 matte claw clips after spring assembly."
    media = SimpleNamespace(id=7, source_url="https://x/a.png")
    html = _wrap_images_in_figures('<p><img src="https://x/a.png" alt="a"/></p>',
                                   [{"slot_id": "s1", "alt": "a", "caption": leaked}], {"s1": media})
    assert "figcaption" not in html and "soft daylight" not in html


# ── own photo library ───────────────────────────────────────────────────

def _png(path: Path) -> None:
    from PIL import Image
    Image.new("RGB", (40, 30), (200, 180, 160)).save(path)


def test_own_library_fills_photo_slots_and_passes_the_real_join_gate(task, tmp_path):
    from hermes_adapter.photo_library import add
    from scripts.openai.image_fork import decide_pipeline
    from scripts.openai.own_library_pipeline import run_for_workspace
    from scripts.pipeline import orchestrator as orch
    tid, ws = task
    slug = json.loads((ws / "state.json").read_text())["project_slug"]
    for name, kind, desc in [("cl-002.png", "product", "CL-002 rectangular matte claw clip product shot"),
                             ("floor.png", "factory", "injection molding machines on the production floor")]:
        _png(tmp_path / name)
        add(slug, tmp_path / name, kind, desc, [])
    (ws / "image-prompts.json").write_text(json.dumps([
        {"slot_id": "cover", "kind": "photo", "alt_text_seed": "custom claw clips"},
        {"slot_id": "molding", "kind": "photo", "alt_text_seed": "injection molding machines at our factory"},
        {"slot_id": "chart1", "kind": "chart", "chart_spec": {}}]))
    (ws / "images.json").write_text(json.dumps([{"slot_id": "chart1", "path": str(tmp_path / "floor.png"),
                                                 "filename": "c.png", "alt": "c", "is_featured": False,
                                                 "source": "chart_render"}]))
    assert decide_pipeline(slug) == "own_library"
    merged = {e["slot_id"]: e for e in run_for_workspace(ws, slug)}
    assert merged["cover"]["filename"] == "cl-002.png" and merged["cover"]["is_featured"] is True
    assert merged["molding"]["filename"] == "floor.png"
    assert merged["chart1"]["source"] == "chart_render"          # charts survive the merge
    assert {merged["cover"]["source"], merged["molding"]["source"]} == {"own_library"}
    assert orch._content_gate_reason(ws, "image-pipeline-join") is None


def test_empty_library_is_refused_before_any_spend(project):
    from hermes_adapter.run_article import _preflight
    assert "no approved photos" in _preflight(project, None)
    assert _preflight(project, 0) is None


def test_ai_never_regenerates_a_library_photo(tmp_path, monkeypatch):
    from scripts.openai import image_regen_slots as rs
    (tmp_path / "images.json").write_text(json.dumps([{"slot_id": "cover", "source": "own_library"}]))
    req = tmp_path / "req.json"
    req.write_text(json.dumps({"task_id": "t", "round": 1, "requests": [
        {"slot_id": "cover", "kind": "photo", "prompt": "a claw clip"}]}))
    called: list[Any] = []
    monkeypatch.setattr(rs, "generate_images", lambda *a, **k: called.append(1))
    assert rs.run(workspace=tmp_path, requests_file=req, task_id="t") == 0
    assert called == []
    out = json.loads((tmp_path / "image-qa-regen-result-r1.json").read_text())
    assert out["refused_slots"] == ["cover"]


def test_rollback_never_deletes_media_that_already_existed():
    from scripts.wordpress import wp_publisher as wpp
    deleted: list[int] = []
    res = SimpleNamespace(rollback_attempted=False, rollback_succeeded=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wpp.wp_media, "delete_media", lambda wp, mid, force=True: deleted.append(mid))
        wpp._rollback(None, res, {"a": SimpleNamespace(id=1), "b": SimpleNamespace(id=2)}, True, keep_ids={1})
    assert deleted == [2]


def test_wp_library_import_is_unapproved_until_a_person_approves():
    from hermes_adapter.photo_library import merge_wp_media
    data: dict[str, Any] = {"photos": []}
    n = merge_wp_media(data, [{"id": 5, "media_type": "image", "source_url": "https://s/a.png",
                               "alt_text": "CL-001", "title": {"rendered": "CL 001"}},
                              {"id": 6, "media_type": "file"}])
    assert n == 1 and data["photos"][0]["approved"] is False
    assert merge_wp_media(data, [{"id": 5, "media_type": "image"}]) == 0


# ── existing posts ──────────────────────────────────────────────────────

def test_overlapping_posts_reach_the_agents(task, monkeypatch):
    from hermes_adapter import topic_overlap as to
    from hermes_adapter.agent import _env_preamble
    tid, ws = task
    titles = ["How to Choose Claw Clips for Wholesale", "Hair Claw Clip Sample Approval Checklist",
              "ABS vs PC Hair Claw Clips", "Custom Hair Claw Clip Tooling"]
    monkeypatch.setattr(to, "fetch_posts", lambda slug: [
        {"title": {"rendered": t}, "link": f"https://s/{i}", "status": "publish"} for i, t in enumerate(titles)])
    rel = to.write_for_task(ws, "claw clip sample approval", "clawclipfactory")
    assert [r["title"] for r in rel] == ["Hair Claw Clip Sample Approval Checklist"]
    pre = _env_preamble(tid, "clawclipfactory", ["Read"])
    assert "Hair Claw Clip Sample Approval Checklist" in pre and "different angle" in pre


# ── config merge keeps facts filled in on the server ────────────────────

def test_reinstall_keeps_server_filled_facts(project):
    from hermes_adapter.bootstrap_project import install
    p = PLUGIN_ROOT / "projects" / project / "business-context.json"
    bc = json.loads(p.read_text())
    bc["company"]["factory_facts"]["monthly_capacity"] = "300,000 pcs"
    bc["wordpress"]["default_categories"] = ["Materials"]
    p.write_text(json.dumps(bc))
    install(TPL, allow_todo=True, force=False)
    after = json.loads(p.read_text())
    assert after["company"]["factory_facts"]["monthly_capacity"] == "300,000 pcs"
    assert after["wordpress"]["default_categories"] == ["Materials"]
    assert after["editorial"]["skip_stages"] == ["citation-capsule-builder"]   # new keys still arrive
