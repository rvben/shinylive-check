"""A fixture app whose requirements.txt names a package that does not exist.

micropip fails to resolve it inside pyodide, so the app never starts even though
the export itself succeeds; the checker must fail on it.
"""

from shiny import App, render, ui

app_ui = ui.page_fluid(ui.output_text("txt"))


def server(input, output, session):
    @render.text
    def txt():
        return "never reached"


app = App(app_ui, server)
