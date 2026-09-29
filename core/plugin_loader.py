"""Plugin loader: discovers PluginBase subclasses under a plugin directory.

Layout conventions (relative to the plugins dir):
  * ``<dir>/<plugin_name>/__init__.py``  -> package plugin (preferred)
  * ``<dir>/<plugin_name>.py``           -> single-file plugin
Modules are loaded with unique synthetic module names so no sys.path hacks are
needed. Every discovered class must subclass PluginBase; names must be unique.
"""
from __future__ import annotations

import importlib
import importlib.util
import itertools
import logging
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Type

from .plugin_base import PluginBase

LOG = logging.getLogger("slaughtermudae.loader")

_PARENT_SEQ = itertools.count()


class PluginLoadError(RuntimeError):
    """Raised when the plugin directory cannot be imported or is inconsistent."""


@dataclass(frozen=True)
class PluginRecord:
    """A discovered plugin: metadata class + where it came from."""

    name: str
    cls: Type[PluginBase]
    source: Path


class PluginLoader:
    """Discovers and instantiates plugins from a directory tree."""

    def __init__(self, plugin_dir: str | Path) -> None:
        self._dir = Path(plugin_dir)
        self._records: Dict[str, PluginRecord] = {}
        self._discovered = False

    @property
    def directory(self) -> Path:
        return self._dir

    def discover(self) -> Dict[str, PluginRecord]:
        """Scan the plugin dir; returns name -> record.

        Raises PluginLoadError on duplicate names or invalid plugins.
        """
        self._records = {}
        if not self._dir.is_dir():
            raise PluginLoadError(f"plugin directory not found: {self._dir}")
        # synthetic parent package so '<dir>/<name>/__init__.py' plugins can use
        # relative imports ('from .plugin import X') without sys.path tricks
        parent_name = f"_mudae_plugroot_{next(_PARENT_SEQ)}"
        parent = types.ModuleType(parent_name)
        parent.__path__ = [str(self._dir)]
        sys.modules[parent_name] = parent
        candidates: List[tuple] = []
        for entry in sorted(self._dir.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            if entry.is_dir() and (entry / "__init__.py").is_file():
                candidates.append(("package", entry.name, entry / "__init__.py"))
            elif entry.is_file() and entry.suffix == ".py":
                candidates.append(("file", entry.name, entry))
        for kind, name, path in candidates:
            if kind == "package":
                try:
                    module = importlib.import_module(f"{parent_name}.{name}")
                except Exception as exc:
                    raise PluginLoadError(f"error importing plugin {path}: {exc}") from exc
            else:
                module = self._import_file(f"{parent_name}_{name}", path)
            prefix = module.__name__ + "."
            for attr in vars(module).values():
                if (
                    isinstance(attr, type)
                    and issubclass(attr, PluginBase)
                    and attr is not PluginBase
                    and (
                        attr.__module__ == module.__name__
                        or attr.__module__.startswith(prefix)
                    )
                ):
                    self._register(attr, path)
        self._discovered = True
        LOG.info("discovered %d plugin(s) in %s: %s", len(self._records), self._dir, sorted(self._records))
        return dict(self._records)

    def _import_file(self, modname: str, path: Path):
        """Load a single-file plugin module under a unique synthetic name."""
        spec = importlib.util.spec_from_file_location(modname, path)
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"cannot import plugin module at {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            raise PluginLoadError(f"error importing plugin {path}: {exc}") from exc
        return module

    def _register(self, cls: Type[PluginBase], path: Path) -> None:
        name = getattr(cls, "name", None)
        if not name or not isinstance(name, str):
            raise PluginLoadError(f"plugin class {cls.__name__} in {path} has no valid 'name'")
        if name != name.lower() or not name.replace("_", "").replace("-", "").isalnum():
            raise PluginLoadError(f"plugin name {name!r} must be lowercase [a-z0-9_-]")
        if name in self._records:
            raise PluginLoadError(f"duplicate plugin name {name!r} ({path} vs {self._records[name].source})")
        self._records[name] = PluginRecord(name=name, cls=cls, source=path)

    def names(self) -> List[str]:
        """Sorted plugin names (discovers lazily on first call)."""
        if not self._discovered:
            self.discover()
        return sorted(self._records)

    def record(self, name: str) -> PluginRecord:
        """Record lookup; raises PluginLoadError for unknown plugins."""
        if not self._discovered:
            self.discover()
        try:
            return self._records[name]
        except KeyError as exc:
            raise PluginLoadError(f"unknown plugin {name!r} (known: {self.names()})") from exc

    def create(self, name: str) -> PluginBase:
        """Fresh instance of a plugin (one per account/plugin pair)."""
        return self.record(name).cls()

    def manifests(self) -> List[dict]:
        """JSON-safe manifests for CLI/dashboard display."""
        if not self._discovered:
            self.discover()
        return [
            {
                "name": rec.name,
                "version": rec.cls.version,
                "description": rec.cls.description,
                "settings": [d.as_manifest() for d in rec.cls.settings],
            }
            for rec in sorted(self._records.values(), key=lambda r: r.name)
        ]
