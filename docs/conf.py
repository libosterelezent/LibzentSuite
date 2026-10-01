# SlaughterMudae documentation build configuration (dev-only dependency).
#
# The RUNTIME project is stdlib-only; Sphinx + myst-parser are documentation
# tools installed separately (see requirements-dev.txt):
#   pip install --user sphinx myst-parser
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(".."))

project = "SlaughterMudae"
author = "SlaughterMudae contributors"
copyright = "2026, SlaughterMudae contributors"  # noqa: A001
release = "1.0.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

# Markdown (handwritten knowledge base excerpts) + RST mixed site
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
master_doc = "index"

# API reference quality
autodoc_member_order = "bysource"
autodoc_default_options = {
    "show-inheritance": True,
}
autoclass_content = "class"
napoleon_numpy_docstring = False
intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
html_theme = "alabaster"
html_static_path = []

myst_heading_anchors = 3
