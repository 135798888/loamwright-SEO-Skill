"""Pipeline BASH stages run a bare `python …`; it must resolve to the adapter's own venv.

Reproduces the first live-run failure: started as `.venv/bin/python -m
hermes_adapter.run_article` (no activated venv), chart-render's `python -m
scripts.build.render_data_charts` hit the system interpreter → No module named 'PIL'.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_PROBE = """
import sys
sys.path.insert(0, {root!r})
{pin}
from scripts.pipeline import run_pipeline
p = run_pipeline._run_bash('python -c "import sys; print(sys.executable)"')
print(p.stdout.strip())
"""


def _run(tmp_path: Path, pin: bool) -> str:
    fake_bin = tmp_path / "systembin"
    fake_bin.mkdir()
    shim = fake_bin / "python"
    shim.write_text("#!/bin/sh\necho SYSTEM-PYTHON-WITHOUT-DEPS\n")
    shim.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    code = _PROBE.format(root=str(ROOT), pin=(
        "from hermes_adapter.runtime import pin_interpreter_on_path; pin_interpreter_on_path()"
        if pin else ""))
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                         capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1]


def test_without_pin_bare_python_hits_the_wrong_interpreter(tmp_path):
    assert _run(tmp_path, pin=False) == "SYSTEM-PYTHON-WITHOUT-DEPS"


def test_with_pin_bare_python_is_the_adapter_interpreter(tmp_path):
    got = _run(tmp_path, pin=True)
    assert Path(got).parent.resolve() == Path(sys.executable).parent.resolve()


def test_run_article_pins_on_import():
    src = (ROOT / "hermes_adapter" / "run_article.py").read_text(encoding="utf-8")
    pin_at = src.index("pin_interpreter_on_path()")
    assert pin_at < src.index("def main(")
