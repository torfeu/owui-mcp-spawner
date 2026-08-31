import inspect
import json
import os
from pathlib import Path
from typing import Any, Optional

from .logger import get_manager_logger
from .schema_gen import build_schema_from_method

logger = get_manager_logger()

_EXCLUDED = {"__init__", "valves", "user_valves"}


def _public_methods(ToolsClass) -> set[str]:
    """Names of public tool methods on an already-exec'd Tools class."""
    if ToolsClass is None:
        return set()
    return {
        name
        for name, member in inspect.getmembers(ToolsClass, predicate=inspect.isfunction)
        if not name.startswith("_") and name not in _EXCLUDED
    }


class OpenWebUITool:
    def __init__(self, raw: dict):
        self.content: str = raw.get("content", "")
        self.specs: list[dict] = raw.get("specs", [])

    def get_mcp_tool_defs(self) -> list[dict]:
        """Return MCP tool definitions built from the live Python code."""
        # Exec once to get the real class and method objects — the method names
        # come from the same namespace, so the tool's import-time side effects
        # (model loads, connection pools) run once per start, not twice.
        exec_ns: dict = {}
        ToolsClass = None
        if self.content:
            try:
                exec(self.content, exec_ns)  # noqa: S102
                ToolsClass = exec_ns.get("Tools")
            except Exception as e:
                logger.warning(f"Could not exec tool code for schema generation: {e}")

        class_methods = _public_methods(ToolsClass)
        specs_by_name = {s["name"]: s for s in self.specs}

        result = []
        for name in sorted(class_methods):
            spec = specs_by_name.get(name)
            description = spec.get("description", "") if spec else ""

            input_schema: dict
            if ToolsClass is not None:
                method = getattr(ToolsClass, name, None)
                if method is not None:
                    input_schema = build_schema_from_method(method, exec_ns)
                else:
                    input_schema = (
                        spec.get("parameters", {"type": "object", "properties": {}})
                        if spec else {"type": "object", "properties": {}}
                    )
            else:
                input_schema = (
                    spec.get("parameters", {"type": "object", "properties": {}})
                    if spec else {"type": "object", "properties": {}}
                )

            result.append({
                "name": name,
                "description": description,
                "inputSchema": input_schema,
            })
        return result


def load_openwebui_json(path: Path) -> Optional[OpenWebUITool]:
    """Load an OpenWebUI tool export JSON (array or single object)."""
    if not path.exists():
        logger.error(f"Tool file not found: {path}")
        return None
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, list):
            raw = raw[0]
        return OpenWebUITool(raw)
    except Exception as e:
        logger.error(f"Failed to load tool file {path}: {e}")
        return None


# Valve names that mean "put the files you produce here". Two fixed names plus
# a suffix, because the suffix is where the tool-specific ones live:
# docx_export_dir, pdf_export_dir, image_export_dir — all the same question.
_CONTENT_VALVES = {"content_dir", "output_dir"}


# The other valve the manager can answer better than the user can. A tool that
# talks back to the spawner (the router, the control tool) ships a default of
# http://127.0.0.1:7860, and on an installation that runs the manager anywhere
# else that default produces "All connection attempts failed" — true, and
# nothing anybody can act on. The manager knows its own address; the runner
# inherits it.
_MANAGER_VALVE = "manager_url"


def manager_url() -> str:
    """Where the manager answers, as seen from a runner on the same machine.

    `MCP_RUNNER_HOST` is a *bind* address, and 0.0.0.0 is not something you can
    connect to — the same substitution the health check's probe URL makes.
    """
    host = os.environ.get("MCP_RUNNER_HOST") or "127.0.0.1"
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = os.environ.get("MCP_MANAGER_PORT") or "7860"
    return f"http://{host}:{port}"


def is_content_valve(name: str) -> bool:
    return name in _CONTENT_VALVES or name.endswith("_export_dir")


def content_valve_names(instance: Any) -> list[str]:
    """Which valves of *instance* ask for an output directory."""
    valves = getattr(instance, "valves", None)
    if valves is None:
        return []
    try:
        fields = list(type(valves).model_fields)
    except Exception:
        fields = [k for k in vars(valves) if not k.startswith("_")]
    return sorted(f for f in fields if is_content_valve(f))


def create_tools_instance(tool: OpenWebUITool, values: dict[str, Any],
                          content_dir: Optional[str] = None) -> Any:
    """
    Exec the tool code, instantiate Tools, and inject config values into Valves.
    Returns the Tools instance or None on failure.

    When *content_dir* is given (the instance has the content store switched
    on), any valve that asks for an output directory is filled with it — unless
    the user set that valve themselves, which always wins. Without this a tool
    written for OpenWebUI writes to its built-in default, which in the runner is
    a path that does not exist.
    """
    if not tool.content:
        return None
    try:
        ns: dict = {}
        exec(tool.content, ns)  # noqa: S102
        ToolsClass = ns.get("Tools")
        if ToolsClass is None:
            return None
        instance = ToolsClass()
        if hasattr(instance, "valves") and values:
            for k, v in values.items():
                if hasattr(instance.valves, k):
                    try:
                        setattr(instance.valves, k, v)
                    except Exception:
                        pass
        if content_dir:
            for name in content_valve_names(instance):
                if str(values.get(name, "")).strip():
                    continue  # the user pointed this one somewhere on purpose
                try:
                    setattr(instance.valves, name, content_dir)
                    logger.info(f"Valve '{name}' set to the content folder: {content_dir}")
                except Exception:
                    pass
        # Same rule, not a second one: filled from what the manager knows, and
        # a value the user set themselves always wins.
        if hasattr(getattr(instance, "valves", None), _MANAGER_VALVE) \
                and not str(values.get(_MANAGER_VALVE, "")).strip():
            try:
                setattr(instance.valves, _MANAGER_VALVE, manager_url())
                logger.info(f"Valve '{_MANAGER_VALVE}' set to {manager_url()}")
            except Exception:
                pass
        return instance
    except Exception as e:
        logger.error(f"Failed to create Tools instance: {e}")
        return None
