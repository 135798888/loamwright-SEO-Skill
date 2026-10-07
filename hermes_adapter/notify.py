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
    lines.append(f"模型花费: ${rep.get('llm_usd')}（{rep.get('llm_calls')} 次调用）")
    if rep.get("unpriced_models"):
        lines.append(f"⚠ 未配置单价的模型: {', '.join(rep['unpriced_models'])}")
    return "\n".join(lines)
