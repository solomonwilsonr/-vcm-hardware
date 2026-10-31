"""Site-specific rules, added without changing VCM.

A rule plugin is a function taking a :class:`RuleContext`. Plugins are found three ways, all of
them code the operator chose to install or name, never code read out of a design repository:

* ``register_rule(name, fn)`` from Python;
* the ``vcm.rules`` entry-point group of installed packages;
* ``$VCM_RULE_PLUGINS``: comma-separated ``module:function`` names importable on ``sys.path``.

Two constraints protect the report's trust model:

* A plugin may add ``rule_based`` or ``speculative`` findings, never ``verified``. Only VCM's own
  data-derived checks can state facts.
* A plugin that raises does not vanish. Its failure becomes a finding, so "the site rules passed"
  can never be confused with "the site rules crashed".
"""
from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata

from .rules_datacenter import cite, is_node_design, node_meta, prop

ENTRY_POINT_GROUP = "vcm.rules"
ALLOWED = ("rule_based", "speculative")
SEVERITIES = ("critical", "warning", "info")


@dataclass
class Plugin:
    name: str
    fn: Callable
    domain: str = "any"  # "node", "board" or "any"
    origin: str = "registered"


_REGISTRY: dict[str, Plugin] = {}


def register_rule(name: str, fn: Callable, domain: str = "any") -> None:
    if domain not in ("node", "board", "any"):
        raise ValueError(f"domain must be node, board or any, not {domain!r}")
    _REGISTRY[name] = Plugin(name, fn, domain)


def unregister_rule(name: str) -> None:
    _REGISTRY.pop(name, None)


def _resolve(spec: str) -> Callable:
    module, _, attr = spec.partition(":")
    fn = getattr(importlib.import_module(module), attr or "rule")
    if not callable(fn):
        raise TypeError(f"{spec} is not callable")
    return fn


def discover() -> list[Plugin]:
    """Registered plugins, then entry points, then ``$VCM_RULE_PLUGINS``."""
    found = dict(_REGISTRY)
    try:
        eps = metadata.entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover - very old importlib.metadata
        eps = []
    for ep in eps:
        found.setdefault(ep.name, Plugin(ep.name, ep.load, getattr(ep, "domain", "any"),
                                         f"entry point {ep.value}"))
    for spec in filter(None, (x.strip() for x in os.environ.get("VCM_RULE_PLUGINS", "").split(","))):
        found.setdefault(spec, Plugin(spec, lambda s=spec: _resolve(s), "any", "env"))
    return list(found.values())


@dataclass
class RuleContext:
    """What a plugin can see and do. Read the designs; add findings through :meth:`add`."""

    builder: object
    diff: dict
    plugin: str
    produced: list[str] = field(default_factory=list)

    @property
    def before(self):
        return self.builder.da

    @property
    def after(self):
        return self.builder.db

    @property
    def changed_refs(self) -> list[str]:
        return list(self.diff["changed_refs"])

    @property
    def meta(self) -> dict:
        return node_meta(self.builder.db)

    @property
    def catalog(self):
        return self.builder.catalog

    def part(self, ref: str) -> dict | None:
        c = self.builder.db.components.get(ref)
        return self.builder.catalog.lookup(c) if c else None

    def value(self, ref: str, key: str, default=None):
        """Manifest value first, then catalog, like the built-in rules."""
        c = self.builder.db.components.get(ref)
        return prop(c, self.part(ref), key, default) if c else default

    def cite(self, ref: str, keys: list[str]) -> dict:
        return cite(self.builder.db.components[ref], self.part(ref), keys)

    def add(self, severity: str, title: str, detail: str, evidence: list[dict] | None = None,
            refs: list[str] | None = None, confidence: str = "rule_based") -> str:
        if confidence not in ALLOWED:
            raise ValueError(f"plugins may add {' or '.join(ALLOWED)} findings, not {confidence!r}")
        if severity not in SEVERITIES:
            raise ValueError(f"severity must be one of {', '.join(SEVERITIES)}")
        ev = list(evidence or []) + [{"type": "plugin", "plugin": self.plugin}]
        fid = self.builder.add(confidence, severity, title, detail, ev, refs or [])
        self.produced.append(fid)
        return fid


def run_plugins(builder, d: dict) -> list[dict]:
    """Run every applicable plugin; returns a per-plugin status list for the report."""
    domain = "node" if is_node_design(builder.db) else "board"
    status = []
    for plugin in discover():
        if plugin.domain not in ("any", domain):
            continue
        ctx = RuleContext(builder, d, plugin.name)
        try:
            fn = plugin.fn() if plugin.origin != "registered" else plugin.fn
            fn(ctx)
            status.append({"name": plugin.name, "origin": plugin.origin, "status": "ok",
                           "findings": ctx.produced})
        except Exception as exc:  # a broken site rule must be visible, never silent
            fid = builder.add("verified", "warning", f"Rule plugin {plugin.name} failed",
                              f"{type(exc).__name__}: {exc}. Its checks did not run; absence of "
                              "its findings says nothing about this change.",
                              [{"type": "plugin", "plugin": plugin.name}])
            status.append({"name": plugin.name, "origin": plugin.origin, "status": "error",
                           "error": str(exc), "findings": ctx.produced + [fid]})
    return status
