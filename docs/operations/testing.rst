Testing
=======

Offline-first: 1,600+ tests (``tests/unit``, ``tests/integration``,
``tests/simulation``, ``tests/stress``) run the PRODUCTION composition over
the in-memory ``SimWorld`` — zero network, zero tokens. Determinism comes
from injected manual clocks, sleep seams, and seeded RNGs. Six scenarios
(``export_basic`` … ``estop_halt``) double as living documentation of
intended behavior.

pytest is the runner and xdist parallelizes it (``--dist loadscope`` groups
test functions by module and test methods by class, so one class's methods
always share a worker — two classes from the same module may not):

.. code-block:: bash

   scripts/run_tests.sh                 # the locked environment, serial
   scripts/run_tests.sh -n auto         # parallel
   scripts/local-ci.sh quick            # lint, format, export sync, pyright ratchet
   scripts/local-ci.sh                  # the full gate — what CI runs
   python3 main.py --sim export_basic   # one scenario, end to end

The two shell scripts are bash: on Windows run them from Git Bash (they come
with Git for Windows).

One suite, every platform
-------------------------

The suite is platform-independent. A test that depends on an OS capability
— POSIX permission bits, symlink creation — probes for that capability and
skips with the reason where it is missing, so the same suite is green on
Linux and on native Windows. CI runs it on Windows (the owner's
platform) on 3.10; the local gate proves the same suite on any machine.
The probes live in ``tests/unit/helpers.py``
(``can_symlink``, ``enforces_file_modes``) and test the capability rather
than the platform's name: they skip exactly where the OS cannot do the
thing under test, and still run wherever it can (Developer Mode on
Windows, a mode-less file system on Linux).

The suite also avoids leaning on behavior an OS forgives. POSIX lets an
open file be unlinked, which once hid a still-open database handle from a
temp-directory cleanup until Windows refused the delete; such handles are
now closed inside the scope that removes their files.

Two figures are not comparable across platforms: wall-clock timing, and the
benchmark's loop-lag histogram (Windows timers tick near 15.6 ms by default,
so its p50 sits near 20 ms where Linux reports well under a millisecond).
Benchmark baselines are therefore kept per platform.

Waits in tests are on observables, not the clock: poll the published
event, the recorded call, or the set flag with ``wait_until_async``
(in ``tests/unit/helpers.py``) until it turns true or a deadline
passes. A fixed ``asyncio.sleep`` survives only where the sleep IS the
property under test — staging with generous margins, negative-check
windows (asserting nothing happened), and tolerance-bound timing gaps.
Every test also carries a 120 s per-test ceiling (``pytest-timeout``,
thread method): a hang fails by name with every thread's stack dumped,
instead of a leg dying at its ceiling with no attribution.

Soak tests are opt-in: ``LZ_SOAK=1`` (fleet soak) and ``LZ_CHAOS=1`` (chaos
matrix).

Suite inventory and techniques: ``knowledgebase/TESTING.md`` (if you
maintain a local knowledgebase — not shipped in a fresh clone).
