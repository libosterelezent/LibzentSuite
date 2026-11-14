Plugin system
=============

Plugins are self-describing modules (metadata + ``Setting`` manifest +
lifecycle hooks + ``handle_event`` + optional declared self-commands and
chain declarations). They never manage accounts, the database, or
rate limiting — they *request services*. The framework ships with NO
bundled plugins; the working example is the test fixture
``tests/fixtures/plugins/probe/``.

.. toctree::
   :maxdepth: 2

   plugin_api
   writing_plugins
