"""
Tool editor backend: validation and OpenWebUI-compatible JSON export.

validate_tool_code() runs the untrusted code in a short-lived subprocess
(app/validate_worker.py) so import-time side effects, crashes or hangs
cannot affect the manager process. The in-process implementation
(_validate_in_process) is only executed inside that worker.
"""
import ast
import inspect
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .schema_gen import build_schema_from_method

BASE_DIR = Path(__file__).parent.parent

VALIDATE_TIMEOUT = 20  # seconds for the validation subprocess

# Matches a `requirements:` frontmatter line in the tool's leading docstring.
_REQUIREMENTS_RE = re.compile(r"^\s*requirements:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def parse_requirements(code: str) -> list[str]:
    """Extract pip dependencies from a `requirements:` frontmatter line.

    OpenWebUI tools declare deps in the module docstring, e.g.
    `requirements: yfinance, pandas, numpy`. Returns [] if absent. Package specs
    are validated later by install_dependencies, so this only splits the line.
    """
    if not code:
        return []
    m = _REQUIREMENTS_RE.search(code[:2000])  # only look at the frontmatter region
    if not m:
        return []
    parts = [p.strip() for p in m.group(1).replace(";", ",").split(",")]
    return [p for p in parts if p]


STARTER_TEMPLATE = '''\
"""
title: My Tool
description: A short description of what this tool does
author:
version: 0.1.0
"""
import requests
from pydantic import BaseModel, Field
from typing import Optional


class Tools:
    class Valves(BaseModel):
        api_url: str = Field(default="https://api.example.com", description="Base API URL")
        api_key: str = Field(default="", description="API key for authentication")

    def __init__(self):
        self.valves = self.Valves()

    def get_data(self, query: str) -> str:
        """Fetch data based on a query.

        Args:
            query: The search term to look up

        Returns:
            Result as a formatted string
        """
        # TODO: implement your tool logic here
        return f"Result for: {query}"
'''


def _invalid(errors: list[str], warnings: list[str] | None = None) -> dict:
    return {"valid": False, "errors": errors, "warnings": warnings or [], "tools": [], "valves": {}}


def validate_tool_code(code: str, python_exe: str | None = None) -> dict:
    """Validate Python tool code in an isolated subprocess.

    Returns a dict with: valid, errors, warnings, tools (specs), valves.

    *python_exe* selects the interpreter that runs the validation worker — pass
    an instance's venv Python so import-time checks see the tool's installed
    dependencies. Defaults to the manager interpreter (sys.executable).
    """
    # Cheap syntax check first — no subprocess needed for broken code
    try:
        ast.parse(code)
    except SyntaxError as e:
        return _invalid([f"SyntaxError line {e.lineno}: {e.msg}"])

    try:
        result = subprocess.run(
            [str(python_exe or sys.executable), "-m", "app.validate_worker"],
            input=code,
            capture_output=True,
            text=True,
            timeout=VALIDATE_TIMEOUT,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        return _invalid([f"Validation timed out after {VALIDATE_TIMEOUT}s — "
                         "does the code block or wait at import time?"])
    except Exception as e:
        return _invalid([f"Validation subprocess failed: {e}"])

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return _invalid([f"Validation subprocess crashed: {detail[-500:]}"])

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return _invalid([f"Validation produced unreadable output: {result.stdout[:200]}"])


def _validate_in_process(code: str) -> dict:
    """Actual validation logic — only run inside the validate_worker subprocess."""
    errors: list[str] = []
    warnings: list[str] = []
    tools: list[dict] = []
    valves: dict = {}

    # 1. Syntax check — safe, no execution
    try:
        ast.parse(code)
    except SyntaxError as e:
        return _invalid([f"SyntaxError line {e.lineno}: {e.msg}"])

    # 2. Runtime check + class introspection
    try:
        ns: dict = {}
        exec(code, ns)  # noqa: S102
    except Exception as e:
        return _invalid([f"Runtime error: {type(e).__name__}: {e}"])

    ToolsClass = ns.get("Tools")
    if ToolsClass is None:
        return _invalid(["No 'Tools' class found — OpenWebUI requires a class named 'Tools'"])

    # 3. Valves defaults
    try:
        if hasattr(ToolsClass, "Valves"):
            valves = ToolsClass.Valves().model_dump()
    except Exception as e:
        warnings.append(f"Could not instantiate Valves: {e}")

    # 4. Method introspection → specs (same schema builder as the MCP runner)
    excluded = {"__init__", "valves", "user_valves"}
    for name, method in inspect.getmembers(ToolsClass, predicate=inspect.isfunction):
        if name.startswith("_") or name in excluded:
            continue

        sig = inspect.signature(method)
        doc = inspect.getdoc(method) or ""

        # Full docstring up to the Args/Returns section becomes the tool description,
        # so multi-line usage guidance actually reaches the LLM via MCP.
        desc_lines: list[str] = []
        for line in doc.splitlines():
            if line.strip().rstrip(":").lower() in ("args", "arguments", "parameters", "returns", "raises"):
                break
            desc_lines.append(line)
        description = "\n".join(desc_lines).strip()

        if not doc:
            warnings.append(f"'{name}': no docstring — description will be empty in OpenWebUI")

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue
            if param.annotation is inspect.Parameter.empty:
                warnings.append(f"'{name}.{param_name}': no type hint, defaulting to string")

        tools.append({
            "name": name,
            "description": description,
            "parameters": build_schema_from_method(method, ns),
        })

    if not tools:
        errors.append("Tools class has no public methods — at least one tool function is required")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "tools": tools,
        "valves": valves,
    }


def generate_openwebui_json(
    code: str, tool_id: str, name: str, description: str,
    validation: dict | None = None,
) -> list[dict]:
    """Generate an OpenWebUI-compatible tool export JSON array.

    Pass an existing validate_tool_code() result as *validation* to avoid
    validating twice. Raises ValueError if the code is invalid so callers
    can return a proper error.
    """
    result = validation if validation is not None else validate_tool_code(code)
    if not result["valid"]:
        raise ValueError(f"Invalid tool code: {'; '.join(result['errors'])}")
    now = int(time.time())
    return [{
        "id": tool_id,
        "user_id": "",
        "name": name,
        "content": code,
        "specs": result["tools"],
        "meta": {
            "description": description,
            "manifest": {},
        },
        "is_active": True,
        "is_global": False,
        "updated_at": now,
        "created_at": now,
    }]
