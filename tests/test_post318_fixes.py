"""Regression tests for post 318 (clawclipfactory, "matte vs glossy claw clips", 2026-10-12).

The post passed every gate and still read like a compliance memo: about 25 sentences said a
finish label "does not prove" hold or scratch resistance, three sentences opened "For matte
vs glossy claw clips, ...", 13 internal links had anchors such as "final resin", and the FAQ
carried generic consumer PAA ("What is a claw clip for hair?") because the PAA gate demanded
them. The fixtures below are sentences from that post.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = PLUGIN_ROOT / "memory" / "workspace"
TPL = PLUGIN_ROOT / "hermes_adapter" / "templates" / "clawclipfactory"
KW = "matte vs glossy claw clips"

HEDGES_318 = [
    "Finish alone does not establish better hair hold or scratch resistance for any collection.",
    "Neither label proves better hold, durability or scratch resistance in daily use by itself.",
    "A polished tool surface alone does not guarantee that every resin or design will produce the same appearance.",
    "Neither matte nor glossy establishes a universal winner for hair hold or scratch resistance.",
    "A scratch can be easy to notice without proving that the surface scratches easily.",
    "Its scope does not make it automatically suitable for a small, curved or textured molded clip.",
    "Matte or glossy appearance is not a breakage-safety specification and buyers should not infer it.",
    "Treat that choice as the visual direction, not as proof of better hold, durability or scratch resistance.",
]

GOOD_318 = [
    "Our standard starting MOQ is 1,000 pieces per style, and we answer quote requests within 24 hours.",
    "Matte often fits neutral palettes, understated branding and product families where the silhouette carries the look.",
    "Glossy often fits saturated colors, translucent concepts or collections designed to catch light on the shelf.",
    "Once the physical sample is approved, we lock the molding conditions used for that result and keep them for repeat batches.",
]


def _doc(paragraphs: list[str], extra: str = "") -> str:
    return "---\ntitle: t\n---\n\n## Section\n\n" + "\n\n".join(paragraphs) + "\n\n" + extra + \
        "\n\n## References\n\n1. Source that does not prove anything, cannot be universal.\n"


CFG = {"max_hedge_sentences": 5, "max_keyword_openers": 1, "max_internal_links": 6,
       "_site_url": "https://clawclipfactory.com"}


# ── over-hedging ─────────────────────────────────────────────────────────

def test_repeated_caveats_fail_the_editorial_check():
    from hermes_adapter.editorial_check import check, repair_brief
    res = check(_doc(GOOD_318 + HEDGES_318), KW, CFG)
    rules = {v["rule"] for v in res["violations"]}
    assert "over_hedging" in rules and res["hedge_sentences"] == len(HEDGES_318)
    # the repair agent is shown the sentences to cut, not just a count
    assert "does not guarantee that every resin" in repair_brief(res)


def test_a_caveat_stated_once_passes():
    from hermes_adapter.editorial_check import check
    res = check(_doc(GOOD_318 + HEDGES_318[:2]), KW, CFG)
    assert res["passed"], res["violations"]
    assert res["hedge_sentences"] == 2


def test_hedge_counter_does_not_flag_plain_factory_facts():
    from hermes_adapter.editorial_check import hedge_sentences
    assert hedge_sentences(GOOD_318) == []


# ── keyword as a sentence label ──────────────────────────────────────────

def test_keyword_openers_fail_but_one_natural_use_passes():
    from hermes_adapter.editorial_check import check
    openers = [
        "For matte vs glossy claw clips, choose matte for a low-reflection look and glossy for highlights.",
        "For matte vs glossy claw clips, repeatability starts with a physical approved sample and a route.",
        "When comparing matte vs glossy claw clips, choose matte for low-reflection collection styling.",
    ]
    bad = check(_doc(GOOD_318 + openers), KW, CFG)
    assert any(v["rule"] == "keyword_opener" for v in bad["violations"]) and bad["keyword_openers"] == 3
    ok = check(_doc(GOOD_318 + openers[:1] + [
        "Our existing molds give buyers two concrete sourcing examples for matte vs glossy claw clips today."]),
        KW, CFG)
    assert ok["passed"], ok["violations"]


# ── internal links ───────────────────────────────────────────────────────

def _links(n: int, repeat: bool = False) -> list[str]:
    paths = ["/hair-claw-clips/", "/custom-tooling/", "/faq/", "/capabilities/", "/about/",
             "/insights/sample-approval/", "/insights/rfq/", "/insights/molds/", "/insights/x/"]
    out = []
    for i in range(n):
        p = paths[0] if repeat and i == n - 1 else paths[i]
        out.append(f"We keep the details in [page {i}](https://clawclipfactory.com{p}) for wholesale buyers who need them.")
    return out


def test_too_many_internal_links_fail():
    from hermes_adapter.editorial_check import check
    res = check(_doc(GOOD_318 + _links(9)), KW, CFG)
    assert any(v["rule"] == "internal_links" for v in res["violations"]) and res["internal_links"] == 9


def test_same_page_linked_twice_fails_even_under_the_cap():
    from hermes_adapter.editorial_check import check
    res = check(_doc(GOOD_318 + _links(4, repeat=True)), KW, CFG)
    v = [v for v in res["violations"] if v["rule"] == "internal_links"]
    assert v and "/hair-claw-clips" in v[0]["excerpt"]


def test_cta_module_and_external_links_are_not_counted():
    from hermes_adapter.editorial_check import check
    cta = ("### Work with us\n\nSend us the look you have in mind. "
           "[Get a Quote](https://clawclipfactory.com/contact/)")
    ext = "ASTM D2457 covers gloss on plastics ([ASTM](https://store.astm.org/d2457-21.html)) for buyers."
    res = check(_doc(GOOD_318 + _links(6) + [ext], extra=cta), KW, CFG)
    assert res["internal_links"] == 6 and res["passed"], res["violations"]


# ── FAQ "see above" ──────────────────────────────────────────────────────

def test_template_bans_see_above_faq_answers():
    from hermes_adapter.editorial_check import check
    cfg = json.loads((TPL / "business-context.json").read_text())["editorial"]
    md = _doc(GOOD_318 + ["See our sample timing and fee policy above, which explains everything for buyers."])
    assert any(v["rule"] == "banned_pattern" for v in check(md, KW, cfg)["violations"])


# ── template rules reach a server that was bootstrapped earlier ──────────

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


def test_reinstall_brings_new_editorial_rules_but_keeps_owner_facts(project):
    from hermes_adapter.bootstrap_project import install
    p = PLUGIN_ROOT / "projects" / project / "business-context.json"
    bc = json.loads(p.read_text())
    bc["company"]["factory_facts"]["monthly_capacity"] = "300,000 pcs"
    bc["editorial"]["banned_patterns"] = bc["editorial"]["banned_patterns"][:3]   # an older rule list
    bc["editorial"].pop("max_hedge_sentences")
    p.write_text(json.dumps(bc))
    install(TPL, allow_todo=True, force=False)
    after = json.loads(p.read_text())
    tpl = json.loads((TPL / "business-context.json").read_text())
    assert after["editorial"] == tpl["editorial"]
    assert after["company"]["factory_facts"]["monthly_capacity"] == "300,000 pcs"


# ── PAA gate no longer forces off-topic consumer questions ───────────────

PAA_318 = ["What is a claw clip for hair?", "Are metal or plastic claw clips better?",
           "Do hair clips cause breakage?", "Are claw clips bad for your hair?"]


def _paa_task(project: str, faq: list[str]) -> str:
    from hermes_adapter.task import create_task
    tid = create_task(project_slug=project, keyword=KW, image_count=2)
    ws = WS_ROOT / tid
    (ws / "research.json").write_text(json.dumps({"paa": PAA_318}))
    body = "\n\n".join(f"### {q}\n\nOur standard starting MOQ is 1,000 pieces per style for every "
                       f"finish, and we confirm the route against the mold." for q in faq)
    (ws / "draft.md").write_text(f"---\ntitle: t\n---\n\n## FAQ\n\n{body}\n\n## References\n\n1. x\n")
    return tid


def test_on_topic_faq_is_not_failed_for_ignoring_generic_paa(project):
    from scripts.lint.paa_alignment_check import check
    tid = _paa_task(project, ["Which finish hides scratches better on a matte or glossy clip?",
                              "Can one mold make both a matte and a glossy finish?",
                              "What is your MOQ for matte or glossy claw clips?"])
    try:
        res = check(tid)
        assert res["passed"], res["notes"]
        assert set(res["paa_off_topic"]) == set(PAA_318)
    finally:
        shutil.rmtree(WS_ROOT / tid, ignore_errors=True)


def test_on_topic_paa_is_still_demanded(project):
    from scripts.lint.paa_alignment_check import check
    tid = _paa_task(project, ["Which finish hides scratches better?", "Can one mold make both?",
                              "What is your MOQ?"])
    ws = WS_ROOT / tid
    (ws / "research.json").write_text(json.dumps({"paa": PAA_318 + [
        "Are matte claw clips better than glossy?", "Do glossy claw clips scratch easily?",
        "How do you make a matte finish on plastic?"]}))
    try:
        res = check(tid)
        assert not res["passed"] and res["paa_count"] == 3
    finally:
        shutil.rmtree(WS_ROOT / tid, ignore_errors=True)


def test_paa_filter_is_off_for_projects_that_did_not_opt_in():
    from scripts.lint.paa_alignment_check import _topic_filter
    ws = WS_ROOT / "testpaa_noproject"
    ws.mkdir(parents=True, exist_ok=True)
    try:
        (ws / "state.json").write_text(json.dumps({"project_slug": "no-such-project",
                                                   "brief": {"primary_keyword": KW}}))
        assert _topic_filter(ws) is None
    finally:
        shutil.rmtree(ws, ignore_errors=True)
