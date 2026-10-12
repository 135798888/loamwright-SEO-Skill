"""Task 20261012_c1ee622e ("claw clip sizes", 2026-10-12): the generated signature read
"For Need custom claw clips? Send us your design or reference photos for a quote., [contact
our team](...)". The noun-phrase frame "For {cta_clause}, ..." was applied to a sentence.
The second opinion failed NATURAL WRITING on it twice, and a repair agent cannot fix a line
the pipeline generates, so the article never reached a draft.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = PLUGIN_ROOT / "memory" / "workspace"
TPL = PLUGIN_ROOT / "hermes_adapter" / "templates" / "clawclipfactory"
BROKEN = ("*Last reviewed and updated: October 2026. Author: ClawClipFactory Team. For Need custom "
          "claw clips? Send us your design or reference photos for a quote., "
          "[contact our team](https://clawclipfactory.com/request-a-quote/).*")


@pytest.fixture()
def project():
    from hermes_adapter.bootstrap_project import install
    slug, _ = install(TPL, allow_todo=True, force=True)
    yield slug


@pytest.fixture()
def task(project):
    from hermes_adapter.task import create_task
    tid = create_task(project_slug=project, keyword="claw clip sizes", image_count=2)
    yield tid, WS_ROOT / tid
    shutil.rmtree(WS_ROOT / tid, ignore_errors=True)


def _draft(sig: str) -> str:
    return ("---\ntitle: t\n---\n\n## Sizes\n\nOur CL-002 measures 105 mm and CL-003 measures 110 mm.\n\n"
            "## References\n\n1. NIST. (n.d.). SI units. https://www.nist.gov/\n\n---\n\n" + sig + "\n")


def test_sentence_cta_clause_reads_as_a_sentence(project):
    from scripts.build.finalize_refs_signature import canonical_signature
    sig = canonical_signature(project)
    assert "For Need" not in sig and "quote.," not in sig
    assert "Need custom claw clips? Send us your design or reference photos for a quote. " \
           "[Contact our team](https://clawclipfactory.com/request-a-quote/).*" in sig


def test_noun_phrase_cta_clause_keeps_the_for_frame(monkeypatch):
    from scripts.build import finalize_refs_signature as f
    monkeypatch.setattr(f, "_signature_template", lambda slug: None)
    monkeypatch.setattr(f, "_signature_spec", lambda slug: ("Team", "/contact/", "tailored guidance"))
    assert "For tailored guidance, [contact our team](/contact/).*" in f.canonical_signature("x")


def test_finalize_replaces_a_broken_writer_signature(task):
    from scripts.build.finalize_refs_signature import finalize
    tid, ws = task
    (ws / "draft.md").write_text(_draft(BROKEN), encoding="utf-8")
    finalize(tid, "clawclipfactory")
    out = (ws / "draft.md").read_text()
    assert "For Need" not in out and out.count("Last reviewed and updated") == 1


def test_driver_refreshes_the_signature_before_any_judge_reads_it(task, monkeypatch):
    """Seam: a draft resumed with the broken signature already in it must reach the
    second opinion with the canonical line, without a repair agent."""
    from hermes_adapter import driver as drv
    from tests.test_hermes_adapter import _cfg
    tid, ws = task
    (ws / "draft.md").write_text(_draft(BROKEN), encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(drv.Driver, "_second_opinion",
                        lambda self: seen.append((ws / "draft.md").read_text()))
    monkeypatch.setattr(drv.Driver, "_generic_repair", lambda *a: pytest.fail("no repair expected"))
    monkeypatch.setattr(drv.Driver, "_dispatch", lambda self, r: True)
    seq = iter([{"action": "DISPATCH_LLM", "stage": "independent-reviewer", "subagent_type": "",
                 "expected_outputs": []}, {"action": "COMPLETE"}])
    rep = drv.Driver(_cfg(), tid, pipeline_drive=lambda *a, **k: next(seq)).run()
    assert rep.status == "complete"
    assert seen and "For Need" not in seen[0] and "[Contact our team]" in seen[0]
    assert json.loads((ws / "editorial-check.json").read_text())["passed"] is True


# ── rewriting a weaker draft for the same keyword (post 326, 2026-10-12) ──

def test_rewrite_of_keeps_the_old_draft_out_of_the_overlap_brief(project, monkeypatch):
    """Seam: run_article._run with --rewrite-of 326. Without it, draft 326 ('Claw Clip Sizes')
    is the strongest overlap and the brief tells the writer to take a different angle, which
    is the opposite of a rewrite."""
    import sys
    from types import SimpleNamespace
    from hermes_adapter import run_article as ra
    from hermes_adapter import topic_overlap as to
    from hermes_adapter.agent import _env_preamble
    posts = [{"id": 326, "title": {"rendered": "Claw Clip Sizes: How to Measure and Specify Bulk Orders"},
              "link": "https://clawclipfactory.com/?p=326", "status": "draft"},
             {"id": 43, "title": {"rendered": "ABS vs PC Hair Claw Clips: A Practical B2B Sourcing Guide"},
              "link": "https://clawclipfactory.com/abs-vs-pc/", "status": "publish"},
             {"id": 154, "title": {"rendered": "Hair Claw Clip Sample Approval Checklist for B2B Buyers"},
              "link": "https://clawclipfactory.com/sample/", "status": "publish"}]
    monkeypatch.setattr(to, "fetch_posts", lambda slug: posts)
    seen: list[str] = []
    monkeypatch.setattr(ra, "drive_task", lambda cfg, tid, on_event=None: (
        seen.append(tid), SimpleNamespace(as_dict=lambda: {"status": "complete", "finished_at": "x"}))[1])
    cfg = SimpleNamespace(telegram_bot_token=None, telegram_chat_id=None)

    def run(extra):
        monkeypatch.setattr(sys, "argv", ["run_article", "--project", project, "--keyword",
                                          "claw clip sizes", "--no-notify", *extra])
        ap = ra.argparse.ArgumentParser()
        args = SimpleNamespace(queue=None, resume=None, project=project, keyword="claw clip sizes",
                               keywords_file=None, secondary="", locale="en-US", word_count=None,
                               image_count=2, template=None, no_notify=True,
                               rewrite_of=[int(x) for x in extra[1:2]] if extra else [])
        assert ra._run(args, cfg, ap) == 0
        tid = seen[-1]
        try:
            return _env_preamble(tid, project, ["Read"])
        finally:
            shutil.rmtree(WS_ROOT / tid, ignore_errors=True)

    assert "Claw Clip Sizes: How to Measure" in run([])
    assert "Claw Clip Sizes: How to Measure" not in run(["--rewrite-of", "326"])
