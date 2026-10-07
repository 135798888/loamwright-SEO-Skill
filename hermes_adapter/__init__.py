"""hermes_adapter — run the loamwright article pipeline WITHOUT Claude Code.

The plugin's quality machinery (orchestrator state machine, run_pipeline driver,
lint/quality gates, provenance checks) is plain Python and host-agnostic. The only
Claude-Code-specific parts are:

  1. dispatching LLM stages to *isolated* subagents with a tool whitelist
     (agents/*.md ``tools:`` frontmatter), and
  2. the hooks (schema validation on Write/Edit, cost guard before Bash).

This package re-implements exactly those two parts on top of any
OpenAI-compatible chat-completions endpoint (official API or a relay/中转站),
so Hermes — or cron, or a shell — can drive a full article end to end:

    python -m hermes_adapter.run_article --project clawclipfactory --keyword "custom claw clips"

Everything else (stage order, gates, publishing) is still done by the original
scripts; this package never re-implements a gate.
"""

__all__ = ["agent", "config", "driver", "llm", "tools"]
