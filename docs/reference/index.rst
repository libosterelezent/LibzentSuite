Reference
=========

Generated knowledge artifacts (regenerate with
``python3 scripts/gen_knowledgebase.py``; drift-check with ``--check``).
The handwritten deep dives in ``knowledgebase/*.md`` are committed with
the repository; only ``knowledgebase/generated/`` is gitignored.

* ``knowledgebase/read_only/SYMBOL_INDEX.md`` — module inventory snapshot
* ``knowledgebase/generated/symbol_index.json`` — machine-readable index
  (1073 symbols across 77 modules at the last measured baseline: qualified
  name, kind, async, purpose, public/plugin-api flags, heuristic
  side-effect tags)
* ``knowledgebase/generated/coverage_report.json`` — measured docstring
  coverage (100% public functions/classes/plugin API/modules at the last
  measured baseline)
* ``knowledgebase/generated/drift_baseline.json`` — signature hashes for
  drift detection
* ``knowledgebase/generated/snapshot_manifest.json`` — commit/time/python
  metadata for the snapshot

Handwritten deep dives (architecture, data flow, lifecycle,
per-subsystem references, the formal plugin API, configuration, CLI,
endpoint reference, testing, security, glossary, changelog)
live in ``knowledgebase/*.md`` — committed with the repository,
maintained by hand, never overwritten by the generator.
