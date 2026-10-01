Plugin system
=============

Plugins are self-describing modules (metadata + ``Setting`` manifest +
lifecycle hooks + ``handle_event``). They never manage accounts, the
database, or rate limiting — they *request services*.

.. toctree::
   :maxdepth: 2

   plugin_api
   writing_plugins
