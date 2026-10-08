"""Opt-in min_long_edge tolerance for relays that ignore the requested image size."""
from __future__ import annotations

import pytest
from PIL import Image

from scripts.openai.openai_image_pipeline import _verify_saved_dimensions


def _img(path, w, h):
    Image.new("RGB", (w, h), (200, 180, 150)).save(path)
    return path


def test_default_is_still_exact_match(tmp_path):
    p = _img(tmp_path / "a.png", 1672, 941)
    with pytest.raises(RuntimeError, match="dimension mismatch"):
        _verify_saved_dimensions(p, "3840x2160", "relay")


def test_relay_16x9_accepted_without_upscale(tmp_path):
    p = _img(tmp_path / "a.png", 1672, 941)
    _verify_saved_dimensions(p, "3840x2160", "relay", min_long_edge=1200)
    assert Image.open(p).size == (1672, 941)


def test_relay_16x9_cropped_to_requested_4x3(tmp_path):
    p = _img(tmp_path / "a.png", 1672, 941)
    _verify_saved_dimensions(p, "3264x2448", "relay", min_long_edge=1200)
    w, h = Image.open(p).size
    assert h == 941 and abs(w / h - 4 / 3) < 0.01


def test_too_small_after_crop_still_fails(tmp_path):
    p = _img(tmp_path / "a.png", 1672, 941)          # 1:1 crop → 941x941
    with pytest.raises(RuntimeError, match="below min_long_edge"):
        _verify_saved_dimensions(p, "2880x2880", "relay", min_long_edge=1200)


def test_provider_config_reads_min_long_edge(tmp_path, monkeypatch):
    from scripts._core import image_provider as ip
    home = tmp_path / "home"
    (home / ".xuanran-seo" / "credentials").mkdir(parents=True)
    (home / ".xuanran-seo" / "credentials" / "openai.key").write_text("sk-x")
    (home / ".xuanran-seo" / "config.yaml").write_text(
        "image:\n  providers:\n    - name: relay\n      base_url: https://r/v1\n"
        "      credential: openai\n      model: gpt-image-2\n      min_long_edge: 1200\n")
    monkeypatch.setattr(ip, "CONFIG_FILE", home / ".xuanran-seo" / "config.yaml", raising=False)
    monkeypatch.setenv("HOME", str(home))
    import importlib

    from scripts._core import credential_hub
    importlib.reload(credential_hub)
    importlib.reload(ip)
    try:
        provs = ip.resolve_providers()
        assert provs[0].name == "relay" and provs[0].min_long_edge == 1200
    finally:
        monkeypatch.undo()
        importlib.reload(credential_hub)
        importlib.reload(ip)


# ── seam: the real per-slot generator honours the provider's min_long_edge ──

class _FakeImages:
    def __init__(self, png: bytes):
        self.png = png
        self.calls = 0

    def generate(self, **kw):
        import base64
        from types import SimpleNamespace
        self.calls += 1
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(self.png).decode())])


def _relay_png(tmp_path) -> bytes:
    p = _img(tmp_path / "relay.png", 1672, 941)        # what the relay always returns
    return p.read_bytes()


def _run_slot(tmp_path, min_long_edge, size="3840x2160"):
    from types import SimpleNamespace

    from scripts._core.image_provider import ImageProvider
    from scripts.openai import openai_image_pipeline as oip
    prov = ImageProvider(name="relay", base_url="https://r/v1", api_key="k", model="gpt-image-2",
                         min_long_edge=min_long_edge)
    images = _FakeImages(_relay_png(tmp_path))
    client = SimpleNamespace(images=images)
    out = tmp_path / "out"
    out.mkdir()
    spec = oip.ImagePromptSpec(slot="cover", prompt="claw clips on linen", size=size)
    log = oip.PipelineLogger(out)
    return oip.generate_realtime_one([(prov, client)], spec, out, log), out


def test_seam_relay_slot_succeeds_with_min_long_edge(tmp_path, monkeypatch):
    from scripts.openai import openai_image_pipeline as oip
    monkeypatch.setattr(oip, "_watermark_after_save", lambda *a, **k: None)
    res, out = _run_slot(tmp_path, 1200)
    assert res.success, res.error
    assert Image.open(res.image_path).size == (1672, 941)


def test_seam_relay_slot_fails_without_setting(tmp_path, monkeypatch):
    from scripts.openai import openai_image_pipeline as oip
    monkeypatch.setattr(oip, "_watermark_after_save", lambda *a, **k: None)
    monkeypatch.setattr(oip.time, "sleep", lambda s: None)
    res, _ = _run_slot(tmp_path, None)
    assert not res.success and "dimension mismatch" in (res.error or "")
