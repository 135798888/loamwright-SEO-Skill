"""scripts/openai/own_library_pipeline.py — fill photo slots from the project's OWN photo library.

Why: AI image models cannot draw a claw clip correctly (hinge, spring, two interlocking
rows of teeth). On clawclipfactory post 289 both generated photos had structurally
impossible clips, and the vision QA passed them. A factory's buyers judge the factory by
its product photos, so product and factory slots must be real photos.

Policy (business-context.json :: image_sourcing_policy):
    {"source": "own_library", "library": "photo-library.json"}

Library (projects/{slug}/photo-library.json):
    {"photos": [{"id": "cl-002-main", "file": "photos/cl-002.jpg" | "url": "https://...",
                 "wp_media_id": 123, "description": "Rectangular matte claw clip CL-002, ...",
                 "tags": ["product", "abs", "matte"], "kind": "product|factory|packaging|material|other",
                 "alt": "...", "caption": "...", "approved": true}]}

Only entries with "approved": true are used. Each photo slot in image-prompts.json is
matched by word overlap between the slot's text and the photo's description/tags/kind;
a photo is used at most once per article while unused photos remain. Charts are rendered
upstream (render_data_charts) and are never touched here. Photos from this library are
recorded with source "own_library"; image_regen_slots refuses to regenerate them.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
SOURCE = "own_library"

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("a an the and or of to for in on at by with is are be as this that it its our we you your "
            "photo image picture shot showing shows show realistic photograph background light no text "
            "logo logos close up closeup view style natural soft daylight neutral sharp focus".split())
_PRODUCT_WORDS = {"clip", "clips", "claw", "claws", "product", "products", "hair", "shark", "sample", "samples"}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def load_library(project_slug: str) -> tuple[list[dict[str, Any]], Path]:
    root = PLUGIN_ROOT / "projects" / project_slug
    policy = {}
    try:
        policy = json.loads((root / "business-context.json").read_text(encoding="utf-8")).get(
            "image_sourcing_policy") or {}
    except (OSError, ValueError):
        pass
    lib_path = root / (policy.get("library") or "photo-library.json")
    if not lib_path.exists():
        return [], lib_path
    data = json.loads(lib_path.read_text(encoding="utf-8"))
    photos = data.get("photos", data) if isinstance(data, dict) else data
    return [p for p in photos if isinstance(p, dict) and p.get("approved") is True], lib_path


def library_policy(project_slug: str) -> bool:
    """True when the project's policy names its own library as the photo source."""
    if not project_slug:
        return False
    try:
        bc = json.loads((PLUGIN_ROOT / "projects" / project_slug / "business-context.json")
                        .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (bc.get("image_sourcing_policy") or {}).get("source") == SOURCE


def uses_own_library(project_slug: str) -> bool:
    """Route photo slots to the library only once it holds at least one approved photo.

    Before that the project's AI style (brand-guideline.yaml) is used — for clawclipfactory
    flat illustrations, never photoreal clips — so a project can start publishing before
    its photo shoot, and switches to real photos automatically once they are approved."""
    return library_policy(project_slug) and bool(load_library(project_slug)[0])


def _slot_text(slot: dict[str, Any]) -> str:
    keys = ("slot_id", "alt_text_seed", "alt", "description", "purpose", "scene", "subject",
            "section_h2", "caption", "title", "product_noun", "filename_seed")
    return " ".join(str(slot.get(k) or "") for k in keys)


def _photo_text(p: dict[str, Any]) -> str:
    return " ".join([str(p.get("description") or ""), " ".join(p.get("tags") or []),
                     str(p.get("kind") or ""), str(p.get("alt") or "")])


def _is_cover(slot: dict[str, Any]) -> bool:
    return bool(slot.get("is_featured")) or slot.get("slot_id") == "cover"


def score(slot: dict[str, Any], photo: dict[str, Any]) -> float:
    s, p = _tokens(_slot_text(slot)), _tokens(_photo_text(photo))
    overlap = len(s & p)
    bonus = 0.0
    if _is_cover(slot) and photo.get("kind") == "product":
        bonus += 1.5  # a buyer-facing cover shows the product
    if s & _PRODUCT_WORDS and photo.get("kind") == "product":
        bonus += 0.5
    return overlap + bonus


def assign(slots: list[dict[str, Any]], photos: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """slot_id -> photo. Cover first; each photo used once while unused ones remain."""
    if not photos:
        return {}
    order = sorted(slots, key=lambda sl: 0 if _is_cover(sl) else 1)
    used: set[str] = set()
    out: dict[str, dict[str, Any]] = {}
    for sl in order:
        ranked = sorted(photos, key=lambda ph: (-score(sl, ph), str(ph.get("id"))))
        fresh = [ph for ph in ranked if str(ph.get("id")) not in used]
        pick = (fresh or ranked)[0]
        used.add(str(pick.get("id")))
        out[sl["slot_id"]] = pick
    return out


def _materialize(photo: dict[str, Any], lib_dir: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if photo.get("file"):
        src = (lib_dir / photo["file"]).resolve()
        if not src.exists():
            raise FileNotFoundError(f"library photo missing on disk: {src}")
        dst = dest_dir / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
        return dst
    url = photo.get("url")
    if not url:
        raise ValueError(f"library photo {photo.get('id')!r} has neither file nor url")
    name = Path(url.split("?")[0]).name or f"{photo.get('id')}.jpg"
    dst = dest_dir / name
    if not dst.exists():
        from scripts._core.ssrf_guard import validate_url
        validate_url(url)
        import httpx
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        dst.write_bytes(r.content)
    return dst


def run_for_workspace(workspace: str | Path, project_slug: str) -> list[dict[str, Any]]:
    ws = Path(workspace)
    if not ws.is_absolute() and not ws.exists():
        ws = PLUGIN_ROOT / "memory" / "workspace" / ws.name
    prompts = json.loads((ws / "image-prompts.json").read_text(encoding="utf-8"))
    plist = prompts if isinstance(prompts, list) else (
        prompts.get("slots") or prompts.get("images") or prompts.get("prompts") or [])
    slots = [p for p in plist if isinstance(p, dict) and p.get("slot_id")
             and str(p.get("kind", "photo")).lower() != "chart"]
    photos, lib_path = load_library(project_slug)
    if slots and not photos:
        raise RuntimeError(
            f"photo library has no approved photos ({lib_path}); add real photos with "
            "`python -m hermes_adapter.photo_library` or run the article with --image-count 0")
    mapping = assign(slots, photos)
    entries: list[dict[str, Any]] = []
    for sl in slots:
        ph = mapping[sl["slot_id"]]
        path = _materialize(ph, lib_path.parent, ws / "images")
        entry = {
            "slot_id": sl["slot_id"],
            "path": str(path.resolve()),
            "filename": path.name,
            "alt": sl.get("alt_text_seed") or sl.get("alt") or ph.get("alt") or ph.get("description", ""),
            "caption": ph.get("caption", ""),
            "title": ph.get("title") or ph.get("description", "")[:80],
            "description": ph.get("description", ""),
            "is_featured": _is_cover(sl),
            "source": SOURCE,
            "library_id": ph.get("id"),
            "upload_format": "original",
        }
        if isinstance(ph.get("wp_media_id"), int):
            entry["wp_media_id"] = ph["wp_media_id"]
        entries.append(entry)

    images_path = ws / "images.json"
    prior: list[dict[str, Any]] = []
    if images_path.exists():
        try:
            data = json.loads(images_path.read_text(encoding="utf-8"))
            prior = data if isinstance(data, list) else data.get("images", [])
        except (OSError, ValueError):
            prior = []
    by_slot = {e["slot_id"]: e for e in prior if isinstance(e, dict) and e.get("slot_id")}
    for e in entries:
        by_slot[e["slot_id"]] = e  # chart entries from render_data_charts survive
    merged = list(by_slot.values())
    images_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Fill photo slots from the project's own photo library")
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--project-slug", required=True)
    args = ap.parse_args(argv)
    merged = run_for_workspace(args.workspace, args.project_slug)
    print(json.dumps({"ok": True, "route": SOURCE, "count": len(merged)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
