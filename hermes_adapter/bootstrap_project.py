"""Create (or update) projects/{slug}/ from a filled-in template folder.

Replaces the interactive /init wizard for adapter users. The wizard crawls the
site and asks questions; for a single site you know well it is faster and more
accurate to fill in the template yourself — and, crucially, every company fact
the writers may state comes from YOU, not from a crawl.

    python -m hermes_adapter.bootstrap_project \\
        --from hermes_adapter/templates/clawclipfactory --check-wp

Template folder contents:
    business-context.json   (required)  → projects/{slug}/business-context.json
    brand-config.json       (optional)  → projects/{slug}/brand/brand-config.json  (+ article CSS)
    brand-guideline.yaml    (optional)  → projects/{slug}/brand-guideline.yaml

Any string value starting with "TODO" is a fact you have not filled in yet. The
script refuses to install a template that still has TODOs (so no placeholder can
reach a writer); pass --allow-todo to install anyway with those fields REMOVED.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def find_todos(obj: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += find_todos(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += find_todos(v, f"{path}[{i}]")
    elif isinstance(obj, str) and obj.strip().upper().startswith("TODO"):
        out.append(path)
    return out


def strip_todos(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: strip_todos(v) for k, v in obj.items()
                if not (isinstance(v, str) and v.strip().upper().startswith("TODO"))}
    if isinstance(obj, list):
        return [strip_todos(v) for v in obj
                if not (isinstance(v, str) and v.strip().upper().startswith("TODO"))]
    return obj


def validate_business_context(bc: dict[str, Any]) -> list[str]:
    import jsonschema
    schema = json.loads((_ROOT / "schemas" / "business-context.schema.json").read_text("utf-8"))
    v = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
            for e in v.iter_errors(bc)]


def _project_claude_md(bc: dict[str, Any]) -> str:
    sig = bc.get("article_signature") or {}
    company = bc.get("company") or {}
    return f"""# Project: {bc['site_slug']} ({bc['site_url']})

Created by hermes_adapter.bootstrap_project. Edit business-context.json, not this file, for facts.

- Business: {company.get('legal_name', '')} — B2B manufacturer; readers are wholesale buyers,
  brand owners and importers (North America, Europe, Australia). Write for procurement, not consumers.
- SEO plugin: SEOPress (meta written through SEOPress's own REST API — scripts/wordpress/seopress_api.py).
- Publish policy: DRAFT only. A human reviews and publishes.
- references_required: true
- Article signature author: {sig.get('author', 'our team')}; contact: {sig.get('contact_url', '')}
- Company self-facts (MOQ, lead times, capacity, materials, certifications, years operating) may ONLY
  be taken from business-context.json :: company. Never estimate or invent them.
- Never cite or link competitor domains listed in citation_source_policy.do_not_cite_domains.
"""


def install(template: Path, *, allow_todo: bool, force: bool) -> tuple[str, list[str]]:
    from scripts._core.project_paths import ensure_project_tree

    bc_src = template / "business-context.json"
    if not bc_src.exists():
        raise SystemExit(f"{bc_src} not found")
    bc = json.loads(bc_src.read_text(encoding="utf-8"))
    notes: list[str] = []

    todos = find_todos(bc)
    brand_cfg = None
    if (template / "brand-config.json").exists():
        brand_cfg = json.loads((template / "brand-config.json").read_text(encoding="utf-8"))
        todos += [f"brand-config.{t}" for t in find_todos(brand_cfg)]
    if todos and not allow_todo:
        raise SystemExit("Fill in these TODO fields first (or pass --allow-todo to drop them):\n  - "
                         + "\n  - ".join(todos))
    if todos:
        notes.append(f"dropped {len(todos)} unfilled TODO field(s): {', '.join(todos)}")
        bc = strip_todos(bc)
        brand_cfg = strip_todos(brand_cfg) if brand_cfg else None

    errors = validate_business_context(bc)
    if errors:
        raise SystemExit("business-context.json fails schemas/business-context.schema.json:\n  - "
                         + "\n  - ".join(errors))

    slug = bc["site_slug"]
    root = ensure_project_tree(slug)
    dst = root / "business-context.json"
    if dst.exists() and not force:
        backup = dst.with_suffix(".json.bak")
        shutil.copy2(dst, backup)
        notes.append(f"previous business-context.json backed up to {backup.name}")
    dst.write_text(json.dumps(bc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if brand_cfg:
        (root / "brand" / "brand-config.json").write_text(
            json.dumps(brand_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        p = subprocess.run([sys.executable, "-m", "scripts.build.article_css_generator", slug, "--json"],
                           cwd=str(_ROOT), capture_output=True, text=True)
        notes.append("article CSS generated" if p.returncode == 0
                     else f"article CSS generation FAILED: {(p.stderr or p.stdout)[-300:]}")
    if (template / "brand-guideline.yaml").exists():
        shutil.copy2(template / "brand-guideline.yaml", root / "brand-guideline.yaml")
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists() or force:
        claude_md.write_text(_project_claude_md(bc), encoding="utf-8")
    links = root / "internal-links-map.md"
    if not links.exists():
        links.write_text("# Internal links map\n\n(populated from the live site by the publisher)\n",
                         encoding="utf-8")
    return slug, notes


def check_wp(slug: str) -> dict[str, Any]:
    """Credentials work + SEOPress's own REST API is present (no MU-plugin needed)."""
    from scripts.wordpress.wp_client import WPClient
    try:
        wp = WPClient(slug)
        h = wp.health_check()
        me = wp.get("/wp/v2/users/me", params={"context": "edit"}).json_data or {}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    roles = me.get("roles") or []
    can_edit = bool(set(roles) & {"administrator", "editor"})
    ok = bool(h.get("wp_rest")) and h.get("seo_plugin") == "seopress" and can_edit
    hint = None
    if h.get("seo_plugin") != "seopress":
        hint = f"SEOPress REST API not detected (seo_plugin={h.get('seo_plugin')!r}) — is SEOPress active?"
    elif not can_edit:
        hint = f"WordPress user roles {roles} — the application-password user should be Editor or Administrator"
    return {"ok": ok, "wp_rest": h.get("wp_rest"), "seo_plugin": h.get("seo_plugin"),
            "user": me.get("slug"), "roles": roles, "hint": hint or h.get("info", {}).get("wp_error")}


def main() -> int:
    ap = argparse.ArgumentParser(description="Install a project from a template folder")
    ap.add_argument("--from", dest="template", type=Path, required=True)
    ap.add_argument("--allow-todo", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite without .bak and regenerate CLAUDE.md")
    ap.add_argument("--check-wp", action="store_true", help="test WordPress credentials + SEOPress API")
    args = ap.parse_args()
    os.chdir(_ROOT)
    from hermes_adapter.runtime import pin_interpreter_on_path
    pin_interpreter_on_path()
    slug, notes = install(args.template.resolve(), allow_todo=args.allow_todo, force=args.force)
    out: dict[str, Any] = {"ok": True, "project": slug, "path": f"projects/{slug}", "notes": notes}
    if args.check_wp:
        out["wordpress"] = check_wp(slug)
        out["ok"] = out["wordpress"]["ok"]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
