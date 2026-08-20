"""The verdict text and the argument helpers, without a browser.

The text form is what a human reads in a CI log, and every failure kind has its
own headline. None of that needs Chromium, so all of it is pinned here rather
than left to the handful of browser tests that happen to render text.
"""

from __future__ import annotations

from typing import Any

from shinylive_check.check import CheckResult, ExpectResult
from shinylive_check.cli import _heartbeat, _requested_format, render_text


def verdict(**overrides: Any) -> CheckResult:
    fields: dict[str, Any] = {
        "ok": False,
        "url": "http://127.0.0.1:8000/",
        "seconds": 1.25,
        "outputs": 0,
        "errors": [],
        "output_errors": [],
        "expects": [],
        "failure": None,
        "screenshot": None,
    }
    fields.update(overrides)
    # The totals default to the lists' own lengths: a verdict built here is an
    # unclipped one unless a test says otherwise.
    fields.setdefault("errors_total", len(fields["errors"]))
    fields.setdefault("output_errors_total", len(fields["output_errors"]))
    return CheckResult(**fields)


def test_a_single_output_is_not_pluralized():
    line = render_text(verdict(ok=True, outputs=1, failure=None))
    assert line == "OK: 1 output rendered in 1.2s at http://127.0.0.1:8000/"


def test_several_outputs_are_pluralized():
    assert "2 outputs rendered" in render_text(verdict(ok=True, outputs=2))


def test_a_pass_reports_how_many_expect_selectors_were_satisfied():
    line = render_text(
        verdict(ok=True, outputs=1, expects=[ExpectResult("#a", True), ExpectResult("#b", True)])
    )
    assert "(2 expect selector(s) satisfied)" in line


def test_console_errors_are_counted_in_the_headline():
    text = render_text(verdict(failure="error", errors=["one", "two"]))
    assert text.splitlines()[0].startswith("FAIL (error): 2 console/page error(s) at")


def test_output_errors_are_counted_in_the_headline():
    text = render_text(verdict(failure="output-error", output_errors=["boom"]))
    assert text.splitlines()[0].startswith("FAIL (output-error): 1 Shiny output error(s) at")


def test_a_timeout_reports_the_time_actually_waited():
    # `seconds` is wall time including browser launch, so it can exceed --timeout.
    # The headline must not claim a window the number then contradicts.
    text = render_text(verdict(failure="timeout", seconds=3.6))
    assert text.splitlines()[0] == (
        "FAIL (timeout): app did not render (waited 3.6s) at http://127.0.0.1:8000/"
    )


def test_a_navigation_failure_says_the_page_would_not_load():
    assert "could not load the page" in render_text(verdict(failure="navigation"))


def test_an_http_failure_says_the_page_answered_an_error():
    assert "answered an HTTP error" in render_text(verdict(failure="http-status"))


def test_an_unknown_failure_kind_still_renders_a_headline():
    assert "check failed" in render_text(verdict(failure="something-new"))


def test_every_line_of_page_text_is_indented_under_the_verdict():
    text = render_text(verdict(failure="error", errors=["first\nsecond"]))
    assert text.splitlines()[1:] == ["  first", "  second"]


def test_unsatisfied_expect_selectors_are_listed():
    text = render_text(verdict(failure="timeout", expects=[ExpectResult("#gone", False)]))
    assert "  expect not satisfied: #gone" in text


def test_the_screenshot_path_is_reported_when_one_was_captured():
    assert "  screenshot: /tmp/shot.png" in render_text(
        verdict(failure="error", errors=["x"], screenshot="/tmp/shot.png")
    )


def test_the_requested_format_is_read_from_every_accepted_spelling():
    assert _requested_format(["-o", "json", "site"]) == "json"
    assert _requested_format(["--output", "text", "site"]) == "text"
    assert _requested_format(["--output=json", "site"]) == "json"
    assert _requested_format(["-ojson", "site"]) == "json"


def test_an_absent_or_unusable_format_request_reads_as_unset():
    assert _requested_format(["site"]) is None
    assert _requested_format(["-o", "auto", "site"]) is None
    assert _requested_format(["-o"]) is None
    assert _requested_format(["--output=nonsense"]) is None


def test_the_heartbeat_reports_no_more_often_than_its_interval():
    written: list[str] = []
    report = _heartbeat(15.0, written.append)
    for elapsed in (0.5, 5.0, 14.9, 15.0, 20.0, 30.0):
        report(elapsed)
    assert written == ["still waiting (15s elapsed)\n", "still waiting (30s elapsed)\n"]
