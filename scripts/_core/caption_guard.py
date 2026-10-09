"""Keep image-generation prompt text out of reader-facing captions.

Post 289 (clawclipfactory, 2026-10-08) shipped "Realistic photograph, natural soft
daylight, neutral off-white background, ... no text, no logos" as a visible figcaption:
the image-prompt designer had copied the art-direction prefix into `caption`. A caption
is a sentence for the reader; anything that reads like a prompt is dropped (an empty
caption renders no <figcaption>, which is always better than prompt text).
"""
from __future__ import annotations

import re

_PROMPT_MARKERS = re.compile(
    r"\b(no text|no logos?|no watermark|photorealistic|realistic photograph|soft daylight|"
    r"natural (soft )?(day)?light|sharp focus|shallow depth of field|bokeh|studio lighting|"
    r"off-white background|neutral background|negative prompt|avoid:|8k|4k|ultra[- ]detailed|"
    r"high resolution|octane|unreal engine|--ar\b|art direction|true-to-life)\b",
    re.IGNORECASE)


def looks_like_prompt(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if len(_PROMPT_MARKERS.findall(t)) >= 1:
        return True
    # Prompt style: a run of comma-separated noun phrases with no sentence punctuation.
    return t.count(",") >= 4 and not re.search(r"[.!?]\s", t)


def reader_caption(text: str) -> str:
    """The caption to show readers: the original, or "" when it is prompt text."""
    return "" if looks_like_prompt(text) else (text or "").strip()
