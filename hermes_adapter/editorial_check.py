"""Deterministic editorial gate for a project's own rules (business-context.json :: editorial).

The pipeline's gates were written for third-party review sites. They never noticed what
made post 289 (clawclipfactory, 2026-10-08) read badly: disclaimers about the company
itself, "When to delay your purchase" sections, the exact keyword jammed in again and
again, and the same advice repeated. The editorial brief asks writers not to do these
things; this module checks that they did not, so the brief has an executor (Rule 6).

    python -m hermes_adapter.editorial_check --task <task_id> [--json]

Config keys (all optional):
    banned_patterns            regexes matched case-insensitively against the body
    banned_h2_patterns         regexes matched against H2 text
    max_exact_keyword_uses     cap on exact primary-keyword occurrences in the body
    max_near_duplicate_sentences  cap on sentence pairs that say the same thing
    max_hedge_sentences        cap on "X does not prove / is not a universal ..." caveats
    max_keyword_openers        cap on sentences opening "For <keyword>, ..."
    max_internal_links         cap on links to our own site (each page is linked once)

Post 318 (2026-10-12) passed the first four checks and still read like a compliance memo:
about 25 sentences restated that a finish label "does not prove" hold or scratch resistance,
three sentences opened "For matte vs glossy claw clips, ...", and 13 internal links with
anchors such as "final resin" sat in 2,000 words. Paraphrases slip past the near-duplicate
check, so the caveat count is measured directly.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("the a an and or of to for in on at by with is are be as that this it your our we you "
            "can may from not but if into than then their them they its".split())


def _body(markdown: str) -> str:
    text = re.sub(r"\A---\n.*?\n---\n", "", markdown, flags=re.DOTALL)
    # References are source titles, not our prose.
    text = re.split(r"^##\s+References\b.*$", text, maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)[0]
    return text


def _h2s(text: str) -> list[str]:
    return [re.sub(r"\s*\{#[^}]*\}\s*$", "", m.group(1)).strip()
            for m in re.finditer(r"^##\s+(?!#)(.+)$", text, flags=re.MULTILINE)]


def _plain(text: str) -> str:
    text = re.sub(r"^\s*\|.*\|\s*$", " ", text, flags=re.MULTILINE)          # tables
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)                          # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)                       # links
    text = re.sub(r"\[(IMAGE-SLOT|claim:)[^\]]*\]", " ", text)
    text = re.sub(r"^#+\s.*$", " ", text, flags=re.MULTILINE)                  # headings
    return re.sub(r"[*_`>#]", " ", text)


def _sentences(plain: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", plain)
    return [" ".join(p.split()) for p in parts if len(p.split()) >= 8]


def _tokens(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in _STOP}


_HEDGE_NEG = re.compile(r"\b(not|no|neither|nor|cannot|can't|without|doesn't|don't|isn't)\b", re.I)
_HEDGE_CLAIM = re.compile(
    r"\b(prove[sn]?|proving|proof|establish(es|ed)?|guarantee[sd]?|universal|automatically|"
    r"evidence|substitute|stand in|infer(red)?|assumed?)\b", re.I)


def hedge_sentences(sentences: list[str]) -> list[str]:
    """Sentences that warn what something does NOT prove/establish/guarantee."""
    return [s for s in sentences if _HEDGE_NEG.search(s) and _HEDGE_CLAIM.search(s)]


def keyword_openers(sentences: list[str], keyword: str) -> list[str]:
    """Sentences that open with the exact keyword as a noun phrase: "For <kw>, ..."."""
    if not keyword:
        return []
    pat = re.compile(r"^(?:[A-Za-z]+\s+){0,2}" + re.escape(keyword) + r"\s*,", re.I)
    return [s for s in sentences if pat.match(s)]


_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)[^)]*\)")


def _without_cta_blocks(body: str) -> str:
    """Drop CTA modules (heading classified as a CTA component + its paragraph):
    their button link is the conversion element, not an in-text link."""
    try:
        from scripts._core.component_headings import classify_heading
    except ImportError:  # pragma: no cover - plugin layout always has it
        return body
    out: list[str] = []
    skipping = False
    for block in re.split(r"\n{2,}", body):
        m = re.match(r"^#{2,3}\s+(.+)$", block.strip())
        if m:
            comp = classify_heading(re.sub(r"\s*\{#[^}]*\}\s*$", "", m.group(1)))
            skipping = bool(comp and comp.startswith("cta"))
            if skipping:
                continue
        elif skipping:
            skipping = False   # the one CTA paragraph
            continue
        out.append(block)
    return "\n\n".join(out)


def internal_links(body: str, site_url: str) -> list[tuple[str, str]]:
    """(anchor, normalized target) for every in-text link to our own site (CTA modules excluded)."""
    body = _without_cta_blocks(body)
    host = re.sub(r"^https?://(www\.)?", "", (site_url or "").lower()).strip("/")
    out: list[tuple[str, str]] = []
    for m in _LINK.finditer(body):
        url = m.group(2).strip().lower()
        if url.startswith("#"):
            continue
        if url.startswith("/"):
            path = url
        elif host and re.match(r"^https?://(www\.)?" + re.escape(host) + r"(/|$)", url):
            path = re.sub(r"^https?://[^/]+", "", url) or "/"
        else:
            continue
        path = path.split("#")[0].split("?")[0].rstrip("/") or "/"
        out.append((m.group(1), path))
    return out


def near_duplicates(sentences: list[str], threshold: float = 0.7) -> list[tuple[str, str]]:
    toks = [_tokens(s) for s in sentences]
    pairs: list[tuple[str, str]] = []
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            a, b = toks[i], toks[j]
            if len(a) < 5 or len(b) < 5:
                continue
            if len(a & b) / len(a | b) >= threshold:
                pairs.append((sentences[i], sentences[j]))
    return pairs


def check(markdown: str, primary_keyword: str, cfg: dict[str, Any]) -> dict[str, Any]:
    body = _body(markdown)
    plain = _plain(body)
    violations: list[dict[str, Any]] = []

    for pat in cfg.get("banned_patterns") or []:
        for m in re.finditer(pat, plain, flags=re.IGNORECASE):
            start = max(0, m.start() - 80)
            violations.append({"rule": "banned_pattern", "pattern": pat,
                               "excerpt": " ".join(plain[start:m.end() + 80].split())})
    for h2 in _h2s(body):
        for pat in cfg.get("banned_h2_patterns") or []:
            if re.search(pat, h2, flags=re.IGNORECASE):
                violations.append({"rule": "banned_h2", "pattern": pat, "excerpt": h2})

    kw_uses = 0
    if primary_keyword:
        kw_uses = len(re.findall(r"\b" + re.escape(primary_keyword.lower()) + r"\b", plain.lower()))
        cap = cfg.get("max_exact_keyword_uses")
        if isinstance(cap, int) and kw_uses > cap:
            violations.append({"rule": "keyword_stuffing",
                               "excerpt": f"exact phrase '{primary_keyword}' used {kw_uses} times "
                                          f"(limit {cap}); replace the extra uses with natural variants"})

    dups = near_duplicates(_sentences(plain))
    cap = cfg.get("max_near_duplicate_sentences")
    if isinstance(cap, int) and len(dups) > cap:
        violations.append({"rule": "repetition",
                           "excerpt": f"{len(dups)} sentence pairs repeat the same point (limit {cap})",
                           "pairs": [list(p) for p in dups[:8]]})

    sentences = _sentences(plain)
    hedges = hedge_sentences(sentences)
    cap = cfg.get("max_hedge_sentences")
    if isinstance(cap, int) and len(hedges) > cap:
        violations.append({"rule": "over_hedging",
                           "excerpt": f"{len(hedges)} sentences warn what something does not prove or "
                                      f"guarantee (limit {cap}); state each limitation once where it "
                                      f"matters and replace the rest with what the buyer should do",
                           "sentences": hedges[:12]})

    openers = keyword_openers(sentences, primary_keyword)
    cap = cfg.get("max_keyword_openers")
    if isinstance(cap, int) and len(openers) > cap:
        violations.append({"rule": "keyword_opener",
                           "excerpt": f"{len(openers)} sentences open with '{primary_keyword},' (limit {cap}); "
                                      f"start them with the point instead",
                           "sentences": openers[:6]})

    links = internal_links(body, cfg.get("_site_url", ""))
    cap = cfg.get("max_internal_links")
    if isinstance(cap, int):
        seen: dict[str, int] = {}
        for _, path in links:
            seen[path] = seen.get(path, 0) + 1
        repeated = sorted(p for p, n in seen.items() if n > 1)
        if len(links) > cap or repeated:
            msg = f"{len(links)} internal links (limit {cap}"
            msg += f"; linked more than once: {', '.join(repeated)})" if repeated else ")"
            violations.append({"rule": "internal_links",
                               "excerpt": msg + "; keep the most useful links, each page once, and turn "
                                                "the rest back into plain text",
                               "sentences": [f"[{a}] -> {p}" for a, p in links][:20]})

    return {"passed": not violations, "violations": violations,
            "exact_keyword_uses": kw_uses, "near_duplicate_pairs": len(dups),
            "hedge_sentences": len(hedges), "keyword_openers": len(openers),
            "internal_links": len(links)}


def project_config(project_slug: str | None) -> dict[str, Any]:
    if not project_slug:
        return {}
    p = PLUGIN_ROOT / "projects" / project_slug / "business-context.json"
    try:
        ctx = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cfg = dict(ctx.get("editorial") or {})
    if cfg:
        cfg["_site_url"] = ctx.get("site_url") or ""
    return cfg


def run(task_id: str) -> dict[str, Any]:
    ws = PLUGIN_ROOT / "memory" / "workspace" / task_id
    state = json.loads((ws / "state.json").read_text(encoding="utf-8"))
    cfg = project_config(state.get("project_slug"))
    keyword = (state.get("brief") or {}).get("primary_keyword", "")
    res = check((ws / "draft.md").read_text(encoding="utf-8"), keyword, cfg)
    res["configured"] = bool(cfg)
    (ws / "editorial-check.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def repair_brief(res: dict[str, Any]) -> str:
    lines = ["The project's editorial check failed. Fix each item in draft.md with the smallest edit:"]
    for v in res.get("violations", []):
        lines.append(f"- [{v['rule']}] {v['excerpt']}")
        for line in v.get("sentences", [])[:12]:
            lines.append(f"    - \"{line[:180]}\"")
        for a, b in v.get("pairs", [])[:5]:
            lines.append(f"    repeated: \"{a[:160]}\" / \"{b[:160]}\" -> keep the better one, cut or merge the other")
    lines.append("Rules: a banned H2 is replaced by a factory-perspective section (rename and rewrite it, "
                 "e.g. 'What to send us for an accurate quote'); disclaimers about the company are deleted, "
                 "not reworded; extra exact keyword uses become natural variants; repeated caveats are cut to one, kept in the "
                 "section where they matter; removed internal links keep their words as plain text. Keep claim markers, image "
                 "slots, the CTA module and References intact.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = run(args.task)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else
          ("PASS" if res["passed"] else repair_brief(res)))
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
