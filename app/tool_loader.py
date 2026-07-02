import inspect
import json
from pathlib import Path
from typing import Any, Optional

from .logger import get_manager_logger
from .schema_gen import build_schema_from_method

logger = get_manager_logger()

_EXCLUDED = {"__init__", "valves", "user_valves"}


def _get_tools_class_methods(content: str) -> set[str]:
    """Return names of public methods defined directly in the Tools class."""
    try:
        ns: dict = {}
        exec(content, ns)  # noqa: S102
        ToolsClass = ns.get("Tools")
        if ToolsClass is None:
            return set()
        return {
            name
            for name, member in inspect.getmembers(ToolsClass, predicate=inspect.isfunction)
            if not name.startswith("_") and name not in _EXCLUDED
        }
    except Exception:
        return set()


class OpenWebUITool:
    def __init__(self, raw: dict):
        self.content: str = raw.get("content", "")
        self.specs: list[dict] = raw.get("specs", [])

    def get_mcp_tool_defs(self) -> list[dict]:
        """Return MCP tool definitions built from the live Python code."""
        # Exec once to get the real class and method objects
        exec_ns: dict = {}
        ToolsClass = None
        if self.content:
            try:
                exec(self.content, exec_ns)  # noqa: S102
                ToolsClass = exec_ns.get("Tools")
            except Exception as e:
                logger.warning(f"Could not exec tool code for schema generation: {e}")

        class_methods = _get_tools_class_methods(self.content)
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


def create_tools_instance(tool: OpenWebUITool, values: dict[str, Any]) -> Any:
    """
    Exec the tool code, instantiate Tools, and inject config values into Valves.
    Returns the Tools instance or None on failure.
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
        return instance
    except Exception as e:
        logger.error(f"Failed to create Tools instance: {e}")
        return None
