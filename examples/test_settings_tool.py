"""
title: Test Tool
description: Minimaler Test-MCP-Server mit einer konfigurierbaren Einstellung (Valve).
author: Torsten
version: 0.1.0
"""

from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        greeting: str = Field(
            default="Hallo",
            description="Begrüßungswort, das jeder Antwort vorangestellt wird",
        )

    def __init__(self):
        self.valves = self.Valves()

    def greet(self, name: str) -> str:
        """Begrüßt eine Person mit dem aktuell konfigurierten Begrüßungswort.

        Args:
            name: Name der Person, die begrüßt werden soll
        """
        return f"{self.valves.greeting}, {name}!"

    def current_setting(self) -> str:
        """Gibt das aktuell eingestellte Begrüßungswort zurück (zum Testen der Valve)."""
        return f"Aktuelles Begrüßungswort: {self.valves.greeting!r}"
