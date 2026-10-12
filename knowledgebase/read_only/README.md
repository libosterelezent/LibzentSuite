# READ-ONLY Knowledge Snapshot

> **Status: READ ONLY (logical convention — see "Enforcement" below)**

This folder is a **generated reference snapshot** of what the codebase is,
derived by static AST inspection of the source tree. It exists so that
developers and coding agents have a stable, browsable inventory of every
module, class, function, method, and plugin — without re-exploring the
repository — and without this reference being casually edited during
ordinary feature work.

## What is here

| Path | Content |
|---|---|
| `SYMBOL_INDEX.md` | summary table of every module (classes/functions/symbols + purpose) |
| `modules/<module>.md` | per-module reference: docstring, classes with public methods, public functions, heuristic side-effect tags |
| `plugins/<name>.md` | per-plugin manifest snapshot (version, description, declared settings) |

Machine-readable counterparts live in `../generated/`
(`symbol_index.json`, `plugins.json`, `module_dependencies.json`,
`coverage_report.json`, `drift_baseline.json`, `snapshot_manifest.json`).

## Source-of-truth relationship

**Priority order (always):**

1. Current source implementation
2. Current tests
3. Current schema/configuration
4. This generated reference
5. Handwritten architectural documentation (`knowledgebase/*.md`)

If this snapshot disagrees with the source, the source is right and the
snapshot is stale. Record the discrepancy (see `../DISCREPANCIES.md`)
instead of editing this folder.

## Enforcement — honest distinction

- **Logical read-only convention:** this folder is generated output. Do not
  hand-edit; any manual change will be silently overwritten by the next
  regeneration.
- **Filesystem enforcement: NOT configured by default.** A folder named
  `read_only` is not immutable at the OS level. If you want real
  enforcement, apply it explicitly, e.g.:

  ```bash
  # make the snapshot unwritable (regeneration requires unlocking first)
  chmod -R a-w knowledgebase/read_only
  # unlock before regenerating
  chmod -R u+w knowledgebase/read_only && python3 scripts/gen_knowledgebase.py
  ```

  The project does not ship this by default so regeneration stays
  frictionless; treat the convention as binding regardless.

## Regeneration & drift detection

```bash
python3 scripts/gen_knowledgebase.py           # regenerate snapshot + indexes
python3 scripts/gen_knowledgebase.py --check   # drift report vs the baseline
```

`--check` reports symbols added/removed/signature-changed since the
baseline was written (see `../generated/drift_baseline.json`), enabling
documentation-drift detection after substantial code changes.

## Snapshot metadata

See `../generated/snapshot_manifest.json` for the authoritative metadata:
generation timestamp, source commit, Python version, generator identity,
schema version, and symbol counts.

## Caveats

- Side-effect tags in `modules/*.md` (`creates_tasks`, `sleeps`,
  `publishes_event`, `db_write`, `network`) are **regex heuristics** over
  function bodies — indicative, not authoritative. Verify in source before
  relying on them for concurrency analysis.
- First-doc-line "purposes" are exactly that: the first line of each
  docstring. Depth lives in the full docstrings (Sphinx API reference)
  and the handwritten `knowledgebase/*.md` guides.
