"""
title: Content Example
description: Minimal example MCP server that produces a file and hands the user a working download link.
author: Torsten Feustel
version: 0.1.0
"""

import os
import re
import secrets
from pathlib import Path

from pydantic import BaseModel, Field

# The OpenWebUI convention for "here is a file I made". A tool returns links
# below this prefix, and the spawner rewrites them into its own download URL —
# with a token for that one file — before the result reaches the model. That
# rewriting is the reason this tool needs to know nothing about the framework:
# the same code also runs inside OpenWebUI, where the prefix resolves natively.
LINK_PREFIX = "/cache/files/"


class Tools:
    class Valves(BaseModel):
        # Left empty on purpose. With File storage switched on for the
        # instance, the spawner fills any valve called content_dir, output_dir
        # or *_export_dir with this instance's folder at startup. A value you
        # type in yourself always wins — that is how you point a tool at a
        # network share instead.
        output_dir: str = Field(
            default="",
            description="Where to write files. Filled automatically when File storage is on.",
        )

    def __init__(self):
        self.valves = self.Valves()

    # ── The one piece of plumbing a file-producing tool needs ────────────────

    def _export_dir(self) -> Path:
        """Where to write, in order of precedence.

        Three ways, because a tool may be installed with File storage off, or
        may have come from somewhere that never heard of this spawner:

        1. the valve — filled by the spawner, or set by hand
        2. MCP_CONTENT_DIR — passed to the runner process
        3. a relative path — runners start with the project root as their
           working directory, so this lands inside the store as well

        Only the first is needed in practice. The other two keep the tool from
        crashing when nobody configured anything — but the third one is a
        symptom, not a solution: files written there sit outside any instance
        folder, so the UI does not list them, and the link this tool returns
        stays a raw /cache/files/ path that leads nowhere. That dead link is
        the point. It shows up on the very first call and says, unmistakably,
        "switch File storage on for this instance".
        """
        configured = (self.valves.output_dir or "").strip()
        target = Path(configured or os.environ.get("MCP_CONTENT_DIR") or "content")
        target.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _safe_name(title: str, suffix: str) -> str:
        """A file name derived from the title — never the title itself.

        Two reasons. A caller picks the title, and a name is part of a path.
        And two documents with the same title must not overwrite each other,
        hence the random tail.
        """
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "note"
        return f"{slug[:40]}_{secrets.token_hex(3)}{suffix}"

    # ── The tools ────────────────────────────────────────────────────────────

    def write_note(self, title: str, text: str) -> str:
        """Write a note to a file and return a download link for it.

        The link is what the user clicks. Returning the *path* instead would be
        useless to them: the file lives on the server, not on their machine.

        Args:
            title: Title of the note; also the basis for the file name
            text: Body of the note
        """
        name = self._safe_name(title, ".md")
        path = self._export_dir() / name
        path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")
        # Deliberately the bare convention prefix and not a full URL: the
        # runner turns this into an absolute, tokenised link on the way out,
        # and it is the only party that knows the server's public address.
        return f"Saved as [{name}]({LINK_PREFIX}{name})"

    def list_notes(self) -> str:
        """List the notes this instance has written so far."""
        files = sorted(p for p in self._export_dir().iterdir() if p.is_file())
        if not files:
            return "No notes yet."
        lines = [f"{len(files)} note(s):"]
        for path in files:
            lines.append(f"- [{path.name}]({LINK_PREFIX}{path.name}) · {path.stat().st_size} B")
        return "\n".join(lines)

    def where_do_files_go(self) -> str:
        """Report which of the three paths is in force (to check the setup)."""
        valve = (self.valves.output_dir or "").strip()
        env = os.environ.get("MCP_CONTENT_DIR", "")
        source = "valve" if valve else "MCP_CONTENT_DIR" if env else "relative fallback"
        lines = [
            f"Writing to: {self._export_dir().resolve()}",
            f"Decided by: {source}",
            f"valve output_dir: {valve or '(empty)'}",
            f"MCP_CONTENT_DIR: {env or '(not set)'}",
        ]
        if source == "relative fallback":
            lines.append(
                "Warning: File storage is off for this instance. Files are written "
                "outside any instance folder and the download links will not work. "
                "Switch it on in Edit \u2192 Config."
            )
        return "\n".join(lines)
