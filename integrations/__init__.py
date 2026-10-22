"""Pluggable integrations for Max.

Drop a new ``.py`` file into this package and it goes live with **no changes to
``main.py``**: ``max integrations`` lists it, and any chat message its ``match()``
claims gets routed to its ``handle()``.

Contract for an integration module (copy ``_template.py`` and rename without the
leading underscore):

    NAME: str                                  # short slug, e.g. "weather"
    DESCRIPTION: str                           # one line for `max integrations`
    USAGE: list[str]                           # example phrases
    def match(text: str) -> dict | None        # claim a message -> payload, else None
    async def handle(event: ChatEvent, payload: dict) -> str | None   # the reply

Modules whose names start with ``_`` are ignored by discovery (that is how the
template stays dormant), and modules missing part of the contract are skipped with a
console warning instead of crashing the bot.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Optional


@dataclass
class ChatEvent:
    """Everything an integration may need to answer a chat message."""

    channel: Any  # Discord text channel (anything with async ``send``)
    author_id: int
    raw_text: str


_modules: dict[str, ModuleType] = {}
_discovered = False


def discover(force: bool = False) -> dict[str, ModuleType]:
    """Import every non-underscore module in this package (once)."""
    global _discovered
    if _discovered and not force:
        return _modules
    package = sys.modules[__name__]
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        name = getattr(module, "NAME", None)
        if (
            not name
            or not callable(getattr(module, "match", None))
            or not callable(getattr(module, "handle", None))
        ):
            print(f"⚠️ integrations/{info.name}.py skipped (needs NAME, match, handle)")
            continue
        _modules[name] = module
    _discovered = True
    return _modules


def match(text: str) -> Optional[tuple[str, dict]]:
    """First integration that claims this message -> (name, payload), else None."""
    for name, module in discover().items():
        payload = module.match(text or "")
        if payload is not None:
            return name, payload
    return None


async def dispatch(name: str, payload: dict, event: ChatEvent) -> Optional[str]:
    """Run the integration's handler and return its reply (already-clipped text)."""
    module = discover().get(name)
    if module is None:
        return None
    return await module.handle(event, payload)


def catalog() -> list[tuple[str, str, list[str]]]:
    """[(name, description, usage examples), …] for help/listing output."""
    return [
        (name, str(getattr(module, "DESCRIPTION", "")), list(getattr(module, "USAGE", [])))
        for name, module in discover().items()
    ]
