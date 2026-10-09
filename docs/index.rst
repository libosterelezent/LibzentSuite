SlaughterMudae — Documentation
==============================

Multi-account Discord automation framework: plugins that request services
from a central core, first-class accounts with direct account→plugin
assignments (the capability model), a
four-scope rate-limiting stack, a centralized task pipeline, SQLite
persistence, and two parity'd management surfaces (CLI + dashboard) over
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
   plugins/index
   operations/index
   api/index
   reference/index

Indices
-------

* :ref:`genindex`
* :ref:`modindex`

Where the knowledge lives
-------------------------

* ``knowledgebase/`` is **generated and gitignored** — a fresh clone has
  no ``knowledgebase/`` directory until you run
  ``python3 scripts/gen_knowledgebase.py``.
* ``knowledgebase/read_only/`` — generated READ-ONLY symbol snapshot
  (regenerate with ``python3 scripts/gen_knowledgebase.py``).
* ``knowledgebase/generated/`` — machine-readable indexes
  (``symbol_index.json``, coverage, drift baseline).
* Handwritten ``knowledgebase/*.md`` deep-dive guides (the WHY) are
  maintained separately and are not part of this repository.
* This Sphinx site — navigable API reference (autodoc from the real
  docstrings) + condensed architecture/operations pages.
