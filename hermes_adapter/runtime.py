"""Make every child process use THIS interpreter.

The original pipeline runs its BASH stages as shell strings that start with a
bare ``python -m scripts....`` (see orchestrator.py stage executors and
run_pipeline._run_bash). Under Claude Code the operator's shell had the venv
active, so ``python`` was the venv. Started as ``.venv/bin/python -m
hermes_adapter.run_article`` (Hermes, cron) the venv is NOT activated, so a bare
``python`` resolves to the system interpreter without the dependencies — the
first BASH stage (chart-render) died with ``ModuleNotFoundError: PIL``.

Putting this interpreter's directory first on PATH makes every ``python`` /
``python3`` in those commands (and in agents' Bash calls) resolve to the same
environment the adapter itself runs in.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def pin_interpreter_on_path() -> str:
    bindir = str(Path(sys.executable).parent)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if not parts or parts[0] != bindir:
        os.environ["PATH"] = os.pathsep.join([bindir] + [p for p in parts if p and p != bindir])
    venv = Path(bindir).parent
    if (venv / "pyvenv.cfg").exists():
        os.environ["VIRTUAL_ENV"] = str(venv)
    return bindir
