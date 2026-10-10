"""The flows window: what it binds and what importing it loads.

``cli.py`` imports the window for every command (and for a run without one), so the window and every
module it imports at the top stay light: no web, HTTP, DB, KIS, MongoDB or AI package, and no other
feature's window (the ``reports`` / ``coverage`` windows load FastAPI; ``prices`` is looked up when a
command runs). The web addresses come through ``web_router()``, which imports the router (and with
it FastAPI) only when it is called.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from fastapi.routing import APIRoute

import research_desk
import research_desk.features.flows as flows

REPOSITORY = Path(research_desk.__file__).resolve().parent.parent
HEAVY = ("fastapi", "starlette", "uvicorn", "httpx", "httpcore", "supabase", "postgrest", "pymongo", "bson",
         "openai", "anthropic", "asyncpg", "langgraph", "research_desk.core.kis", "research_desk.core.db",
         "research_desk.core.llm", "research_desk.core.mongo", "research_desk.web.app",
         "research_desk.features.prices", "research_desk.features.reports",
         "research_desk.features.coverage", "research_desk.features.flows.router")


def test_the_window_binds_register_jobs_latest_run_and_web_router():
    tree = ast.parse(Path(flows.__file__).read_text(encoding="utf-8"))
    bound = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            bound += [alias.asname or alias.name for alias in node.names]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.append(node.name)
        elif isinstance(node, (ast.Import, ast.Assign)):
            bound.append(type(node).__name__)
    assert bound == ["register_jobs", "latest_run", "web_router"]
    assert callable(flows.register_jobs) and callable(flows.latest_run) and callable(flows.web_router)


def test_web_router_imports_the_router_only_when_called():
    tree = ast.parse(Path(flows.__file__).read_text(encoding="utf-8"))
    [function] = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "web_router"]
    imports = [node for node in ast.walk(function) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert [(node.level, node.module, [a.name for a in node.names]) for node in imports] == [(1, "router", ["router"])]


def test_web_router_gives_the_two_addresses():
    router = flows.web_router()
    routes = sorted((route.path, tuple(sorted(route.methods)), route.name)
                    for route in router.routes if isinstance(route, APIRoute))
    assert routes == [("/api/flows", ("GET",), "flows_ranking"),
                      ("/api/stocks/{code}/flows", ("GET",), "stock_flows")]
    assert flows.web_router() is router


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


def test_calling_web_router_is_what_loads_fastapi_and_the_router():
    # The coverage window and the DB client still load only when a web answer needs them.
    loaded = ("fastapi", "research_desk.features.flows.router", "research_desk.features.coverage",
              "research_desk.core.db")
    assert run_python("import sys\n"
                      "from research_desk.features.flows import web_router\n"
                      "before = 'fastapi' in sys.modules\n"
                      "web_router()\n"
                      f"print(before, *(m in sys.modules for m in {loaded!r}))\n") == "False True True False False"
