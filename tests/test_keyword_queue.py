"""Weekly scheduled articles (2026-10-21): the owner asked for two articles a week from the
keyword master file, never duplicating an existing post. The queue must skip a keyword an
existing post already covers, keep a partially overlapping one (written with a different
angle), and never consume a keyword on a run refused by the daily cost cap."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TPL = PLUGIN_ROOT / "hermes_adapter" / "templates" / "clawclipfactory"

POSTS = [
    {"id": 334, "title": {"rendered": "Claw Clip Sizes: mm and Inch Chart for Wholesale Buyers"}},
    {"id": 286, "title": {"rendered": "Existing Mold vs New Tooling for Hair Claw Clips: Buyer FAQ"}},
    {"id": 154, "title": {"rendered": "Hair Claw Clip Sample Approval Checklist for B2B Buyers"}},
    {"id": 47, "title": {"rendered": "Custom Hair Claw Clip Tooling for Private Label Brands"}},
    {"id": 43, "title": {"rendered": "ABS vs PC Hair Claw Clips: A Practical B2B Sourcing Guide"}},
]


@pytest.fixture()
def project():
    from hermes_adapter.bootstrap_project import install
    slug, _ = install(TPL, allow_todo=True, force=True)
    return slug


def _run(project, queue, monkeypatch, budget=(1.0, 40.0)):
    from hermes_adapter import run_article as ra
    from hermes_adapter import topic_overlap as to
    monkeypatch.setattr(to, "fetch_posts", lambda slug: POSTS)
    monkeypatch.setattr(ra, "_daily_budget_left", lambda: budget)
    started: list[str] = []

    def fake_create(**kw):
        started.append(kw["keyword"])
        raise SystemExit(0)  # stop before any spend; we only test which keyword was chosen
    monkeypatch.setattr(ra, "create_task", fake_create)
    args = SimpleNamespace(queue=queue, resume=None, project=project, keyword=None, keywords_file=None,
                           secondary="", locale="en-US", word_count=None, image_count=2, template=None,
                           no_notify=True, rewrite_of=[])
    cfg = SimpleNamespace(telegram_bot_token=None, telegram_chat_id=None)
    try:
        rc = ra._run(args, cfg, ra.argparse.ArgumentParser())
    except SystemExit:
        rc = None
    return rc, started


def test_covered_keyword_is_skipped_and_logged(project, monkeypatch, tmp_path):
    q = tmp_path / "queue.txt"
    q.write_text("claw clip sizes\ntypes of claw clips\nhow to use a claw clip\n")
    _, started = _run(project, q, monkeypatch)
    assert started == ["types of claw clips"]
    assert q.read_text() == "how to use a claw clip\n"
    done = (tmp_path / "queue.txt.done").read_text()
    assert "skipped_duplicate" in done and "claw clip sizes" in done and "Claw Clip Sizes" in done


def test_partial_overlap_is_written_with_a_different_angle_not_skipped(project, monkeypatch, tmp_path):
    q = tmp_path / "queue.txt"
    q.write_text("custom logo claw clips\n")
    _, started = _run(project, q, monkeypatch)
    assert started == ["custom logo claw clips"]


def test_over_budget_run_leaves_the_queue_untouched(project, monkeypatch, tmp_path):
    q = tmp_path / "queue.txt"
    q.write_text("types of claw clips\n")
    rc, started = _run(project, q, monkeypatch, budget=(41.0, 40.0))
    assert rc == 2 and started == [] and q.read_text() == "types of claw clips\n"


def test_unreadable_site_skips_nothing(project, monkeypatch, tmp_path):
    from hermes_adapter import run_article as ra
    from hermes_adapter import topic_overlap as to

    def boom(slug):
        raise RuntimeError("wp down")
    monkeypatch.setattr(to, "fetch_posts", boom)
    q = tmp_path / "queue.txt"
    q.write_text("claw clip sizes\n")
    kw, skipped = ra.next_queue_keyword(q, project)
    assert kw == "claw clip sizes" and skipped == []
