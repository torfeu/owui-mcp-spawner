"""
title: Valve Example
description: Minimal example MCP server with one configurable setting (Valve).
author: Torsten Feustel
version: 0.1.0
"""

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        greeting: str = Field(
            default="Hello",
            description="Greeting word prepended to every answer",
        )

    def __init__(self):
        self.valves = self.Valves()

    def greet(self, name: str) -> str:
        """Greet a person with the currently configured greeting word.

        Args:
            name: Name of the person to greet
        """
        return f"{self.valves.greeting}, {name}!"

    def current_setting(self) -> str:
        """Return the greeting word that is configured right now (to test the Valve)."""
        return f"Current greeting word: {self.valves.greeting!r}"
