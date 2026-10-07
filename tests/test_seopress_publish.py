"""SEOPress support: meta mapping, publisher routing, and verify_post check 07 for drafts."""
from __future__ import annotations

from scripts.wordpress.verify_post import check_rankmath_meta
from scripts.wordpress.wp_publisher import build_seopress_meta


def test_seopress_mapping_core_fields_and_inverted_robots():
    meta = {"seo_title": "Custom Claw Clips Wholesale: MOQ, Molds and Lead Times",
            "meta_description": "How custom claw clip orders work.",
            "focus_keyphrase": ["custom claw clips wholesale", "claw clip manufacturer"],
            "canonical_url": "https://clawclipfactory.com/blog/custom-claw-clips/",
            "robots": ["index"], "category_ids": [12], "og_title": "OG"}
    out = build_seopress_meta(meta, featured_media_id=99)
    assert out["_seopress_titles_title"].startswith("Custom Claw Clips")
    assert out["_seopress_titles_desc"] == "How custom claw clip orders work."
    assert out["_seopress_analysis_target_kw"] == "custom claw clips wholesale, claw clip manufacturer"
    assert out["_seopress_robots_canonical"].endswith("/custom-claw-clips/")
    assert out["_seopress_robots_index"] == ""          # indexable → empty, NOT "no"
    assert out["_seopress_robots_follow"] == ""
    assert out["_seopress_robots_primary_cat"] == "12"
    assert out["_seopress_social_fb_img_attachment_id"] == "99"
    noindex = build_seopress_meta({"robots": ["noindex", "nofollow"]})
    assert noindex["_seopress_robots_index"] == "yes" and noindex["_seopress_robots_follow"] == "yes"
    assert "rank_math_title" not in out


def test_verify_check07_seopress_draft_passes_and_fails_for_the_right_reasons():
    good = {"meta": {"_seopress_titles_title": "T", "_seopress_titles_desc": "D",
                     "_seopress_analysis_target_kw": "kw", "_seopress_robots_index": ""}}
    assert check_rankmath_meta(good).passed
    missing = {"meta": {"_seopress_titles_title": "T", "_seopress_titles_desc": "",
                        "_seopress_analysis_target_kw": "kw"}}
    r = check_rankmath_meta(missing)
    assert not r.passed and "_seopress_titles_desc" in r.detail
    noindex = {"meta": {**good["meta"], "_seopress_robots_index": "yes"}}
    assert not check_rankmath_meta(noindex).passed


def test_verify_check07_rankmath_path_unchanged():
    rm = {"meta": {"rank_math_title": "T", "rank_math_canonical_url": "u",
                   "rank_math_focus_keyword": "k", "rank_math_robots": ["index"]}}
    assert check_rankmath_meta(rm).passed
    assert not check_rankmath_meta({"meta": {}}).passed


def test_publisher_routes_to_seopress(monkeypatch):
    from scripts.wordpress import wp_publisher as wpp

    class FakeResp:
        def __init__(self, data):
            self.json_data = data

    class FakeWP:
        def __init__(self):
            self.posted = None

        def post(self, path, json_body=None):
            self.posted = (path, json_body)
            return FakeResp({})

        def get(self, path, params=None):
            return FakeResp({"meta": dict(self.posted[1]["meta"])})

    wp = FakeWP()
    res = wpp.PublishResult(success=False, post_id=5, post_url=None, status=None)
    wpp._set_seopress_meta(wp, 5, {"seo_title": "T", "meta_description": "D",
                                   "focus_keyphrase": "k"}, None, res)
    assert wp.posted[0] == "/wp/v2/posts/5"
    assert res.seopress_set and res.seo_plugin_used == "seopress"

    class LossyWP(FakeWP):
        def get(self, path, params=None):
            return FakeResp({"meta": {}})            # bridge missing: nothing reads back
    res2 = wpp.PublishResult(success=False, post_id=5, post_url=None, status=None)
    wpp._set_seopress_meta(LossyWP(), 5, {"seo_title": "T"}, None, res2)
    assert not res2.seopress_set
