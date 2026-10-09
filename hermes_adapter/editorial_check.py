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

    return {"passed": not violations, "violations": violations,
            "exact_keyword_uses": kw_uses, "near_duplicate_pairs": len(dups)}


def project_config(project_slug: str | None) -> dict[str, Any]:
    if not project_slug:
        return {}
    p = PLUGIN_ROOT / "projects" / project_slug / "business-context.json"
    try:
        return (json.loads(p.read_text(encoding="utf-8")).get("editorial") or {})
    except (OSError, ValueError):
        return {}


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
        for a, b in v.get("pairs", [])[:5]:
            lines.append(f"    repeated: \"{a[:160]}\" / \"{b[:160]}\" -> keep the better one, cut or merge the other")
    lines.append("Rules: a banned H2 is replaced by a factory-perspective section (rename and rewrite it, "
                 "e.g. 'What to send us for an accurate quote'); disclaimers about the company are deleted, "
                 "not reworded; extra exact keyword uses become natural variants. Keep claim markers, image "
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
