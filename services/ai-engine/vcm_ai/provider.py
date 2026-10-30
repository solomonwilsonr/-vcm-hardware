"""Pluggable LLM provider interface. The default is a no-op: nothing leaves the machine."""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class LLMProvider(Protocol):
    name: str

    def complete(self, prompt: str) -> str | None:
        """Return commentary for the prompt, or None for no output."""


class NoopProvider:
    """Default provider: never calls any network and returns nothing."""

    name = "none"

    def complete(self, prompt: str) -> str | None:
        return None


_REGISTRY: dict[str, Callable[[], LLMProvider]] = {"none": NoopProvider}


def register_provider(name: str, factory: Callable[[], LLMProvider]) -> None:
    """Register a provider factory (e.g. from a plugin). Not enabled unless selected by name."""
    _REGISTRY[name] = factory


def get_provider(name: str = "none") -> LLMProvider:
    if name not in _REGISTRY:
        raise KeyError(f"unknown LLM provider: {name} (available: {', '.join(sorted(_REGISTRY))})")
    return _REGISTRY[name]()
