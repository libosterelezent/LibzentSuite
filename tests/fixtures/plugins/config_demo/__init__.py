"""Config Demo fixture plugin: the Dashboard UI API reference example.

A realistic configuration page built entirely with the builder-based
Dashboard UI API — every pattern a plugin author needs in one file
(tabs, cards, all eight input types, a validated form, data-backed
table/list, metrics, progress, confirmation + disabled buttons and a
visibility check). See ``plugin.py``.
"""
from .plugin import ConfigDemoPlugin

__all__ = ["ConfigDemoPlugin"]
