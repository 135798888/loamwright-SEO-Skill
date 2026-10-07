---
name: seo-article
description: Write and upload an SEO blog article (as a WordPress DRAFT) for clawclipfactory.com using the loamwright pipeline on this server. Use when the user asks to write/produce an SEO article or blog post for a keyword, to process the keyword queue, to check on a running article, or to resume a failed one.
---

# SEO article pipeline (loamwright + hermes_adapter)

The pipeline lives in a git checkout on this server. Default location:
`~/loamwright-seo-skill` (if it is elsewhere, use that path everywhere below).
It is a long-running, multi-stage job (typically 30–90 minutes per article). You do NOT write
the article yourself — you start the job, monitor it, and report the result.

## Rules

- The keyword the user gives is the exact SEO target. Pass it verbatim; never "improve" it.
- Articles are only ever created as WordPress **drafts**. Never publish live; a human reviews
  the draft in WordPress and publishes it.
- Run ONE article job at a time. If a job is already running (see Monitor), say so and wait.
- Never edit files under the checkout's `scripts/`, `agents/`, `skills/` to "fix" a failure.
  Report the failure instead.

## Start one article

```bash
cd ~/loamwright-seo-skill && mkdir -p logs && \
nohup python -m hermes_adapter.run_article --project clawclipfactory \
  --keyword "KEYWORD HERE" > logs/article-$(date +%Y%m%d-%H%M%S).log 2>&1 &
echo started
```

Optional flags: `--secondary "kw2,kw3"`, `--image-count 3` (0 = text only),
`--word-count 2500`.

## Process the next keyword from the queue

```bash
cd ~/loamwright-seo-skill && mkdir -p logs && \
nohup python -m hermes_adapter.run_article --project clawclipfactory \
  --queue keywords.txt > logs/queue-$(date +%Y%m%d-%H%M%S).log 2>&1 &
```

To add keywords to the queue, append one per line to `~/loamwright-seo-skill/keywords.txt`.
Finished keywords are moved to `keywords.txt.done` with their status and task id.

## Monitor

```bash
cd ~/loamwright-seo-skill && ls -t logs | head -3 && tail -n 15 logs/$(ls -t logs | head -1)
```

Progress lines look like `runner → DISPATCH_LLM section-drafter`. The job is finished when the
log's LAST line is a JSON object starting with `{"ok":`. Check every ~10 minutes; don't poll faster.

## Report the result

Parse the final JSON line. For each entry in `articles`:
- `status: complete` → tell the user the draft is ready: `preview_url`, `post_id`, `llm_usd`.
  Remind them to review and publish it in WordPress.
- `status: failed` → report `stage` and `detail` in plain words, and the `task_id`.
- `status: budget_exceeded` → report the spend and that the per-article limit stopped it.

## Resume a failed article

After the cause is fixed (e.g. an API key topped up), finished stages are kept:

```bash
cd ~/loamwright-seo-skill && nohup python -m hermes_adapter.run_article \
  --resume TASK_ID > logs/resume-$(date +%Y%m%d-%H%M%S).log 2>&1 &
```

## Health check

```bash
cd ~/loamwright-seo-skill && python -m hermes_adapter.run_article --check
```
