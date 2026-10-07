"""SEOPress via its OWN REST API: payload mapping, write+readback, verify_post check 07."""
from __future__ import annotations

import pytest

from scripts.wordpress import seopress_api
from scripts.wordpress.verify_post import check_rankmath_meta
from scripts.wordpress.wp_client import WPNotFoundError

META = {"seo_title": "Custom Claw Clips Wholesale: MOQ, Molds and Lead Times",
        "meta_description": "How custom claw clip orders work.",
        "focus_keyphrase": ["custom claw clips wholesale", "claw clip manufacturer"],
        "canonical_url": "https://clawclipfactory.com/blog/custom-claw-clips/",
        "robots": ["index"], "category_ids": [12], "og_title": "OG"}


class _Resp:
    def __init__(self, data):
        self.json_data = data


class FakeSEOPress:
    """In-memory SEOPress. shape='dict' or 'kv' (list of {key, value}) for GET answers."""

    def __init__(self, shape: str = "dict", drop: set[str] | None = None, installed: bool = True):
        self.store: dict[str, dict] = {}
        self.shape, self.drop, self.installed = shape, drop or set(), installed
        self.puts: list[str] = []

    def _section(self, path: str) -> str:
        return path.rsplit("/", 1)[-1]

    def put(self, path, json_body=None):
        if not self.installed:
            raise WPNotFoundError(404, "rest_no_route")
        sec = self._section(path)
        self.puts.append(sec)
        if sec not in self.drop:                      # simulate a write that silently no-ops
            self.store.setdefault(sec, {}).update(json_body)
        return _Resp({"code": "success"})

    def get(self, path, params=None):
        if not self.installed:
            raise WPNotFoundError(404, "rest_no_route")
        data = dict(self.store.get(self._section(path), {}))
        if self.shape == "kv":
            return _Resp([{"key": k, "value": v} for k, v in data.items()])
        return _Resp(data)


def test_payloads_map_fields_and_never_send_index_flags_for_indexable_posts():
    p = seopress_api.build_payloads(META, featured_media_id=99)
    assert p["title-description-metas"] == {"title": META["seo_title"], "description": META["meta_description"]}
    assert p["target-keywords"] == {"_seopress_analysis_target_kw":
                                    "custom claw clips wholesale, claw clip manufacturer"}
    robots = p["meta-robot-settings"]
    assert robots["_seopress_robots_canonical"].endswith("/custom-claw-clips/")
    assert robots["_seopress_robots_primary_cat"] == "12"
    assert not any(k in robots for k in ("_seopress_robots_index", "_seopress_robots_follow"))
    assert p["social-settings"]["_seopress_social_fb_img_attachment_id"] == 99
    noindex = seopress_api.build_payloads({"robots": ["noindex", "nofollow"]})["meta-robot-settings"]
    assert noindex["_seopress_robots_index"] == "yes" and noindex["_seopress_robots_follow"] == "yes"


@pytest.mark.parametrize("shape", ["dict", "kv"])
def test_write_then_readback_verifies_for_both_response_shapes(shape):
    wp = FakeSEOPress(shape)
    ok, problems = seopress_api.write_seopress(wp, 5, META, 99)
    assert ok, problems
    assert set(wp.puts) == set(seopress_api.SECTIONS)
    got = seopress_api.read_seopress(wp, 5)
    assert got["title"] == META["seo_title"] and got["noindex"] is False


def test_silent_noop_write_is_caught_by_readback():
    wp = FakeSEOPress(drop={"title-description-metas"})
    ok, problems = seopress_api.write_seopress(wp, 5, META)
    assert not ok and any("title" in p for p in problems)


def test_read_returns_none_when_seopress_absent():
    assert seopress_api.read_seopress(FakeSEOPress(installed=False), 5) is None


def test_check07_seopress_branch_passes_and_fails_for_the_right_reasons():
    good = {"title": "T", "description": "D", "target_kw": "kw", "noindex": False}
    assert check_rankmath_meta({}, seopress=good).passed
    r = check_rankmath_meta({}, seopress={**good, "description": None})
    assert not r.passed and "description" in r.detail
    assert not check_rankmath_meta({}, seopress={**good, "noindex": True}).passed
    # focus keywords are reported, not required
    r2 = check_rankmath_meta({}, seopress={**good, "target_kw": None})
    assert r2.passed and "target keywords" in r2.detail


def test_check07_rankmath_path_unchanged():
    rm = {"meta": {"rank_math_title": "T", "rank_math_canonical_url": "u",
                   "rank_math_focus_keyword": "k", "rank_math_robots": ["index"]}}
    assert check_rankmath_meta(rm).passed
    assert not check_rankmath_meta({"meta": {}}).passed


def test_publisher_seopress_route_end_to_end():
    from scripts.wordpress import wp_publisher as wpp
    wp = FakeSEOPress()
    res = wpp.PublishResult(success=False, post_id=5, post_url=None, status=None)
    wpp._set_seopress_meta(wp, 5, META, None, res)
    assert res.seopress_set and res.seo_plugin_used == "seopress"
    # …and what verify_post would read for this draft passes check 07 (shared module seam)
    assert check_rankmath_meta({}, seopress=seopress_api.read_seopress(wp, 5)).passed


@pytest.mark.parametrize("shape", ["dict", "kv"])
def test_noindex_written_is_read_back_and_fails_check07(shape):
    wp = FakeSEOPress(shape)
    seopress_api.write_seopress(wp, 5, {**META, "robots": ["noindex"]})
    got = seopress_api.read_seopress(wp, 5)
    assert got["noindex"] is True
    assert not check_rankmath_meta({}, seopress=got).passed
