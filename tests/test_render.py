"""The verdict text and the argument helpers, without a browser.

The text form is what a human reads in a CI log, and every failure kind has its
own headline. None of that needs Chromium, so all of it is pinned here rather
than left to the handful of browser tests that happen to render text.
"""

from __future__ import annotations

import json
from typing import Any

from shinylive_check.check import (
    _KEEP_CONTROLS,
    CheckResult,
    ExpectResult,
    _bounded,
    _json_cost,
    _printable,
)
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
    # A rendered output is itself proof the runtime ran, so the default is derived
    # rather than fixed: a verdict built here is never internally contradictory.
    fields.setdefault("shiny_detected", fields["outputs"] > 0)
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


def test_a_timeout_with_no_runtime_names_the_runtime_as_the_reason():
    """A timeout whose selectors all matched is otherwise unreadable.

    The gate can fail a page every `--expect` selector was satisfied on, and
    "app did not render" alongside a list of satisfied selectors reads as a bug in
    the tool. The verdict has to say which half of the condition was missing, or
    the user goes off retuning the selector that already worked.
    """
    text = render_text(
        verdict(
            failure="timeout",
            shiny_detected=False,
            expects=[ExpectResult("#ready", True)],
            seconds=5.0,
        )
    )
    assert "no Shiny runtime detected" in text


def test_a_timeout_that_did_see_the_runtime_does_not_blame_it():
    """The control: the line above must be evidence, not decoration."""
    text = render_text(
        verdict(failure="timeout", shiny_detected=True, expects=[ExpectResult("#late", False)])
    )
    assert "no Shiny runtime detected" not in text


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


def test_the_json_cost_model_agrees_with_the_encoder_it_predicts():
    """The size bound is only as honest as this arithmetic.

    `_printable` spends a byte budget using `_json_cost` rather than encoding
    each candidate prefix, so if the model ever disagreed with `json.dumps` the
    verdict would quietly exceed its stated ceiling again. Every character that
    survives stripping is checked against the real encoder, which is the only
    authority on what a character costs.
    """
    survivors = [
        chr(code)
        for code in [*range(0, 0x2000), 0x1F4A5, 0x1F600, 0x10FFFF]
        if chr(code) in _KEEP_CONTROLS or not (code < 0x20 or 0x7F <= code <= 0x9F)
    ]
    assert len(survivors) > 8000, "the sample has to actually cover the range"
    wrong = [(hex(ord(c)), _json_cost(c), len(json.dumps(c)) - 2) for c in survivors]
    assert [entry for entry in wrong if entry[1] != entry[2]] == []


def test_a_bounded_message_stays_under_its_byte_ceiling_in_any_alphabet():
    """Emoji and ASCII have to hit the same ceiling, which was the F24 defect."""
    for char in ("A", "\U0001f4a5", "é", "\t"):
        reported = _printable(char * 50_000)
        encoded = len(json.dumps(reported)) - 2
        assert encoded < 11_000, f"{char!r} encoded to {encoded} bytes"
        assert "truncated, 50000 characters total" in reported


def test_no_page_can_push_a_reported_list_past_its_ceiling():
    """The property, not another scenario.

    This bound has now been relocated three times: capping message length left
    the count unbounded, capping the count left the alphabet unbounded, and each
    time the tests written for the previous shape still passed. Those are
    scenarios; this is the invariant they were each approximating, asserted over
    the shapes an attacker actually picks - one enormous message, many messages,
    a wide alphabet, and the combinations.
    """
    alphabets = ("A", '"', "\U0001f4a5", "é", "\n", "\U0001f4a5A")
    shapes = ((1, 5_000_000), (100_000, 50), (5_000, 5_000))
    for char in alphabets:
        for count, length in shapes:
            reported = _bounded([char * length] * count)
            encoded = len(json.dumps(reported))
            assert encoded < 600_000, f"{char!r} x{count} of {length} gave {encoded} bytes"


def test_the_heartbeat_reports_no_more_often_than_its_interval():
    written: list[str] = []
    report = _heartbeat(15.0, written.append)
    for elapsed in (0.5, 5.0, 14.9, 15.0, 20.0, 30.0):
        report(elapsed)
    assert written == ["still waiting (15s elapsed)\n", "still waiting (30s elapsed)\n"]
