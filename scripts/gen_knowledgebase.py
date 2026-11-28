#!/usr/bin/env python3
"""Knowledge-base generator: static symbol inventory from the ACTUAL source.

Principles (documentation spec):
  * source code is the source of truth — everything here is derived by
    AST-based STATIC inspection (no project code is executed except a
    controlled import of plugin manifests, which are pure declarations);
  * generated output never explains architectural intent by itself —
    signatures/docstrings only; the WHY lives in the handwritten
    knowledgebase/*.md files;
  * honesty over polish: coverage numbers are measured, side-effect tags
    are clearly-labelled heuristics, discrepancies are reported not hidden.

Outputs (all under knowledgebase/; read_only/README.md itself is
HANDWRITTEN and maintained separately):
  generated/symbol_index.json     machine-readable symbol inventory
  generated/module_dependencies.json  import graph (module -> imports)
  generated/plugins.json          plugin manifests (name/version/settings)
  generated/coverage_report.json  measured docstring coverage
  generated/snapshot_manifest.json  commit/time/python/counts metadata
  generated/drift_baseline.json   symbol signature hashes (drift detection)
  read_only/README.md             READ ONLY contract + metadata
  read_only/SYMBOL_INDEX.md       human summary of the inventory
  read_only/modules/<mod>.md      per-module generated reference
  read_only/plugins/<name>.md     per-plugin generated reference

Usage:
  python3 scripts/gen_knowledgebase.py            regenerate everything
  python3 scripts/gen_knowledgebase.py --check    drift report vs baseline
  python3 scripts/gen_knowledgebase.py --skip-plugins  skip the controlled
                                                  plugin-manifest import
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "knowledgebase"
PACKAGES = ["core", "adapters", "database", "interfaces", "plugins"]
EXTRA_MODULES = ["libzentsuite/__main__.py"]

#: symbols that are part of the SUPPORTED plugin API (architecture intent,
#: maintained by hand — everything else public is internal core API)
PLUGIN_API = {
    "libzentsuite.core.plugin_base.PluginBase",
    "libzentsuite.core.plugin_base.PluginContext",
    "libzentsuite.core.plugin_base.PluginServices",
    "core.plugin_base.PluginBase.on_load",
    "core.plugin_base.PluginBase.on_enable",
    "core.plugin_base.PluginBase.on_disable",
    "core.plugin_base.PluginBase.on_unload",
    "core.plugin_base.PluginBase.handle_event",
    "core.account_manager.services.AccountPluginServices.send_message",
    "core.account_manager.services.AccountPluginServices.send_dm",
    "core.account_manager.services.AccountPluginServices.open_dm",
    "core.account_manager.services.AccountPluginServices.fetch_user",
    "core.account_manager.services.AccountPluginServices.add_reaction",
    "core.account_manager.services.AccountPluginServices.click_button",
    "core.account_manager.services.AccountPluginServices.wait_for_message",
    "core.account_manager.services.AccountPluginServices.collect_messages",
    "core.account_manager.services.AccountPluginServices.publish",
    "core.account_manager.services.AccountPluginServices.validate_channel",
    "core.account_manager.services.AccountPluginServices.describe_channel",
    "core.task_scheduler.scheduler.TaskScheduler.submit",
    "core.settings_manager.Setting",
    "core.priority.Priority",
}

#: side-effect heuristics (documented as such — regex over the function body)
_SIDE_EFFECT_PATTERNS = {
    "creates_tasks": ("create_task(", "ensure_future("),
    "sleeps": ("asyncio.sleep(",),
    "publishes_event": ("publish_nowait(", "publish(", "set_result("),
    "db_write": (
        "._db.add(",
        "._repo.",
        "_repo.set",
        "_repo.add",
        "upsert",
        ".kv_set(",
        "set_setting(",
        "set_state(",
        "set_policy_values(",
    ),
    "network": (
        ".send_message(",
        ".send_dm(",
        ".fetch_",
        ".open_dm(",
        ".click_button(",
        ".add_reaction(",
        ".connect()",
    ),
}


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception:
        return "unknown"


def module_name(path: Path) -> str | None:
    rel = path.relative_to(ROOT)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    if rel.name in EXTRA_MODULES and len(parts) == 1 and parts[0] == "main":
        return "main"
    if parts[0] not in PACKAGES:
        return None
    return ".".join(parts)


def decorators_of(node: ast.AST) -> list[str]:
    names = []
    for dec in getattr(node, "decorator_list", []):
        if isinstance(dec, ast.Name):
            names.append(dec.id)
        elif isinstance(dec, ast.Attribute):
            names.append(dec.attr)
        elif isinstance(dec, ast.Call):
            target = dec.func
            names.append(getattr(target, "id", getattr(target, "attr", "call")))
    return names


def signature_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    return f"{prefix} {node.name}({args}){ret}"


def side_effects(source: str) -> list[str]:
    tags = []
    for tag, needles in _SIDE_EFFECT_PATTERNS.items():
        if any(needle in source for needle in needles):
            tags.append(tag)
    return tags


def classify_class(node: ast.ClassDef, decorators: list[str]) -> str:
    bases = {ast.unparse(b) for b in node.bases}
    if any("Enum" in b or "IntEnum" in b or "StrEnum" in b for b in bases):
        return "enum"
    if any("Protocol" in b for b in bases):
        return "protocol"
    if any(
        b in ("Exception", "BaseException") or b.endswith(("Error", "Exception")) for b in bases
    ):
        return "exception"
    if any("ABC" in b for b in bases):
        return "abc"
    if "dataclass" in decorators:
        return "dataclass"
    return "class"


def first_doc_line(doc: str | None) -> str:
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def scan_module(path: Path, module: str) -> dict[str, Any]:
    """AST scan of one module: docstring, classes, functions, imports."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    info: dict[str, Any] = {
        "module": module,
        "path": str(path.relative_to(ROOT)),
        "doc": ast.get_docstring(tree) or "",
        "classes": [],
        "functions": [],
        "imports": [],
        "symbols": 0,
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                info["imports"].append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            info["imports"].append(node.module)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_src = ast.get_source_segment(source, node) or ""
            info["functions"].append(
                {
                    "name": node.name,
                    "qualified": f"{module}.{node.name}",
                    "kind": "function",
                    "async": isinstance(node, ast.AsyncFunctionDef),
                    "signature": signature_of(node),
                    "line": node.lineno,
                    "decorators": decorators_of(node),
                    "public": not node.name.startswith("_"),
                    "plugin_api": f"{module}.{node.name}" in PLUGIN_API,
                    "doc": first_doc_line(ast.get_docstring(node)),
                    "doc_full": (ast.get_docstring(node) or "").strip(),
                    "side_effects": side_effects(body_src),
                }
            )
            info["symbols"] += 1
        elif isinstance(node, ast.ClassDef):
            decorators = decorators_of(node)
            entry: dict[str, Any] = {
                "name": node.name,
                "qualified": f"{module}.{node.name}",
                "kind": classify_class(node, decorators),
                "bases": [ast.unparse(b) for b in node.bases],
                "decorators": decorators,
                "line": node.lineno,
                "public": not node.name.startswith("_"),
                "plugin_api": f"{module}.{node.name}" in PLUGIN_API,
                "doc": first_doc_line(ast.get_docstring(node)),
                "doc_full": (ast.get_docstring(node) or "").strip(),
                "methods": [],
                "side_effects": [],
            }
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    body_src = ast.get_source_segment(source, sub) or ""
                    qual = f"{module}.{node.name}.{sub.name}"
                    entry["methods"].append(
                        {
                            "name": sub.name,
                            "qualified": qual,
                            "kind": "method",
                            "async": isinstance(sub, ast.AsyncFunctionDef),
                            "signature": signature_of(sub),
                            "line": sub.lineno,
                            "decorators": decorators_of(sub),
                            "public": not sub.name.startswith("_"),
                            "plugin_api": qual in PLUGIN_API,
                            "doc": first_doc_line(ast.get_docstring(sub)),
                            "doc_full": (ast.get_docstring(sub) or "").strip(),
                            "side_effects": side_effects(body_src),
                        }
                    )
                    entry["side_effects"] = sorted(
                        set(entry["side_effects"]) | set(entry["methods"][-1]["side_effects"])
                    )
                    info["symbols"] += 1
            info["classes"].append(entry)
    return info


def plugin_manifests() -> list[dict[str, Any]]:
    """Controlled introspection of plugin manifests (declarations only).

    Safe by construction: importing plugins/* executes only imports of the
    stdlib + core declarations — no client, no network, no instantiation
    beyond the loader's own create() (a plain constructor).
    """
    sys.path.insert(0, str(ROOT))
    from libzentsuite.core.plugin_manager.loader import PluginLoader

    manifests: list[dict[str, Any]] = []
    loader = PluginLoader(ROOT / "plugins")
    try:
        records = loader.discover()
    except Exception as exc:  # pragma: no cover
        print(f"warning: plugin discovery failed: {exc}", file=sys.stderr)
        return manifests
    for name, record in sorted(records.items()):
        cls = record.cls
        manifests.append(
            {
                "name": name,
                "version": getattr(cls, "version", "?"),
                "description": getattr(cls, "description", ""),
                "source": str(record.source.relative_to(ROOT)),
                "settings": [s.as_manifest() for s in getattr(cls, "settings", ())],
            }
        )
    return manifests


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def render_module_md(info: dict[str, Any], commit: str) -> str:
    lines = [
        f"# Module `{info['module']}`",
        "",
        "> GENERATED REFERENCE — READ ONLY. Produced by "
        "`scripts/gen_knowledgebase.py` from static AST inspection.",
        f"> Commit: `{commit}`. Regenerate; do not hand-edit.",
        "",
        f"**Path:** `{info['path']}`",
        "",
    ]
    if info["doc"]:
        lines += ["```", info["doc"].strip(), "```", ""]
    if info["classes"]:
        lines += [
            "## Classes",
            "",
            "| Symbol | Kind | Public | Purpose (first doc line) |",
            "|---|---|---|---|",
        ]
        for cls in info["classes"]:
            api = " **PLUGIN API**" if cls["plugin_api"] else ""
            purpose = cls["doc"] or "—"
            lines.append(
                f"| `{cls['name']}`{api} | {cls['kind']} | "
                f"{'yes' if cls['public'] else 'no'} | {purpose} |"
            )
        lines.append("")
        for cls in info["classes"]:
            if cls["methods"]:
                api = " — PLUGIN API" if cls["plugin_api"] else ""
                lines += [f"### `{cls['name']}`{api}", ""]
                for method in cls["methods"]:
                    if not method["public"]:
                        continue
                    api_tag = " *(plugin API)*" if method["plugin_api"] else ""
                    effects = ", ".join(method["side_effects"])
                    effects_note = f" — side effects: `{effects}`" if effects else ""
                    lines.append(f"- `{method['signature']}`{api_tag}{effects_note}")
                    if method["doc"]:
                        lines.append(f"  - {method['doc']}")
                lines.append("")
    if info["functions"]:
        lines += ["## Functions", ""]
        for fn in info["functions"]:
            if not fn["public"]:
                continue
            api_tag = " *(plugin API)*" if fn["plugin_api"] else ""
            effects = ", ".join(fn["side_effects"])
            effects_note = f" — side effects: `{effects}`" if effects else ""
            lines.append(f"- `{fn['signature']}`{api_tag}{effects_note}")
            if fn["doc"]:
                lines.append(f"  - {fn['doc']}")
        lines.append("")
    return "\n".join(lines)


def render_plugin_md(manifest: dict[str, Any], commit: str) -> str:
    lines = [
        f"# Plugin `{manifest['name']}`",
        "",
        "> GENERATED REFERENCE — READ ONLY (declarations extracted by "
        "controlled import; no execution of plugin logic).",
        f"> Commit: `{commit}`.",
        "",
        f"- **Version:** {manifest['version']}",
        f"- **Description:** {manifest['description']}",
        f"- **Source:** `{manifest['source']}`",
        "",
    ]
    if manifest["settings"]:
        lines += [
            "## Declared settings",
            "",
            "| Setting | Type | Default | Bounds | Description |",
            "|---|---|---|---|---|",
        ]
        for s in manifest["settings"]:
            bounds = ""
            if s.get("min") is not None or s.get("max") is not None:
                bounds = f"{s.get('min')}..{s.get('max')}"
            default = "`" + json.dumps(s.get("default")) + "`"
            lines.append(
                f"| `{s['name']}` | {s['type']} | {default} | "
                f"{bounds} | {s.get('description', '')} |"
            )
        lines.append("")
    return "\n".join(lines)


def symbol_hash(entry: dict[str, Any]) -> str:
    basis = "|".join(
        [
            entry.get("qualified", ""),
            entry.get("signature", entry.get("kind", "")),
            str(entry.get("async", False)),
        ]
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def build_all(skip_plugins: bool) -> dict[str, Any]:
    commit = git_commit()
    modules: list[dict[str, Any]] = []
    paths = []
    for package in PACKAGES:
        paths.extend(sorted((ROOT / package).rglob("*.py")))
    for name in EXTRA_MODULES:
        path = ROOT / name
        if path.is_file():
            paths.append(path)
    for path in paths:
        module = module_name(path)
        if module is None:
            continue
        modules.append(scan_module(path, module))
    modules.sort(key=lambda m: m["module"])

    flat: list[dict[str, Any]] = []
    for info in modules:
        flat.append(
            {
                "module": info["module"],
                "symbol": info["module"],
                "kind": "module",
                "async": False,
                "purpose": first_doc_line(info["doc"]),
                "public": True,
                "plugin_api": False,
                "path": info["path"],
            }
        )
        for cls in info["classes"]:
            base = {
                "module": info["module"],
                "symbol": cls["name"],
                "qualified": cls["qualified"],
                "kind": cls["kind"],
                "async": False,
                "purpose": cls["doc"],
                "public": cls["public"],
                "plugin_api": cls["plugin_api"],
                "path": info["path"],
                "line": cls["line"],
                "bases": cls["bases"],
                "decorators": cls["decorators"],
                "side_effects": cls["side_effects"],
            }
            flat.append(base)
            for method in cls["methods"]:
                flat.append(
                    {
                        "module": info["module"],
                        "symbol": f"{cls['name']}.{method['name']}",
                        "qualified": method["qualified"],
                        "kind": "method",
                        "async": method["async"],
                        "purpose": method["doc"],
                        "public": method["public"],
                        "plugin_api": method["plugin_api"],
                        "path": info["path"],
                        "line": method["line"],
                        "signature": method["signature"],
                        "side_effects": method["side_effects"],
                    }
                )
        for fn in info["functions"]:
            flat.append(
                {
                    "module": info["module"],
                    "symbol": fn["name"],
                    "qualified": fn["qualified"],
                    "kind": "function",
                    "async": fn["async"],
                    "purpose": fn["doc"],
                    "public": fn["public"],
                    "plugin_api": fn["plugin_api"],
                    "path": info["path"],
                    "line": fn["line"],
                    "signature": fn["signature"],
                    "side_effects": fn["side_effects"],
                }
            )

    plugins = [] if skip_plugins else plugin_manifests()
    return {"commit": commit, "modules": modules, "flat": flat, "plugins": plugins}


def coverage(modules: list[dict[str, Any]]) -> dict[str, Any]:
    stats = {
        "public_functions": [0, 0],
        "public_classes": [0, 0],
        "plugin_api": [0, 0],
        "modules": [0, 0],
    }
    for info in modules:
        stats["modules"][0] += 1
        if info["doc"].strip():
            stats["modules"][1] += 1
        for fn in info["functions"]:
            if fn["public"]:
                stats["public_functions"][0] += 1
                if fn["doc_full"]:
                    stats["public_functions"][1] += 1
            if fn["plugin_api"]:
                stats["plugin_api"][0] += 1
                if fn["doc_full"]:
                    stats["plugin_api"][1] += 1
        for cls in info["classes"]:
            if cls["public"]:
                stats["public_classes"][0] += 1
                if cls["doc_full"]:
                    stats["public_classes"][1] += 1
            if cls["plugin_api"]:
                stats["plugin_api"][0] += 1
                if cls["doc_full"]:
                    stats["plugin_api"][1] += 1
            for method in cls["methods"]:
                if method["plugin_api"]:
                    stats["plugin_api"][0] += 1
                    if method["doc_full"]:
                        stats["plugin_api"][1] += 1
    report: dict[str, Any] = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    for key, (total, documented) in stats.items():
        report[key] = {
            "total": total,
            "documented": documented,
            "percent": round(100.0 * documented / total, 1) if total else None,
        }
    gaps = []
    for info in modules:
        for fn in info["functions"]:
            if fn["public"] and not fn["doc_full"]:
                gaps.append(fn["qualified"])
        for cls in info["classes"]:
            if cls["public"] and not cls["doc_full"]:
                gaps.append(cls["qualified"])
    report["missing_doc_public_symbols"] = gaps
    return report


def drift_check(new_flat: list[dict[str, Any]]) -> int:
    baseline_path = KB / "generated" / "drift_baseline.json"
    if not baseline_path.is_file():
        # knowledgebase/ is generated AND gitignored — a fresh checkout has
        # none. Degrade gracefully: nothing to compare is not an error.
        print(
            "no baseline present (knowledgebase/ is generated + gitignored) — "
            "nothing to drift-check; run WITHOUT --check once to generate it"
        )
        return 0
    old = json.loads(baseline_path.read_text())
    new = {e["qualified"]: symbol_hash(e) for e in new_flat if e.get("qualified")}
    removed = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    changed = sorted(q for q in set(old) & set(new) if old[q] != new[q])
    print(f"DRIFT REPORT: +{len(added)} new, -{len(removed)} removed, ~{len(changed)} changed")
    for q in added[:40]:
        print(f"  + {q}")
    for q in removed[:40]:
        print(f"  - {q}")
    for q in changed[:40]:
        print(f"  ~ {q}")
    return 0 if not (added or removed or changed) else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="compare current symbols against the baseline"
    )
    parser.add_argument("--skip-plugins", action="store_true")
    args = parser.parse_args()

    data = build_all(skip_plugins=args.skip_plugins)
    commit, modules, flat, plugins = (
        data["commit"],
        data["modules"],
        data["flat"],
        data["plugins"],
    )

    if args.check:
        return drift_check(flat)

    KB.mkdir(exist_ok=True)
    write_json(KB / "generated" / "symbol_index.json", flat)
    write_json(
        KB / "generated" / "module_dependencies.json", {m["module"]: m["imports"] for m in modules}
    )
    write_json(KB / "generated" / "plugins.json", plugins)
    write_json(KB / "generated" / "coverage_report.json", coverage(modules))
    write_json(
        KB / "generated" / "drift_baseline.json",
        {e["qualified"]: symbol_hash(e) for e in flat if e.get("qualified")},
    )
    write_json(
        KB / "generated" / "snapshot_manifest.json",
        {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "commit": commit,
            "python": sys.version.split()[0],
            "generator": "scripts/gen_knowledgebase.py (static AST inspection)",
            "schema": 1,
            "counts": {
                "modules": len(modules),
                "symbols": len(flat),
                "plugins": len(plugins),
                "public_symbols": sum(1 for e in flat if e.get("public")),
                "plugin_api_symbols": sum(1 for e in flat if e.get("plugin_api")),
            },
        },
    )

    # ---- read_only snapshot (generated markdown) ----
    ro = KB / "read_only"
    (ro / "modules").mkdir(parents=True, exist_ok=True)
    (ro / "plugins").mkdir(parents=True, exist_ok=True)
    for info in modules:
        (ro / "modules" / f"{info['module']}.md").write_text(
            render_module_md(info, commit), encoding="utf-8"
        )
    for manifest in plugins:
        (ro / "plugins" / f"{manifest['name']}.md").write_text(
            render_plugin_md(manifest, commit), encoding="utf-8"
        )
    index_lines = [
        "# SYMBOL INDEX (generated, READ ONLY)",
        "",
        f"> Commit `{commit}` — regenerate with "
        "`python3 scripts/gen_knowledgebase.py`. Machine-readable form: "
        "`knowledgebase/generated/symbol_index.json`.",
        "",
        "| Module | Classes | Functions | Symbols | Purpose |",
        "|---|---|---|---|---|",
    ]
    for info in modules:
        index_lines.append(
            f"| [`{info['module']}`](modules/{info['module']}.md) | "
            f"{len(info['classes'])} | {len(info['functions'])} | "
            f"{info['symbols']} | {first_doc_line(info['doc']) or '—'} |"
        )
    (ro / "SYMBOL_INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    counts = json.loads((KB / "generated" / "snapshot_manifest.json").read_text())["counts"]
    print(
        f"generated: {counts['modules']} modules, {counts['symbols']} "
        f"symbols, {counts['plugins']} plugins -> {KB}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
