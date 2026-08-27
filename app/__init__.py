"""OWUI MCP Spawner.

`__version__` is the single source of truth for the version: pyproject.toml
reads it via `[tool.setuptools.dynamic]`, the API serves it as APP_VERSION and
the update check compares it against the latest GitHub release. A second place
to bump would mean shipping a build that reports itself outdated (or up to
date) when it is not.
"""

__version__ = "0.2.0"
