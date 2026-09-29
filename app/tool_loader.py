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
        self._ns: Optional[dict] = None
        self._exec_error: Optional[Exception] = None

    def namespace(self) -> dict:
        """The module namespace of the tool code, executed once per object.

        The runner needs the code twice — once to build the Tools instance,
        once for the schemas — and used to exec it twice, so a tool's
        import-time side effects (model loads, connection pools, a log line)
        ran twice per start while the comment here promised once. Both now
        share this. Raises what the exec raised, every time it is asked.
        """
        if self._exec_error is not None:
            raise self._exec_error
        if self._ns is None:
            ns: dict = {}
            try:
                exec(self.content, ns)  # noqa: S102
            except Exception as e:
                self._exec_error = e
                raise
            self._ns = ns
        return self._ns

    def get_mcp_tool_defs(self) -> list[dict]:
        """Return MCP tool definitions built from the live Python code."""
        exec_ns: dict = {}
        ToolsClass = None
        if self.content:
            try:
                exec_ns = self.namespace()
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
    # "http://::1:7860" is not a URL — the port would be read into the address.
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


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


def _valve_defaults(instance: Any) -> dict[str, Any]:
    """The valve values of a freshly constructed Tools instance."""
    valves = getattr(instance, "valves", None)
    if valves is None:
        return {}
    try:
        return valves.model_dump()
    except Exception:
        return {k: v for k, v in vars(valves).items() if not k.startswith("_")}


def _set_by_user(values: dict[str, Any], defaults: dict[str, Any], name: str) -> bool:
    """Whether the config holds a value for *name* that somebody chose.

    Empty is not a choice, and neither is the tool's own default: the config
    carries the defaults as well, and treating them as deliberate is what kept
    the router on `http://127.0.0.1:7860` on every manager that runs elsewhere.
    Somebody who types exactly the default loses nothing by this — for the
    manager URL the filled-in address is the right one anyway, and a tool's
    built-in output path is the one that does not exist in the runner.
    """
    value = values.get(name)
    if not str(value if value is not None else "").strip():
        return False
    return name not in defaults or value != defaults[name]


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
        ToolsClass = tool.namespace().get("Tools")
        if ToolsClass is None:
            return None
        instance = ToolsClass()
        # What the tool itself ships, read before anything is applied. The
        # config holds these defaults too — every install and every code save
        # copies them in so they are editable — so a value in the config is
        # only the user's own choice when it differs from them.
        defaults = _valve_defaults(instance)
        if hasattr(instance, "valves") and values:
            for k, v in values.items():
                if hasattr(instance.valves, k):
                    try:
                        setattr(instance.valves, k, v)
                    except Exception:
                        pass
        if content_dir:
            for name in content_valve_names(instance):
                if _set_by_user(values, defaults, name):
                    continue  # the user pointed this one somewhere on purpose
                try:
                    setattr(instance.valves, name, content_dir)
                    logger.info(f"Valve '{name}' set to the content folder: {content_dir}")
                except Exception:
                    pass
        # Same rule, not a second one: filled from what the manager knows, and
        # a value the user set themselves always wins.
        if hasattr(getattr(instance, "valves", None), _MANAGER_VALVE) \
                and not _set_by_user(values, defaults, _MANAGER_VALVE):
            try:
                setattr(instance.valves, _MANAGER_VALVE, manager_url())
                logger.info(f"Valve '{_MANAGER_VALVE}' set to {manager_url()}")
            except Exception:
                pass
        return instance
    except Exception as e:
        logger.error(f"Failed to create Tools instance: {e}")
        return None
