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
   plans/index
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

* ``knowledgebase/`` — the deep knowledge base, LOCAL-ONLY (gitignored,
  not in a fresh clone): handwritten ``*.md`` deep-dive guides (the WHY)
  plus generated artifacts. It lives on the machines that built it; the
  repository itself carries ``docs/`` as the shared record.
* ``knowledgebase/read_only/`` — generated READ-ONLY symbol snapshot
  (regenerate with ``python3 scripts/gen_knowledgebase.py``).
* ``knowledgebase/generated/`` — machine-readable indexes
  (``symbol_index.json``, coverage, drift baseline).
* ``dump/`` — the retirement wing (also local-only): stale code
  preserved verbatim; its README stays tracked as the pointer.
* This Sphinx site — navigable API reference (autodoc from the real
  docstrings) + condensed architecture/operations pages.
