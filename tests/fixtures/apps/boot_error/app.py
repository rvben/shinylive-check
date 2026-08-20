"""A fixture app that raises while booting; the checker must fail on it.

The shiny import matters: shinylive's export bundles the wheels for the imports
it finds in app.py, so without it the export has no shiny at all and the failure
becomes a missing-wheel fetch error instead of the boot exception under test.
"""

from shiny import App, ui

raise RuntimeError("boom: shinylive-check boot_error fixture")
