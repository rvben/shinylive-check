"""The green fixture: a minimal app that boots and renders one text output."""

from shiny import App, render, ui

app_ui = ui.page_fluid(
    ui.h1("shinylive-check green fixture"),
    ui.input_slider("n", "Multiplier", min=1, max=50, value=10),
    ui.output_text("txt"),
)


def server(input, output, session):
    @render.text
    def txt():
        return f"n times two is {input.n() * 2}"


app = App(app_ui, server)
