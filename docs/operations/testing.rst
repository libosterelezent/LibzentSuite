Testing
=======

Offline-first: 1,600+ tests (``tests/unit``, ``tests/integration``,
``tests/simulation``, ``tests/stress``) run the PRODUCTION composition over
the in-memory ``SimWorld`` — zero network, zero tokens. Determinism comes
from injected manual clocks, sleep seams, and seeded RNGs. Six scenarios
(``export_basic`` … ``estop_halt``) double as living documentation of
intended behavior.

pytest is the runner and xdist parallelizes it (``--dist loadscope`` keeps
each module's tests on one worker):

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
— POSIX permission bits, symlink creation — probes for it and skips with the
reason where it is missing, so the same suite is green on Linux (where CI
gates) and on native Windows. Never skip by platform *name*: a probe skips
exactly where the OS cannot do the thing under test and still runs wherever
it can (Developer Mode on Windows, a mode-less file system on Linux).

A test must not lean on behavior an OS forgives. POSIX lets an open file be
unlinked, which hid a still-open database handle from a temp-directory
cleanup until Windows refused the delete: close handles *inside* the scope
that removes their files.

Two figures are not comparable across platforms: wall-clock timing, and the
benchmark's loop-lag histogram (Windows timers tick near 15.6 ms by default,
so its p50 sits near 20 ms where Linux reports well under a millisecond).
Keep benchmark baselines per platform.

Soak tests are opt-in: ``LZ_SOAK=1`` (fleet soak) and ``LZ_CHAOS=1`` (chaos
matrix).

Suite inventory and techniques: ``knowledgebase/TESTING.md`` (if you
maintain a local knowledgebase — not shipped in a fresh clone).
