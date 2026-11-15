LibzentSuite — Documentation
==============================

Multi-account Discord automation framework: plugins that request services
from a central core, first-class accounts with direct account→plugin
assignments (the capability model), a
four-scope rate-limiting stack, a centralized task pipeline, SQLite
persistence, and the CLI management surface (a FastAPI core API + Flet UI
is the planned next interface) over
ONE Core API.

.. warning::
   Selfbot usage violates Discord's Terms of Service. The safety stack
   (1.5s/action hard floor, human-like jitter, server-wide sequential
   lane, server-limit compliance) mitigates risk — it does not authorize
   it.

.. toctree::
   :maxdepth: 2
   :caption: Site

   architecture/index
   design/index
   plugins/index
   operations/index
   api/index
   reference/index
   roadmap

Indices
-------

* :ref:`genindex`
* :ref:`modindex`

Where the knowledge lives
-------------------------

* ``knowledgebase/`` — the deep knowledge base, committed with the
  repository: handwritten ``*.md`` deep-dive guides (the WHY — start at
  ``knowledgebase/README.md``; the complete surface reference is
  ``knowledgebase/API_ENDPOINTS.md``) plus generated artifacts.
* ``knowledgebase/read_only/`` — generated READ-ONLY symbol snapshot
  (regenerate with ``python3 scripts/gen_knowledgebase.py``).
* ``knowledgebase/generated/`` — machine-readable indexes
  (``symbol_index.json``, coverage, drift baseline) — gitignored.
* This Sphinx site — navigable API reference (autodoc from the real
  docstrings) + condensed architecture/operations pages.
