"""
Shared JSON-Schema generation for Tools methods.

Used by tool_loader (runtime tool definitions) and tool_editor (validation /
export) so both produce identical schemas from the same Python code.
"""
import inspect
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

# OpenWebUI injected parameter names — skip from schema
INJECTED_PARAMS = {"self", "cls", "__user__", "__event_emitter__", "__event_call__", "__request__"}


def type_to_json_schema(annotation: Any) -> dict:
    """Convert a Python type annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}

    origin = get_origin(annotation)
    args = get_args(annotation)

    # Annotated[T, Field(...)] — unwrap, description is handled by the caller
    if origin is Annotated:
        return type_to_json_schema(args[0])

    # Optional[X] / Union[X, None]
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return type_to_json_schema(non_none[0])
        return {"type": "string"}

    # Literal["a", "b", ...]
    if origin is Literal:
        values = list(args)
        if all(isinstance(v, str) for v in values):
            return {"type": "string", "enum": values}
        if all(isinstance(v, int) for v in values):
            return {"type": "integer", "enum": values}
        return {"enum": values}

    # List[X]
    if origin is list:
        item_schema = type_to_json_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}

    if origin is dict:
        return {"type": "object"}

    # Primitives
    _map = {str: "string", int: "integer", float: "number", bool: "boolean",
            list: "array", dict: "object"}
    if annotation in _map:
        return {"type": _map[annotation]}

    return {"type": "string"}


def field_description(annotation: Any) -> str:
    """Extract Field(description=...) from an Annotated type, if present."""
    if get_origin(annotation) is not Annotated:
        return ""
    for meta in get_args(annotation)[1:]:
        if hasattr(meta, "description") and isinstance(meta.description, str) and meta.description:
            return meta.description
        # plain string as second Annotated arg
        if isinstance(meta, str):
            return meta
    return ""


def parse_docstring_params(doc: str) -> dict[str, str]:
    """Parse a Google-style 'Args:' / 'Parameters:' section from a docstring."""
    if not doc:
        return {}
    params: dict[str, str] = {}
    in_args = False
    current: str | None = None
    lines_buf: list[str] = []

    for line in doc.splitlines():
        stripped = line.strip()

        if stripped.rstrip(":").lower() in ("args", "arguments", "parameters") and stripped.endswith(":"):
            in_args = True
            continue

        if in_args:
            # New top-level section ends the Args block
            if stripped and not line.startswith("    "):
                break

            # Param line: "    param_name: description" or "    param_name (type): desc"
            if line.startswith("    ") and not line.startswith("        ") and ":" in stripped:
                if current is not None:
                    params[current] = " ".join(lines_buf).strip()
                colon = stripped.index(":")
                param_name = stripped[:colon].split("(")[0].strip()
                current = param_name
                lines_buf = [stripped[colon + 1:].strip()]
            elif line.startswith("        ") and current is not None:
                lines_buf.append(stripped)

    if current is not None:
        params[current] = " ".join(lines_buf).strip()

    return params


def build_schema_from_method(method: Any, exec_ns: dict | None = None) -> dict:
    """
    Build a full JSON Schema for a Tools method by introspecting its
    type hints (Literal → enum, Annotated/Field → description) and defaults.
    Falls back gracefully when introspection fails.
    """
    ns = exec_ns or {}
    try:
        hints = get_type_hints(method, globalns=ns, localns=ns, include_extras=True)
    except Exception:
        try:
            hints = get_type_hints(method, include_extras=True)
        except Exception:
            hints = {}

    try:
        sig = inspect.signature(method)
    except Exception:
        return {"type": "object", "properties": {}}

    doc_params = parse_docstring_params(inspect.getdoc(method) or "")

    properties: dict[str, dict] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in INJECTED_PARAMS:
            continue

        raw_annotation = hints.get(param_name, inspect.Parameter.empty)
        fragment = type_to_json_schema(raw_annotation)
        desc = field_description(raw_annotation) or doc_params.get(param_name, "")
        if desc:
            fragment["description"] = desc

        if param.default is not inspect.Parameter.empty and param.default is not None:
            fragment["default"] = param.default
        elif param.default is inspect.Parameter.empty:
            required.append(param_name)

        properties[param_name] = fragment

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema
