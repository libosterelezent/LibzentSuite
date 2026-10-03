Testing
=======

Offline-first: 705 tests (``tests/unit``, ``tests/integration``,
``tests/simulation``) run the PRODUCTION composition over the in-memory
``SimWorld`` — zero network, zero tokens. Determinism comes from injected
manual clocks, sleep seams, and seeded RNGs. Six scenarios
(``soulcopy_basic`` … ``control_mention``) double as living documentation
of intended behavior.

.. code-block:: bash

   bash scripts/run_tests.sh
   python3 main.py --sim soulcopy_basic

Suite inventory and techniques: ``knowledgebase/TESTING.md``.
