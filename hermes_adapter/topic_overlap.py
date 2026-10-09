"""Find existing posts on the site that cover the same ground as a new keyword.

Rule 1 (CLAUDE.md): a new keyword is a new article, never silently skipped. But the
writers must KNOW what the site already says, or they repeat it: post 289 duplicated
large parts of "How to Choose Claw Clips for Wholesale" and the sample-approval
checklist that were already live. The overlapping posts are written to
memory/workspace/{task}/existing-posts.json and shown to every agent of that task
(hermes_adapter.agent), which must take a different angle and link to them.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

_STOP = set("a an the and or of to for in on at by with how what why which is are your our you "
            "vs guide b2b buyers buyer best".split())


def _norm(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def tokens(text: str) -> set[str]:
    return {_norm(w) for w in re.findall(r"[a-z0-9]+", html.unescape(text or "").lower())
            if w not in _STOP}


def overlap(keyword: str, title: str, generic: set[str] | None = None) -> float:
    g = generic or set()
    k, t = tokens(keyword) - g, tokens(title) - g
    return len(k & t) / len(k) if k else 0.0


def _title(p: dict[str, Any]) -> str:
    return p.get("title", {}).get("rendered", "") if isinstance(p.get("title"), dict) else str(p.get("title", ""))


def site_generic_words(titles: list[str]) -> set[str]:
    """Words in at least half the site's titles ("claw", "clip", "hair" on a claw clip
    site) say nothing about the topic and would make every post look related."""
    if len(titles) < 3:
        return set()
    counts: dict[str, int] = {}
    for t in titles:
        for w in tokens(t):
            counts[w] = counts.get(w, 0) + 1
    return {w for w, c in counts.items() if c >= len(titles) / 2}


def related_posts(keyword: str, posts: list[dict[str, Any]], threshold: float = 0.5) -> list[dict[str, Any]]:
    generic = site_generic_words([_title(p) for p in posts])
    out = []
    for p in posts:
        title = _title(p)
        score = overlap(keyword, title, generic)
        if score >= threshold:
            out.append({"title": html.unescape(re.sub(r"<[^>]+>", "", title)), "url": p.get("link"),
                        "status": p.get("status"), "overlap": round(score, 2)})
    return sorted(out, key=lambda x: -x["overlap"])


def fetch_posts(project_slug: str) -> list[dict[str, Any]]:
    from scripts.wordpress.wp_client import WPClient
    wp = WPClient(project_slug)
    posts: list[dict[str, Any]] = []
    for page in range(1, 10):
        r = wp.get("/wp/v2/posts", params={"per_page": 100, "page": page, "context": "edit",
                                           "status": "publish,draft,pending,future",
                                           "_fields": "id,title,link,status"})
        batch = r.json_data or []
        posts += batch
        if len(batch) < 100:
            break
    return posts


def write_for_task(workspace: Path, keyword: str, project_slug: str) -> list[dict[str, Any]]:
    try:
        rel = related_posts(keyword, fetch_posts(project_slug))
    except Exception as e:  # noqa: BLE001 — never block an article on this lookup
        rel = []
        (workspace / "existing-posts.json").write_text(
            json.dumps({"error": f"{type(e).__name__}: {e}", "related": []}, indent=2), encoding="utf-8")
        return rel
    (workspace / "existing-posts.json").write_text(
        json.dumps({"keyword": keyword, "related": rel}, ensure_ascii=False, indent=2), encoding="utf-8")
    return rel


def brief_block(workspace: Path) -> str:
    p = workspace / "existing-posts.json"
    try:
        rel = json.loads(p.read_text(encoding="utf-8")).get("related") or []
    except (OSError, ValueError):
        return ""
    if not rel:
        return ""
    lines = [f"- {r['title']} ({r.get('url')})" for r in rel[:8]]
    return ("\n\n## EXISTING ARTICLES ON THIS SITE THAT OVERLAP THIS TOPIC\n"
            "Do not repeat their content or structure. Choose a clearly different angle for this "
            "keyword, cover only what they do not, and link to them where a reader needs that detail.\n"
            + "\n".join(lines))
