"""CLI entry point — what Hermes (or cron, or you) calls.

    # one article
    python -m hermes_adapter.run_article --project clawclipfactory --keyword "custom claw clips"

    # several, one after another (one keyword per line; '#' comments allowed)
    python -m hermes_adapter.run_article --project clawclipfactory --keywords-file kw.txt

    # take the next keyword from a queue file (cron / Hermes schedule: 1 article per run)
    python -m hermes_adapter.run_article --project clawclipfactory --queue keywords.txt

    # resume an interrupted task (all finished stages are kept)
    python -m hermes_adapter.run_article --resume 20261007_ab12cd34

    # check config + endpoint without writing anything
    python -m hermes_adapter.run_article --check

Exit code: 0 = every article reached COMPLETE (a verified WordPress DRAFT), 3 = another run is in progress,
1 = at least one failed, 2 = configuration problem. A JSON summary is printed on
stdout (last line) so a calling agent can parse it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Run from anywhere: make the plugin root importable and the cwd.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.chdir(_ROOT)

from hermes_adapter.runtime import pin_interpreter_on_path  # noqa: E402

pin_interpreter_on_path()  # bare `python` in pipeline BASH stages → this venv

from hermes_adapter.config import ConfigError, load_config
from hermes_adapter.driver import drive_task
from hermes_adapter.llm import ChatClient
from hermes_adapter.notify import format_report, telegram
from hermes_adapter.task import create_task


def _progress(ev: dict) -> None:
    k = ev.get("kind")
    if k == "runner":
        steps = ev.get("steps") or []
        extra = f" (ran: {', '.join(steps)})" if steps else ""
        print(f"[{ev['ts'][11:19]}] runner → {ev.get('action')} {ev.get('stage') or ''}{extra}",
              file=sys.stderr, flush=True)
    elif k in ("dispatch", "dispatch_done", "repair", "repair_done", "llm_retry", "bash_retry",
               "writers_start", "finish"):
        info = {x: ev[x] for x in ev if x not in ("ts", "kind")}
        print(f"[{ev['ts'][11:19]}] {k}: {json.dumps(info, ensure_ascii=False, default=str)[:300]}",
              file=sys.stderr, flush=True)


_NOOP_TOOL = [{"type": "function", "function": {
    "name": "noop", "description": "no-op", "parameters": {"type": "object", "properties": {}}}}]


def _check(cfg) -> int:
    """Ping every distinct configured model (with a tool attached — agents need tool calling),
    and give the second-opinion model a tiny judging task to prove it returns a usable verdict."""
    client = ChatClient(cfg)
    roles = sorted(set(cfg.models) | {"default"})
    out: dict = {"endpoint": cfg.base_url, "models": {r: cfg.model_for(r) for r in roles},
                 "model_check": {}}
    for model in sorted({cfg.model_for(r) for r in roles}):
        try:
            client.chat(model=model, stage="check", max_attempts=2, tools=_NOOP_TOOL,
                        messages=[{"role": "user", "content": "Reply with the single word: pong"}])
            out["model_check"][model] = "ok"
        except Exception as e:  # noqa: BLE001
            out["model_check"][model] = f"ERROR: {e}"[:300]
    if cfg.second_opinion_mode != "off":
        from hermes_adapter.second_opinion import _SYSTEM, parse_verdict
        so = {"mode": cfg.second_opinion_mode, "model": cfg.second_opinion_model}
        try:
            res = client.chat(model=cfg.second_opinion_model, stage="check", max_attempts=2, tools=None,
                              messages=[{"role": "system", "content": _SYSTEM},
                                        {"role": "user", "content":
                                         "CRITERIA:\n1. FACTS: no invented numbers.\n\nARTICLE:\n"
                                         "## Claw clips\n\nOur factory has made claw clips since 1850 "
                                         "and ships 9 billion units a day."}])
            v = parse_verdict(res.message.get("content") or "")
            so["result"] = ("ok — returned a valid verdict" if v else
                            "ERROR: reply was not valid verdict JSON: "
                            + (res.message.get("content") or "")[:200])
            if v:
                so["sample_verdict"] = "FAIL (expected)" if not v["criteria"][0]["pass"] else \
                    "PASS — it missed obviously invented numbers; consider another model"
        except Exception as e:  # noqa: BLE001
            so["result"] = f"ERROR: {e}"[:300]
        out["second_opinion"] = so
    priced = set(cfg.prices)
    used = {cfg.model_for(r) for r in roles} | ({cfg.second_opinion_model}
                                                 if cfg.second_opinion_mode != "off" else set())
    out["unpriced"] = sorted(m for m in used if m and m not in priced)
    ok = all(v == "ok" for v in out["model_check"].values()) and \
        str(out.get("second_opinion", {}).get("result", "ok")).startswith("ok")
    out["ok"] = ok
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if ok else 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Run the loamwright article pipeline via an OpenAI-compatible LLM")
    ap.add_argument("--project", help="project slug under projects/")
    ap.add_argument("--keyword", help="primary keyword (exact SEO target — never paraphrased)")
    ap.add_argument("--secondary", default="", help="comma-separated secondary keywords")
    ap.add_argument("--keywords-file", type=Path, help="one keyword per line")
    ap.add_argument("--resume", help="existing task_id to continue")
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--word-count", type=int)
    ap.add_argument("--image-count", type=int, help="total images incl. cover (0 = text only)")
    ap.add_argument("--template", help="force a format template id (else format-selector picks)")
    ap.add_argument("--queue", type=Path,
                    help="take ONE keyword from the top of this file (for cron/Hermes schedules); "
                         "it is moved to <file>.done with the result")
    ap.add_argument("--check", action="store_true", help="validate llm.yaml + endpoint and exit")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    if args.check:
        return _check(cfg)

    # One adapter run at a time per server: a cron tick that fires while yesterday's
    # article is still running must not start a second, parallel spend.
    from scripts._core import file_lock
    try:
        with file_lock.locked(_ROOT / "memory" / ".hermes-run", timeout=1.0):
            return _run(args, cfg, ap)
    except file_lock.LockTimeout:
        print(json.dumps({"ok": False, "error": "another hermes_adapter run is in progress"}))
        return 3


def _pop_queue(path: Path) -> str | None:
    if not path.exists():
        return None
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        kw = line.strip()
        if kw and not kw.startswith("#"):
            rest = lines[:i] + lines[i + 1:]
            path.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
            return kw
    return None


def _run(args, cfg, ap) -> int:
    task_ids: list[str] = []
    queue_kw: str | None = None
    if args.queue:
        queue_kw = _pop_queue(args.queue)
        if not queue_kw:
            print(json.dumps({"ok": True, "articles": [], "note": f"queue {args.queue} is empty"}))
            return 0
        args.keyword = queue_kw
    if args.resume:
        task_ids.append(args.resume)
    else:
        if not args.project:
            ap.error("--project is required (unless --resume/--check)")
        keywords: list[str] = []
        if args.keyword:
            keywords.append(args.keyword)
        if args.keywords_file:
            for line in args.keywords_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    keywords.append(line)
        if not keywords:
            ap.error("give --keyword or --keywords-file")
        os.environ["XS_ACTIVE_PROJECT"] = args.project  # Rule 7: pin project identity
        for kw in keywords:
            task_ids.append(create_task(
                project_slug=args.project, keyword=kw,
                secondary=[s for s in args.secondary.split(",") if s.strip()],
                locale=args.locale, word_count=args.word_count,
                image_count=args.image_count, template_id=args.template))

    reports = []
    for tid in task_ids:
        print(f"=== task {tid} ===", file=sys.stderr, flush=True)
        state = json.loads((_ROOT / "memory" / "workspace" / tid / "state.json").read_text("utf-8"))
        if state.get("project_slug"):
            os.environ["XS_ACTIVE_PROJECT"] = state["project_slug"]
        rep = drive_task(cfg, tid, on_event=_progress).as_dict()
        reports.append(rep)
        if not args.no_notify:
            telegram(cfg, format_report(rep))
        if args.queue and queue_kw:
            with Path(str(args.queue) + ".done").open("a", encoding="utf-8") as f:
                f.write(f"{rep['finished_at'][:19]}\t{rep['status']}\t{tid}\t{queue_kw}\n")
        if rep["status"] == "budget_exceeded":
            break  # don't start the next article over budget

    summary = {"ok": all(r["status"] == "complete" for r in reports), "articles": reports}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
