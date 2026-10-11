"""Telegram notification (optional). Silent no-op when no bot token / chat id is set."""
from __future__ import annotations

import httpx

from hermes_adapter.config import LLMConfig


def telegram(cfg: LLMConfig, text: str) -> bool:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=20,
        )
        return r.status_code == 200
    except httpx.HTTPError:
        return False


def format_report(rep: dict) -> str:
    icon = {"complete": "✅", "failed": "❌", "budget_exceeded": "💸"}.get(rep.get("status"), "ℹ️")
    lines = [
        f"{icon} SEO 文章 [{rep.get('status')}]",
        f"关键词: {rep.get('keyword')}",
        f"项目: {rep.get('project_slug')}   任务: {rep.get('task_id')}",
    ]
    if rep.get("preview_url"):
        lines.append(f"草稿预览: {rep['preview_url']}")
    if rep.get("post_id"):
        lines.append(f"WordPress 文章 ID: {rep['post_id']}（草稿，需人工确认发布）")
    if rep.get("status") != "complete":
        lines.append(f"卡在: {rep.get('stage')}\n原因: {str(rep.get('detail'))[:1200]}")
    so = rep.get("second_opinion")
    if so:
        label = {"PASS": "通过", "FAIL": "未通过", "ERROR": "未能给出结论"}.get(so, so)
        extra = f"（未通过: {', '.join(rep.get('second_opinion_failed') or [])}）" \
            if rep.get("second_opinion_failed") else ""
        lines.append(f"Gemini 第二意见: {label}{extra} — 详见 second-opinion.json")
    lines.append(f"模型花费: ${rep.get('llm_usd')}（{rep.get('llm_calls')} 次调用）")
    if rep.get("unpriced_models"):
        lines.append(f"⚠ 未配置单价的模型: {', '.join(rep['unpriced_models'])}")
    return "\n".join(lines)


# Stages worth a progress message (Chinese labels for the owner's Telegram).
MILESTONES = {
    "research": "调研", "outline-architect": "大纲", "section-drafter": "写作",
    "fact-check-and-citation": "事实核查", "independent-reviewer": "审稿",
    "image-visual-qa": "图片质检", "pre-publish-gate": "发布前检查",
    "wordpress-publisher": "创建 WordPress 草稿",
}


class ProgressNotifier:
    """Sends one Telegram line the first time each milestone stage is reached.

    A background run used to be silent from start to finish, so the owner could not tell
    a long stage from a dead process (2026-10-11)."""

    def __init__(self, cfg: LLMConfig, keyword: str, task_id: str):
        self.cfg, self.keyword, self.task_id = cfg, keyword, task_id
        self.seen: set[str] = set()

    def on_event(self, ev: dict) -> bool:
        if ev.get("kind") != "runner":
            return False
        stages = [ev.get("stage")] + list(ev.get("steps") or [])
        sent = False
        for st in stages:
            if st in MILESTONES and st not in self.seen:
                self.seen.add(st)
                sent = telegram(self.cfg, f"⏳ {self.keyword}：进入「{MILESTONES[st]}」（{self.task_id}）") or sent
        return sent
