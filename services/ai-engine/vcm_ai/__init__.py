"""Offline rule-based hardware change impact analysis with a pluggable LLM interface."""
from .engine import CLASSES, analyze
from .provider import LLMProvider, NoopProvider, get_provider, register_provider
from .render import to_markdown

__all__ = ["CLASSES", "analyze", "LLMProvider", "NoopProvider", "get_provider",
           "register_provider", "to_markdown"]
