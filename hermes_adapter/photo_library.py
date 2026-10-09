"""Manage a project's own photo library (projects/{slug}/photo-library.json).

Articles use these REAL photos for product and factory images instead of AI renders
(scripts/openai/own_library_pipeline.py). Only photos marked approved are used, so
nothing reaches an article until a person has said "this is a real photo of ours".

    # import every image already in the WordPress media library (unapproved)
    python -m hermes_adapter.photo_library clawclipfactory --from-wp

    # add a photo from disk (approved straight away)
    python -m hermes_adapter.photo_library clawclipfactory --add /path/cl-002-pink.jpg \\
        --kind product --desc "CL-002 rectangular matte claw clip in pink, top view" --tags abs,matte

    # approve an imported WordPress photo and give it a proper description
    python -m hermes_adapter.photo_library clawclipfactory --approve wp-123 \\
        --kind factory --desc "Injection molding machines on our production floor"

    python -m hermes_adapter.photo_library clawclipfactory --list
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

KINDS = ("product", "factory", "packaging", "material", "other")


def _paths(slug: str) -> tuple[Path, Path]:
    root = _ROOT / "projects" / slug
    return root / "photo-library.json", root / "photos"


def load(slug: str) -> dict[str, Any]:
    lib, _ = _paths(slug)
    if lib.exists():
        data = json.loads(lib.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"photos": data}
    return {"photos": []}


def save(slug: str, data: dict[str, Any]) -> Path:
    lib, _ = _paths(slug)
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lib


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "photo"


def merge_wp_media(data: dict[str, Any], media: list[dict[str, Any]]) -> int:
    """Add WP media items not already in the library (by wp_media_id). Returns count added."""
    have = {p.get("wp_media_id") for p in data["photos"]}
    added = 0
    for m in media:
        if m.get("media_type") != "image" or m.get("id") in have:
            continue
        title = re.sub(r"<[^>]+>", "", (m.get("title") or {}).get("rendered", "") if isinstance(
            m.get("title"), dict) else str(m.get("title") or ""))
        data["photos"].append({
            "id": f"wp-{m['id']}",
            "wp_media_id": m["id"],
            "url": m.get("source_url"),
            "description": (m.get("alt_text") or title or "").strip(),
            "alt": (m.get("alt_text") or "").strip(),
            "caption": "",
            "kind": "other",
            "tags": [],
            "approved": False,
        })
        added += 1
    return added


def from_wp(slug: str) -> int:
    from scripts.wordpress.wp_client import WPClient
    wp = WPClient(slug)
    media: list[dict[str, Any]] = []
    for page in range(1, 20):
        r = wp.get("/wp/v2/media", params={"media_type": "image", "per_page": 100, "page": page,
                                           "_fields": "id,media_type,source_url,alt_text,title"})
        batch = r.json_data or []
        media += batch
        if len(batch) < 100:
            break
    data = load(slug)
    n = merge_wp_media(data, media)
    save(slug, data)
    return n


def add(slug: str, file: Path, kind: str, desc: str, tags: list[str], caption: str = "") -> str:
    _, photos = _paths(slug)
    photos.mkdir(parents=True, exist_ok=True)
    dst = photos / file.name
    shutil.copy2(file, dst)
    data = load(slug)
    pid = _slug(file.stem)
    data["photos"] = [p for p in data["photos"] if p.get("id") != pid]
    data["photos"].append({"id": pid, "file": f"photos/{file.name}", "description": desc,
                           "alt": desc, "caption": caption, "kind": kind, "tags": tags,
                           "approved": True})
    save(slug, data)
    return pid


def approve(slug: str, pid: str, kind: str | None, desc: str | None, tags: list[str] | None,
            caption: str | None, value: bool = True) -> bool:
    data = load(slug)
    for p in data["photos"]:
        if p.get("id") == pid:
            p["approved"] = value
            if kind:
                p["kind"] = kind
            if desc:
                p["description"] = desc
                p["alt"] = p.get("alt") or desc
            if tags is not None:
                p["tags"] = tags
            if caption is not None:
                p["caption"] = caption
            save(slug, data)
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--from-wp", action="store_true")
    g.add_argument("--add", type=Path)
    g.add_argument("--approve")
    g.add_argument("--reject")
    g.add_argument("--list", action="store_true")
    ap.add_argument("--kind", choices=KINDS)
    ap.add_argument("--desc")
    ap.add_argument("--tags", default=None, help="comma-separated")
    ap.add_argument("--caption", default=None, help="reader-facing caption (optional)")
    a = ap.parse_args()
    tags = [t.strip() for t in a.tags.split(",") if t.strip()] if a.tags is not None else None
    if a.from_wp:
        print(json.dumps({"added": from_wp(a.project), "library": str(_paths(a.project)[0])}))
    elif a.add:
        if not (a.kind and a.desc):
            ap.error("--add needs --kind and --desc (what the photo really shows)")
        print(json.dumps({"added": add(a.project, a.add, a.kind, a.desc, tags or [], a.caption or "")}))
    elif a.approve or a.reject:
        ok = approve(a.project, a.approve or a.reject, a.kind, a.desc, tags, a.caption,
                     value=bool(a.approve))
        print(json.dumps({"ok": ok}))
        return 0 if ok else 1
    else:
        for p in load(a.project)["photos"]:
            mark = "OK " if p.get("approved") else "-- "
            print(f"{mark}{p.get('id'):<28} {p.get('kind'):<10} {(p.get('description') or '')[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
