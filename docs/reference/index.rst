Reference
=========

Generated knowledge artifacts (regenerate with
``python3 scripts/gen_knowledgebase.py``; drift-check with ``--check``):

* ``knowledgebase/read_only/SYMBOL_INDEX.md`` — module inventory snapshot
* ``knowledgebase/generated/symbol_index.json`` — machine-readable index
  (873 symbols: qualified name, kind, async, purpose, public/plugin-api
  flags, heuristic side-effect tags)
* ``knowledgebase/generated/coverage_report.json`` — measured docstring
  coverage (currently 100% public functions/classes/plugin API/modules)
* ``knowledgebase/generated/drift_baseline.json`` — signature hashes for
  drift detection
* ``knowledgebase/generated/snapshot_manifest.json`` — commit/time/python
  metadata for the snapshot
* ``knowledgebase/AGENT_GUIDE.md`` — orientation for coding/debugging
  agents (source-of-truth priority order included)

Handwritten deep dives live one level up in ``knowledgebase/`` —
architecture, data flow, lifecycle, per-subsystem references (rate
limiting, scheduler, event bus, logging, database, errors), the formal
plugin API, plugin guides, configuration, CLI, dashboard, testing,
security, glossary, and changelog.
