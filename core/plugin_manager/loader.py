"""Plugin discovery: finds PluginBase subclasses under a plugin directory.

Ported from the audited ``core/plugin_loader.py`` (git aa76da4). Layout
conventions (relative to the plugins dir):

  * ``<dir>/<plugin_name>/__init__.py``  -> package plugin (preferred;
    submodules can use relative imports, e.g. ``from .textutils import ...``)
  * ``<dir>/<plugin_name>.py``           -> single-file plugin

Modules are loaded under a **synthetic parent package** (a unique module
object whose ``__path__`` is the plugins dir), so package plugins get
working relative imports without any ``sys.path`` hacks and without
colliding with real packages.

Audit-hardened behaviour kept from the original:
  * strict ASCII plugin names (``[a-z0-9_-]+``) — ``isalnum()`` accepted
    Unicode slugs that could never round-trip through settings keys,
  * duplicate plugin names and duplicate setting keys are hard errors,
  * dotted directory names are skipped with a warning (they cannot be
    imported as packages — a confusing ImportError otherwise),
  * a module exporting no PluginBase subclass logs a warning instead of
    being silently skipped.

All discovery failures raise :class:`core.errors.PluginError` with a
human-readable message (the old loader's PluginLoadError folded into the
shared domain error vocabulary).
"""
from __future__ import annotations

import importlib
import importlib.util
import itertools
import logging
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Type

from ..errors import PluginError
from ..plugin_base import PluginBase

LOG = logging.getLogger("slaughtermudae.loader")

#: unique synthetic parent package names across repeated scans (tests)
_PARENT_SEQ = itertools.count()

#: the only legal plugin slugs: lowercase ascii, digits, underscore, dash
_NAME_RE = re.compile(r"[a-z0-9_-]+")


@dataclass(frozen=True)
class PluginRecord:
    """A discovered plugin: metadata class + where it came from."""

    name: str
    cls: Type[PluginBase]
    source: Path


class PluginLoader:
    """Discovers and instantiates plugins from a directory tree.

    One loader owns one directory; ``discover()`` rescans on every call
    (fresh module objects under a fresh synthetic parent) so tests stay
    isolated — production code normally scans once at boot.
    """

    def __init__(self, plugin_dir: str | Path) -> None:
        self._dir = Path(plugin_dir)
        self._records: Dict[str, PluginRecord] = {}
        self._discovered = False
        #: synthetic parents THIS loader created (purged on rescan — see
        #: ``_purge_stale_parents``; per-loader so parallel loaders on
        #: other dirs never touch each other's modules)
        self._parent_names: List[str] = []

    def _purge_stale_parents(self) -> None:
        """Drop THIS loader's previous synthetic parents from sys.modules.

        Every discover() builds a fresh parent (unique via ``_PARENT_SEQ``);
        without this cleanup each rescan left the whole previous generation
        of plugin modules alive in sys.modules forever (unbounded growth in
        long-lived processes that rescan). Classes/instances created from
        those modules stay functional — only the import registry entry goes.
        """
        for parent in self._parent_names:
            for modname in [
                m for m in sys.modules
                if m == parent
                or m.startswith(parent + ".")
                or m.startswith(parent + "_")
            ]:
                sys.modules.pop(modname, None)
        self._parent_names = []

    @property
    def directory(self) -> Path:
        """The directory this loader scans."""
        return self._dir

    # ------------------------------------------------------------ discovery
    def discover(self) -> Dict[str, PluginRecord]:
        """Scan the plugin dir; returns ``name -> record`` (fresh scan).

        Raises PluginError when the directory is missing, a module cannot
        be imported, or the tree is inconsistent (duplicate plugin name,
        duplicate setting key, invalid plugin name).
        """
        self._purge_stale_parents()
        self._records = {}
        if not self._dir.is_dir():
            raise PluginError(f"plugin directory not found: {self._dir}")
        # synthetic parent package so '<dir>/<name>/__init__.py' plugins can
        # use relative imports ('from .plugin import X') without sys.path
        parent_name = f"_mudae_plugroot_{next(_PARENT_SEQ)}"
        self._parent_names.append(parent_name)
        parent = types.ModuleType(parent_name)
        parent.__path__ = [str(self._dir)]
        sys.modules[parent_name] = parent
        candidates: List[tuple] = []
        for entry in sorted(self._dir.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            # dotted dir names can't be imported as packages — skip them
            # clearly instead of failing with a confusing ImportError
            if entry.is_dir() and "." in entry.name:
                LOG.warning("skipping plugin dir with dotted name: %s", entry)
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
                    raise PluginError(f"error importing plugin {path}: {exc}") from exc
            else:
                module = self._import_file(f"{parent_name}_{Path(name).stem}", path)
            found_here = 0
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
                    found_here += 1
            if found_here == 0:
                # a module exporting no plugin used to be silently skipped
                LOG.warning(
                    "%s exports no PluginBase subclass (did you forget to inherit?)", path
                )
        self._discovered = True
        LOG.info(
            "discovered %d plugin(s) in %s: %s",
            len(self._records), self._dir, sorted(self._records),
        )
        return dict(self._records)

    def _import_file(self, modname: str, path: Path) -> types.ModuleType:
        """Load a single-file plugin module under a unique synthetic name.

        Raises PluginError when the file cannot be loaded or executed.
        """
        spec = importlib.util.spec_from_file_location(modname, path)
        if spec is None or spec.loader is None:
            raise PluginError(f"cannot import plugin module at {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(modname, None)  # no half-executed module residue
            raise PluginError(f"error importing plugin {path}: {exc}") from exc
        return module

    def _register(self, cls: Type[PluginBase], path: Path) -> None:
        """Validate one candidate class and record it under its slug.

        Raises PluginError for a missing/invalid name, a duplicate setting
        key, or a duplicate plugin name.
        """
        name = getattr(cls, "name", None)
        if not name or not isinstance(name, str):
            raise PluginError(f"plugin class {cls.__name__} in {path} has no valid 'name'")
        if not _NAME_RE.fullmatch(name):
            raise PluginError(f"plugin name {name!r} must be lowercase [a-z0-9_-]")
        seen_keys = set()
        for setting in cls.settings or ():
            if setting.name in seen_keys:
                raise PluginError(
                    f"plugin {name!r} declares duplicate setting key {setting.name!r}"
                )
            seen_keys.add(setting.name)
        if name in self._records:
            raise PluginError(
                f"duplicate plugin name {name!r} "
                f"({path} vs {self._records[name].source})"
            )
        self._records[name] = PluginRecord(name=name, cls=cls, source=path)

    # ------------------------------------------------------------- accessors
    def names(self) -> List[str]:
        """Sorted plugin names (discovers lazily on first call)."""
        if not self._discovered:
            self.discover()
        return sorted(self._records)

    def record(self, name: str) -> PluginRecord:
        """Record lookup; raises PluginError for unknown plugins."""
        if not self._discovered:
            self.discover()
        try:
            return self._records[name]
        except KeyError as exc:
            raise PluginError(
                f"unknown plugin {name!r} (known: {self.names()})"
            ) from exc

    def create(self, name: str) -> PluginBase:
        """Fresh instance of a plugin (one per account/plugin pair)."""
        return self.record(name).cls()

    def manifests(self) -> List[dict]:
        """JSON-safe manifests (metadata + settings schema) for CLI/UI."""
        if not self._discovered:
            self.discover()
        return [
            {
                "name": rec.name,
                "version": rec.cls.version,
                "description": rec.cls.description,
                "settings": [s.as_manifest() for s in rec.cls.settings],
            }
            for rec in sorted(self._records.values(), key=lambda r: r.name)
        ]


__all__ = ["PluginLoader", "PluginRecord"]
