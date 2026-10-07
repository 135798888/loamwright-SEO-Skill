"""SEOPress native REST API (free, built into SEOPress — no MU-plugin needed).

Single source of truth for how the pipeline WRITES and READS SEOPress post SEO
fields; used by both wp_publisher (write + readback) and verify_post (check 07),
so the two can never disagree about field names (Rule 11).

Endpoints (SEOPress docs, "Get started with the SEOPress REST API"; capability
edit_post; Application Password Basic auth):

    PUT /seopress/v1/posts/{id}/title-description-metas   {title, description}
    PUT /seopress/v1/posts/{id}/target-keywords           {_seopress_analysis_target_kw}
    PUT /seopress/v1/posts/{id}/meta-robot-settings       {_seopress_robots_*: "yes"|"no", canonical, primary_cat, breadcrumbs}
    PUT /seopress/v1/posts/{id}/social-settings           {_seopress_social_*}

The same authenticated paths answer GET, which — unlike the public
GET /seopress/v1/posts/{id} (published posts only) — also works for DRAFTS.
Their response shapes are not formally documented (dict, or a list of
{key, value}), so read_seopress() normalizes every shape into one flat dict.

SEOPress robots flags are inverted vs Rank Math: "_seopress_robots_index": "yes"
means NOINDEX. We only ever send a flag to ENABLE a restriction the meta asks
for; an indexable post sends no robots flags at all (SEOPress default = index).
"""
from __future__ import annotations

from typing import Any

SECTIONS = ("title-description-metas", "target-keywords", "meta-robot-settings", "social-settings")

_ROBOT_FLAGS = {
    "noindex": "_seopress_robots_index",
    "nofollow": "_seopress_robots_follow",
    "noimageindex": "_seopress_robots_imageindex",
    "noarchive": "_seopress_robots_archive",
    "nosnippet": "_seopress_robots_snippet",
}


def build_payloads(meta: dict, featured_media_id: int | None = None) -> dict[str, dict]:
    """Map the pipeline's FLAT meta.json onto SEOPress endpoint payloads (pure)."""
    out: dict[str, dict] = {}

    title = meta.get("seo_title") or meta.get("title", "")
    desc = meta.get("meta_description") or meta.get("excerpt", "")
    td = {k: v for k, v in (("title", title), ("description", desc)) if v}
    if td:
        out["title-description-metas"] = td

    focus = meta.get("focus_keyphrase") or ""
    if isinstance(focus, list):
        focus = ", ".join(str(k) for k in focus if k)
    if focus:
        out["target-keywords"] = {"_seopress_analysis_target_kw": focus}

    robots: dict[str, str] = {}
    directives = meta.get("robots") if isinstance(meta.get("robots"), list) else []
    for directive, key in _ROBOT_FLAGS.items():
        if directive in directives:
            robots[key] = "yes"
    if meta.get("canonical_url"):
        robots["_seopress_robots_canonical"] = meta["canonical_url"]
    if meta.get("breadcrumb_title"):
        robots["_seopress_robots_breadcrumbs"] = meta["breadcrumb_title"]
    if isinstance(meta.get("primary_category"), int):
        robots["_seopress_robots_primary_cat"] = str(meta["primary_category"])
    elif isinstance(meta.get("category_ids"), list) and meta["category_ids"]:
        robots["_seopress_robots_primary_cat"] = str(meta["category_ids"][0])
    if robots:
        out["meta-robot-settings"] = robots

    social: dict[str, Any] = {}
    for src, dst in (("og_title", "_seopress_social_fb_title"),
                     ("og_description", "_seopress_social_fb_desc"),
                     ("og_image", "_seopress_social_fb_img"),
                     ("twitter_title", "_seopress_social_twitter_title"),
                     ("twitter_description", "_seopress_social_twitter_desc"),
                     ("twitter_image", "_seopress_social_twitter_img")):
        if meta.get(src):
            social[dst] = meta[src]
    if not meta.get("og_image") and featured_media_id:
        social["_seopress_social_fb_img_attachment_id"] = int(featured_media_id)
    if social:
        out["social-settings"] = social
    return out


def _flatten(obj: Any, into: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        if set(obj) >= {"key", "value"} and isinstance(obj.get("key"), str):
            into[obj["key"]] = obj["value"]
            return
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _flatten(v, into)
            else:
                into[str(k)] = v
    elif isinstance(obj, list):
        for item in obj:
            _flatten(item, into)


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten any SEOPress GET shape(s) into {title, description, target_kw, noindex}."""
    flat: dict[str, Any] = {}
    _flatten(raw, flat)

    def first(*keys: str) -> Any:
        for k in keys:
            if flat.get(k) not in (None, ""):
                return flat[k]
        return None

    noindex_raw = first("_seopress_robots_index", "noindex")
    noindex = noindex_raw is True or str(noindex_raw).lower() in ("yes", "true", "1")
    return {
        "title": first("title", "_seopress_titles_title"),
        "description": first("description", "_seopress_titles_desc"),
        "target_kw": first("_seopress_analysis_target_kw", "target_keywords", "keywords"),
        "noindex": noindex,
        "canonical": first("canonical", "_seopress_robots_canonical"),
    }


def read_seopress(wp, post_id: int) -> dict[str, Any] | None:
    """Authenticated read of a post's SEOPress fields (works for drafts).

    Returns None when SEOPress's API is not reachable at all (plugin inactive,
    route missing) — callers must treat that as UNKNOWN, never as "fields match".
    """
    raw: dict[str, Any] = {}
    reached = False
    for section in ("title-description-metas", "target-keywords", "meta-robot-settings"):
        try:
            r = wp.get(f"/seopress/v1/posts/{post_id}/{section}")
        except Exception:  # noqa: BLE001 — one section failing must not hide the others
            continue
        reached = True
        raw[section] = r.json_data
    return normalize(raw) if reached else None


def write_seopress(wp, post_id: int, meta: dict, featured_media_id: int | None = None
                   ) -> tuple[bool, list[str]]:
    """PUT every payload, then READ BACK (Rule 13: a 200 is not proof).

    Returns (verified, problems).
    """
    problems: list[str] = []
    payloads = build_payloads(meta, featured_media_id)
    for section, body in payloads.items():
        try:
            wp.put(f"/seopress/v1/posts/{post_id}/{section}", json_body=body)
        except Exception as e:  # noqa: BLE001
            problems.append(f"PUT {section} failed: {e}")
    got = read_seopress(wp, post_id)
    if got is None:
        problems.append("could not read SEOPress fields back (is SEOPress active?)")
        return False, problems
    td = payloads.get("title-description-metas", {})
    if td.get("title") and got.get("title") != td["title"]:
        problems.append(f"title reads back as {got.get('title')!r}")
    if td.get("description") and got.get("description") != td["description"]:
        problems.append(f"description reads back as {got.get('description')!r}")
    kw = payloads.get("target-keywords", {}).get("_seopress_analysis_target_kw")
    if kw and got.get("target_kw") != kw:
        problems.append(f"target keywords read back as {got.get('target_kw')!r}")
    return not problems, problems
