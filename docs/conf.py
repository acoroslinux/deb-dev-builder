from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

project = "Deb-Dev-Builder"
author = "Deb-Dev-Builder contributors"
release = "1.0"
extensions = ["myst_parser", "sphinx.ext.autodoc", "sphinx.ext.napoleon"]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build"]
html_theme = "alabaster"
html_title = "Deb-Dev-Builder Manual"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
