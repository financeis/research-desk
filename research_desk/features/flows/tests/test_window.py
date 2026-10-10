"""The flows window: what it binds and what importing it loads.

``cli.py`` imports the window for every command (and for a run without one), so the window and every
module it imports at the top stay light: no web, HTTP, DB, KIS, MongoDB or AI package, and no other
feature's window (the ``reports`` / ``coverage`` windows load FastAPI; ``prices`` is looked up when a
command runs).
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import research_desk
import research_desk.features.flows as flows

REPOSITORY = Path(research_desk.__file__).resolve().parent.parent
HEAVY = ("fastapi", "starlette", "uvicorn", "httpx", "httpcore", "supabase", "postgrest", "pymongo", "bson",
         "openai", "anthropic", "asyncpg", "langgraph", "research_desk.core.kis", "research_desk.core.db",
         "research_desk.core.llm", "research_desk.core.mongo", "research_desk.web.app",
         "research_desk.features.prices", "research_desk.features.reports",
         "research_desk.features.coverage", "research_desk.features.flows.router")


def test_the_window_binds_register_jobs_and_latest_run():
    tree = ast.parse(Path(flows.__file__).read_text(encoding="utf-8"))
    bound = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            bound += [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.append(node.name)
        elif isinstance(node, (ast.Import, ast.Assign)):
            bound.append(type(node).__name__)
    assert bound == ["register_jobs", "latest_run"]
    assert callable(flows.register_jobs) and callable(flows.latest_run)


def run_python(code: str) -> str:
    result = subprocess.run([sys.executable, "-c", code], cwd=REPOSITORY, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_importing_the_window_loads_nothing_heavy():
    """Every command imports each window when it starts; the heavy parts load when a command runs."""
    assert run_python("import sys, research_desk.features.flows\n"
                      f"print(sorted(m for m in {HEAVY!r} if m in sys.modules))\n") == "[]"


def test_registering_the_command_loads_nothing_heavy_either():
    assert run_python("import argparse, sys\n"
                      "from research_desk.features.flows import register_jobs\n"
                      "parser = argparse.ArgumentParser(prog='research_desk')\n"
                      "register_jobs(parser.add_subparsers(dest='command'))\n"
                      f"print(sorted(m for m in {HEAVY!r} if m in sys.modules))\n") == "[]"
