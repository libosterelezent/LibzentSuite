Testing
=======

Offline-first: 1094+ tests (``tests/unit``, ``tests/integration``,
``tests/simulation``) run the PRODUCTION composition over the in-memory
``SimWorld`` — zero network, zero tokens. Determinism comes from injected
manual clocks, sleep seams, and seeded RNGs. Seven scenarios
(``export_basic`` … ``estop_halt``) double as living documentation
of intended behavior.

.. code-block:: bash

   bash scripts/run_tests.sh
   python3 main.py --sim export_basic

Suite inventory and techniques: ``knowledgebase/TESTING.md`` (if you
maintain a local knowledgebase — not shipped in a fresh clone).
