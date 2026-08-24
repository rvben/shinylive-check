"""The verdict text and the argument helpers, without a browser.

The text form is what a human reads in a CI log, and every failure kind has its
own headline. None of that needs Chromium, so all of it is pinned here rather
than left to the handful of browser tests that happen to render text.
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

from shinylive_check.check import (
    _MAX_REPORTED_ITEMS,
    _MAX_RETAINED_ITEMS,
    _MAX_SCANNED_CHARS,
    _MAX_TRACKED_LOADS,
    _RESPONSIVE_FLOOR_MILLIS,
    _RESPONSIVE_MILLIS,
    CheckResult,
    ExpectResult,
    _bounded,
    _Bucket,
    _fault,
    _Heard,
    _json_cost,
    _printable,
    _Scan,
    _scan_frames,
    _sweep_millis,
)
from shinylive_check.cli import _heartbeat, _requested_format, render_text
from shinylive_check.schema import FAILURE_KINDS


def verdict(**overrides: Any) -> CheckResult:
    fields: dict[str, Any] = {
        "ok": False,
        "url": "http://127.0.0.1:8000/",
        "seconds": 1.25,
        "outputs": 0,
        "errors": [],
        "output_errors": [],
        "resource_errors": [],
        "ignored_errors": [],
        "expects": [],
        "failure": None,
        "screenshot": None,
    }
    fields.update(overrides)
    # The totals default to the lists' own lengths: a verdict built here is an
    # unclipped one unless a test says otherwise.
    fields.setdefault("errors_total", len(fields["errors"]))
    fields.setdefault("output_errors_total", len(fields["output_errors"]))
    fields.setdefault("resource_errors_total", len(fields["resource_errors"]))
    fields.setdefault("ignored_errors_total", len(fields["ignored_errors"]))
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


def test_an_ignored_console_error_is_shown_on_a_pass():
    """Ignored must not mean hidden, on the same terms a failed load gets.

    `--ignore-console` is how a team gets past one message it has decided about.
    A check that then prints nothing turns that decision into an invisible one: the
    message can change, the app can start printing it for a new reason, and the
    only line saying anything was set aside is in a CI config nobody rereads. The
    flag that produced the exemption is named here for the same reason.
    """
    text = render_text(verdict(ok=True, outputs=1, ignored_errors=["ResizeObserver loop"]))
    assert text.startswith("OK:")
    assert "ResizeObserver loop" in text
    assert "--ignore-console" in text


def test_ignored_console_errors_are_shown_beside_the_ones_that_failed():
    """One exemption does not make the next error quieter."""
    text = render_text(
        verdict(failure="error", errors=["real boom"], ignored_errors=["known noise"])
    )
    assert text.splitlines()[0].startswith("FAIL (error): 1 console/page error(s) at")
    assert "real boom" in text
    assert "known noise" in text


def test_the_ignored_count_is_what_occurred_not_what_is_listed():
    """The same clipping the other lists get, counted the same way.

    A page that prints an ignored message in a loop has its list clipped, and a
    count read off the clipped list would understate it with a plausible number.
    """
    text = render_text(verdict(ok=True, outputs=1, ignored_errors=["one"], ignored_errors_total=9))
    assert "9 console errors ignored" in text


def test_a_navigation_failure_says_the_page_would_not_load():
    assert "could not load the page" in render_text(verdict(failure="navigation"))


def test_an_http_failure_says_the_page_answered_an_error():
    assert "answered an HTTP error" in render_text(verdict(failure="http-status"))


def test_every_declared_failure_kind_has_its_own_headline():
    """A kind the renderer does not know falls back to a headline saying nothing.

    The fallback exists so an unknown kind still renders, which means a kind added
    to the contract and forgotten here degrades silently into "check failed" for
    the one reader who needed to know why. Distinctness is the second half: two
    kinds sharing a headline is the same loss.
    """
    headlines = {kind: render_text(verdict(failure=kind)).splitlines()[0] for kind in FAILURE_KINDS}
    for kind, headline in headlines.items():
        assert "check failed" not in headline, kind
    assert len(set(headlines.values())) == len(FAILURE_KINDS)


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


def test_the_end_of_options_marker_stops_the_format_search():
    """After `--` a token is a target, however much it looks like a flag.

    A directory named `-ojson` is a legal argument there, and argparse reads it as
    the target. Reading it as a format request as well changed what the user saw:
    `shinylive-check -- -ojson extra` on a terminal answered its usage error as a
    JSON envelope, usage block dropped, because of what the directory was called.
    """
    assert _requested_format(["--", "-ojson", "extra"]) is None
    assert _requested_format(["--", "-o", "json"]) is None
    # A request made before the marker still stands, and one after cannot revise
    # it: the marker ends the flags, it does not undo them.
    assert _requested_format(["-o", "json", "--", "-o", "text"]) == "json"


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
        if _printable(chr(code)) == chr(code)
    ]
    assert len(survivors) > 6000, "the sample has to actually cover the range"
    wrong = [(hex(ord(c)), _json_cost(c), len(json.dumps(c)) - 2) for c in survivors]
    assert [entry for entry in wrong if entry[1] != entry[2]] == []


def test_nothing_surviving_sanitising_can_break_the_tools_own_output():
    """Whatever gets through still has to be something the tool can print.

    Every reported string is written to stdout and to JSON, and a lone surrogate
    encodes to neither: it would turn a page's console message into this tool's
    own UnicodeEncodeError, reported as an internal failure against a site that
    was merely unusual. No page has been observed producing one - Chromium hands
    over U+FFFD - so this is not a live hole; it is the reason the filter is a
    question about what a character IS rather than a list of the ones seen so far.
    """
    for char in ("\ud800", "\udfff", "\ud83d"):
        assert _printable(f"a{char}b") == "ab"
    assert _printable("a\ud800b").encode("utf-8") == b"ab"


def test_a_bounded_message_stays_under_its_byte_ceiling_in_any_alphabet():
    """Emoji and ASCII have to hit the same ceiling, which was the F24 defect."""
    for char in ("A", "\U0001f4a5", "é", "\t"):
        reported = _printable(char * 50_000)
        encoded = len(json.dumps(reported)) - 2
        assert encoded < 11_000, f"{char!r} encoded to {encoded} bytes"
        assert "truncated, 50000 characters total" in reported


def test_reading_one_message_is_bounded_as_well_as_reporting_it():
    """The page chooses how long its error is; the tool chooses what reading it costs.

    Deciding what survives is a category lookup per character, and the result was
    built a character at a time, so the whole of a 50 MB console.error was read to
    report ten kilobytes of it: measured at 2743 MB peak against a single such
    message. The scan now stops where the report has to stop anyway.

    Nothing readable is lost by stopping there. A character that survives costs at
    least one byte of the reported ceiling, so the ceiling is reached first unless
    the page pads with characters that are stripped, which buys it no more of the
    verdict than it would have had. The count in the marker stays the page's own,
    because a clipped message must never be read as a short one.
    """
    padded = "​" * (_MAX_SCANNED_CHARS + 1_000) + "boom"
    reported = _printable(padded)
    assert "boom" not in reported, "the whole message was read to find it"
    assert f"truncated, {len(padded)} characters total" in reported


def test_no_page_can_push_a_reported_list_past_its_ceiling():
    """The property, not another scenario.

    This bound has now been relocated three times: capping message length left
    the count unbounded, capping the count left the alphabet unbounded, and each
    time the tests written for the previous shape still passed. Those are
    scenarios; this is the invariant they were each approximating, asserted over
    the shapes an attacker actually picks - one enormous message, many messages,
    a wide alphabet, and the combinations.

    Driven through the accumulator rather than through the renderer alone, since
    that is the path a verdict takes: sanitising moved to the moment a message
    arrives, and a property asserted about `_bounded` on its own would no longer
    be asserted about anything a page can reach.
    """
    alphabets = ("A", '"', "\U0001f4a5", "é", "\n", "\U0001f4a5A")
    shapes = ((1, 5_000_000), (100_000, 50), (5_000, 5_000))
    for char in alphabets:
        for count, length in shapes:
            heard = _Heard(ignore=())
            message = char * length
            for _ in range(count):
                heard.said(message)
            reported = _bounded(heard.buckets()[0])
            encoded = len(json.dumps(reported))
            assert encoded < 600_000, f"{char!r} x{count} of {length} gave {encoded} bytes"


def test_no_page_can_choose_how_much_the_tool_retains():
    """The bound clipping the reported list does not give.

    Clipping bounds what is printed, not what is held. Every message stayed in
    memory in full so that the verdict could count it, so a page erroring in a
    render loop chose the tool's memory: 91801 messages in one check, 1809 MB
    resident, and 23.6 s spent against a 10 s budget. What is kept is bounded now,
    and what is not kept is counted rather than forgotten.

    Both dimensions, because a page picks both and bounding either alone leaves
    the product free: many messages, one enormous message, and enough long ones to
    reach the cap by either route. The ceiling is the retained budget plus the
    reserve the two other kinds can still claim after a flood has spent it, since
    that is the most this tool is allowed to hold; a single kind, which is what
    each case here floods, reaches only the budget. The test below floods all
    three and pins the number exactly.

    Measured in retained characters rather than in bytes of the process, because
    a memory figure is a property of the machine and this is a property of the
    code. The same message object is passed every time, so a version that keeps
    everything holds one string and 20000 references to it: the assertion counts
    what a verdict would have to render, and fails on the defect rather than on
    the memory it would take to reproduce it faithfully.
    """
    ceiling = (_MAX_RETAINED_ITEMS + 2 * _MAX_REPORTED_ITEMS) * _MAX_SCANNED_CHARS
    for count, length in ((20_000, 5_000), (1, 5_000_000), (_MAX_RETAINED_ITEMS, 100_000)):
        heard = _Heard(ignore=())
        message = "E" * length
        for _ in range(count):
            heard.said(message)
        app, resource, ignored = heard.buckets()
        assert app.total == count
        held = sum(len(item.text) for bucket in (app, resource, ignored) for item in bucket.items)
        assert held <= ceiling, f"{count} of {length} left {held} characters retained"


def test_the_counts_stay_exact_when_the_text_is_no_longer_kept():
    """Retaining less must not mean counting less.

    `errors_total` is the field a reader is told to trust over `len(errors)`, so a
    bound that stops keeping a message has to count it on the way out, in the
    bucket it belongs to. Every bucket is filled well past the cap here, and each
    one still reports what actually happened.

    This is what a plausible version of the bound gets wrong: dropping past the
    cap without classifying is simpler, passes the retention test above, and
    reports 500 errors on a page that raised 3000. Measured as a mutation, it
    fails here with the app count short by exactly what was dropped.
    """
    heard = _Heard(ignore=("known noise",))
    for index in range(1_000):
        heard.load_failed(f"http://127.0.0.1/missing-{index}.js")
    for index in range(1_000):
        heard.said("boom")
        heard.said("Failed to load resource", f"http://127.0.0.1/missing-{index}.js")
        heard.said("known noise from a vendor")
    app, resource, ignored = heard.buckets()
    assert (app.total, resource.total, ignored.total) == (1_000, 1_000, 1_000)


def test_a_flood_of_one_kind_does_not_take_another_kind_s_place_in_the_verdict():
    """The list a reader needs is the one a flood is least likely to fill.

    Retention was one budget across every kind, and the kinds do not arrive
    evenly: a broken export fails hundreds of loads while the app raises the one
    error that says why. Spending the whole budget on the loads left the app's own
    error counted and not kept, so the verdict failed the check on an error whose
    text it no longer had, and printed an omission marker where the diagnosis
    should have been. That is the flood scenario the bound exists for, answered by
    discarding exactly the message worth keeping.

    Each kind now has the verdict's own printed ceiling reserved for it, so no
    kind can be starved of what the verdict would show.
    """
    heard = _Heard(ignore=("known noise",))
    for index in range(_MAX_RETAINED_ITEMS):
        url = f"http://127.0.0.1/missing-{index}.js"
        heard.load_failed(url)
        heard.said("Failed to load resource", url)
    heard.said("the app itself is broken")

    app, resource, _ = heard.buckets()

    assert app.total == 1, "the count was never the problem"
    assert [item.text for item in app.items] == ["the app itself is broken"]
    assert _bounded(app) == ["the app itself is broken"], "and it reaches the verdict"
    assert resource.total == _MAX_RETAINED_ITEMS, "the flood is still counted whole"


def test_the_room_each_kind_is_reserved_is_itself_bounded():
    """A reserve is a floor under each kind, not an exemption from the ceiling.

    The obvious way to write the reserve is a full retained budget per kind, which
    passes the test above and hands a page four times the memory the single budget
    was written to deny it. Each answer a message can arrive with is reserved what
    the verdict prints instead, so the worst a page can do is spend the shared
    budget on one and then claim a reserve for each of the other three.

    Flooded in turn rather than interleaved, because that is the arrangement that
    reaches the ceiling: four floods at once split the shared budget between them
    and every reserve is already above its floor, so nothing is left to claim. It
    is also the order the real case arrives in, a boot that fails its loads before
    the app says anything.

    The last flood is located at URLs whose network events never come, so it draws
    on the reserve held for an answer that is not settled yet and joins the app's
    own errors at the verdict. That is why the ceiling counts four reserves while
    the verdict shows three lists, and why the app's list holds two reserves' worth
    while nothing was taken from any other kind's floor.
    """
    heard = _Heard(ignore=("known noise",))
    flood = _MAX_RETAINED_ITEMS * 2
    for index in range(flood):
        url = f"http://127.0.0.1/missing-{index}.js"
        heard.load_failed(url)
        heard.said("Failed to load resource", url)
    for _ in range(flood):
        heard.said("the app itself is broken")
    for _ in range(flood):
        heard.said("known noise from a vendor")
    for index in range(flood):
        heard.said("Refused to execute inline script", f"http://127.0.0.1/blocked-{index}.html")

    app, resource, ignored = heard.buckets()

    held = [len(bucket.items) for bucket in (app, resource, ignored)]
    assert sum(held) == _MAX_RETAINED_ITEMS + 3 * _MAX_REPORTED_ITEMS, f"held {held}"
    assert held == [2 * _MAX_REPORTED_ITEMS, _MAX_RETAINED_ITEMS, _MAX_REPORTED_ITEMS]
    assert [bucket.total for bucket in (app, resource, ignored)] == [2 * flood, flood, flood]


def _flooded(loads: int, *, strict: bool = False) -> tuple[int, int, str | None]:
    """One console error for a failed load, on a page that failed ``loads`` of them."""
    heard = _Heard(ignore=())
    last = ""
    for index in range(loads):
        last = f"http://127.0.0.1/missing-{index}.js"
        heard.load_failed(last)
    heard.said("Failed to load resource: the server responded with a status of 404", last)
    app, resource, _ = heard.buckets()
    scan = _Scan(outputs=1, output_errors=_Bucket([], 0), shiny_detected=True)
    return app.total, resource.total, _fault(heard, scan, strict=strict)


def test_a_bound_on_what_is_remembered_is_not_a_verdict_on_the_page():
    """The same page must not pass under a bound and fail above it.

    Recognising the browser's report of a failed load takes the URL it failed at,
    and that set is bounded, so past the bound the check no longer holds it.
    Reading what it cannot recognise as the app's own error makes the bound decide
    the verdict: the check fails, names an error the app never raised, and prints
    the browser's sentence about a 404 as the app's diagnosis. It also answers a
    question the caller already answered, since failing on a failed load is what
    --fail-on-resource-error is for and it was not asked for here.

    So the bound degrades instead of promoting. Past it, a located message the
    check cannot match is read as the failed load it almost certainly is, which is
    what an unbounded set would have called it: of the messages past a bound on
    failed loads, the ones that put it there are failed loads.

    Degrading is not hiding, which is the last line here: what the check gives up
    is the promotion and not the report, so the message is still counted, still
    listed among the failed loads, and still fails the check for the caller who
    said a failed load is a failure. Trading a false failure for a false pass
    would be the worse of the two.
    """
    assert _flooded(_MAX_TRACKED_LOADS - 1) == (0, 1, None), "the control, under the bound"
    assert _flooded(_MAX_TRACKED_LOADS + 5) == (0, 1, None), "and the bound changes nothing"
    assert _flooded(_MAX_TRACKED_LOADS + 5, strict=True) == (0, 1, "resource-error")


_LOAD_FAILURE = "Failed to load resource: the server responded with a status of 404"


def _flooded_by_failed_loads() -> _Heard:
    """A page that has filled the retention cap with nothing but failed loads.

    The cap is reached deliberately rather than by a shortcut, because what is
    being tested is the seam between what is kept and what is only counted. Every
    message is located and every one of them is a failed load by the time the
    verdict is read, so nothing in this page is the app's own error.

    The reserve is filled first, with its network events arriving after its console
    messages, since that inversion is what makes a located message read as the
    app's own on the way in.
    """
    heard = _Heard(ignore=())
    reserve = [f"http://127.0.0.1/reserve-{index}.js" for index in range(_MAX_REPORTED_ITEMS)]
    for url in reserve:
        heard.said(_LOAD_FAILURE, url)
    for url in reserve:
        heard.load_failed(url)
    for index in range(_MAX_RETAINED_ITEMS - _MAX_REPORTED_ITEMS):
        url = f"http://127.0.0.1/bulk-{index}.js"
        heard.load_failed(url)
        heard.said(_LOAD_FAILURE, url)
    return heard


def _one_too_many(*, load_fails: bool) -> tuple[int, int, str | None]:
    """A located console error past the retention cap, and whether its load failed."""
    heard = _flooded_by_failed_loads()
    extra = "http://127.0.0.1/one-too-many.js"
    heard.said(_LOAD_FAILURE, extra)
    if load_fails:
        heard.load_failed(extra)
    app, resource, _ = heard.buckets()
    scan = _Scan(outputs=1, output_errors=_Bucket([], 0), shiny_detected=True)
    return app.total, resource.total, _fault(heard, scan, strict=False)


def test_a_message_counted_rather_than_kept_is_still_read_at_the_verdict():
    """What is retained and what is only counted must reach the same verdict.

    A located message is read as the app's own until a failed load explains it,
    and the driver does not promise the order the two arrive in, so the lists are
    partitioned again at the verdict against every failed load known by then. A
    message past the retention cap never gets that second reading: it was tallied
    on the way in and the tally is what the verdict is computed from, so its
    first, provisional answer becomes its final one.

    The page here raises nothing of its own. Every message is the browser's report
    of a load that failed, and the last of them is one message past what the check
    retains. Counting that one as the app's own fails a check nobody asked to fail,
    on evidence the check already holds and would have used had the message
    arrived a moment earlier.

    The second case is the control, and it is why this cannot be fixed by reading
    every located message as a failed load: a message located at a URL that no
    failed load ever explains is a blocked script or a refused inline handler, the
    browser rejecting something in the page rather than failing to fetch it, and
    it is still the app's own error whether or not it was the one retained.
    """
    assert _one_too_many(load_fails=True) == (0, _MAX_RETAINED_ITEMS + 1, None)
    assert _one_too_many(load_fails=False) == (1, _MAX_RETAINED_ITEMS, "error")


_DIAGNOSIS = "ReferenceError: Shiny is not defined"


def _app_error_after_a_flood() -> tuple[int, list[str]]:
    """The app's own error, raised once the failed loads have filled the cap."""
    heard = _flooded_by_failed_loads()
    heard.said(_DIAGNOSIS)
    app, _, _ = heard.buckets()
    return app.total, [item.text for item in app.items]


def test_the_reserve_is_held_for_the_kind_a_message_ends_up_in():
    """The one error worth reading must survive a flood of the errors that caused it.

    The reserve exists so that a page failing hundreds of loads cannot spend the
    whole budget before the app raises the one error saying why. A located message
    reads as the app's own until a failed load explains it, so a flood whose
    network events trail its console messages draws on the app's reserve on the
    way in and is moved to the failed loads at the verdict. Leaving the reserve
    spent where the messages no longer are hands the app's share to the loads by
    the back door.

    What that costs is exactly what the reserve was added to prevent: the check
    still fails and still reports one app error, but reports it as a number with
    no text beside it, so the verdict names a failure without naming its cause.
    """
    assert _app_error_after_a_flood() == (1, [_DIAGNOSIS])


class _Counted(list):  # type: ignore[type-arg]
    """A handed-over list that records how far it was actually read.

    What a sweep spends on an over-answering frame is invisible in the verdict,
    which is the same either way: the difference is how many entries were stepped
    through to reach it, so that is what this observes.
    """

    def __init__(self, items: list[Any]) -> None:
        super().__init__(items)
        self.read = 0

    def __iter__(self) -> Any:
        for item in super().__iter__():
            self.read += 1
            yield item


class StubFrame:
    """A frame answering the scan the way the page-side script does.

    It records the budget it was asked for, because that is the thing a sweep
    spends and the one thing that cannot be read back afterwards: the text a
    frame hands over is exactly the cost the bound exists to stop, and what is
    over the ceiling is gone by the time `_scan_frames` returns. A budget of
    `None` is the pre-budget call shape, so a version that asks every frame for
    everything is recorded rather than raising.

    `obeys=False` is a page answering with more than it was asked for, which a
    real one can do: the scan is this tool's script, but it runs in the page's
    context and is built out of things the page is free to replace, so the
    object that comes back is the page's to compose.

    A length accompanies a message only when it was cut, as the real script sends
    it, since the length of a message handed over whole is the length of what
    arrived.
    """

    def __init__(self, errors: int, *, obeys: bool = True, chars: int = 4) -> None:
        self._errors = errors
        self._obeys = obeys
        self._chars = chars
        self.asked: int | None = None

    def evaluate(self, script: str, kept: int | None = None) -> dict[str, Any]:
        self.asked = kept
        handed = self._errors if kept is None or not self._obeys else min(self._errors, kept)
        cut = self._chars > _MAX_SCANNED_CHARS
        said = {"text": "b" * self._chars, "length": self._chars if cut else None}
        return {
            "bound": 0,
            "inputs": 0,
            "rendered": 0,
            "errors": [said] * handed,
            "errorTotal": self._errors,
            "runtime": False,
        }


def test_no_page_can_multiply_the_frame_scan_by_holding_many_frames():
    """The scan's ceiling belongs to a sweep, not to each frame.

    A frame hands over at most the retained ceiling's worth of errors, and the
    page picks how many frames it holds, so the product was still the page's to
    choose: 21 frames of 500 errors at 20000 characters each cost one sweep 6.9 s
    and 116 MB to keep the same 500 items that one frame yields, while a sweep
    runs twice a second. One budget is spent across the frames instead, so a
    frame is asked only for what the frames before it left.

    Measured as the budgets handed out rather than as memory, because a memory
    figure is a property of the machine and this is a property of the code. Both
    controls are in the numbers above: 21 frames holding nothing cost 0.02 s, and
    21 frames of 500 SHORT errors cost 0.44 s, so what is being bounded is the
    text, not the frames and not the count.
    """
    frames = [StubFrame(errors=400), StubFrame(errors=400), StubFrame(errors=400)]

    # A stand-in for a playwright Frame, which only this call's own use of it has
    # to satisfy: the sweep asks a frame to evaluate the scan and reads the object
    # that comes back.
    scan = _scan_frames(cast("list[Any]", frames))

    assert [frame.asked for frame in frames] == [_MAX_RETAINED_ITEMS, 100, 0]
    assert len(scan.output_errors.items) == _MAX_RETAINED_ITEMS
    assert scan.output_errors.total == 1_200, "counting stays whole across every frame"


def test_a_frame_answering_past_its_budget_is_still_held_to_the_ceiling():
    """Asking a page for less is not the same as being given less.

    The budget keeps an honest sweep cheap, and every real frame is honest,
    because the script doing the cutting is this tool's own. It runs in the
    page's context all the same, where every builtin it is made of belongs to
    the page, so what comes back is composed by the thing being judged. The
    ceiling is therefore enforced on this side as well, in both dimensions the
    script was asked to cut, and the counting is taken from the frame's own
    tally either way. A message still reports the size it was written at, since
    that is the frame's account of its own text rather than a measurement of
    what arrived.
    """
    over = _MAX_SCANNED_CHARS * 3
    frames = [
        StubFrame(errors=5_000, obeys=False, chars=over),
        StubFrame(errors=5_000, obeys=False, chars=over),
    ]

    scan = _scan_frames(cast("list[Any]", frames))

    assert len(scan.output_errors.items) == _MAX_RETAINED_ITEMS
    assert max(len(item.text) for item in scan.output_errors.items) == _MAX_SCANNED_CHARS
    assert scan.output_errors.items[0].written == over, "reported at the size it really was"
    assert scan.output_errors.total == 10_000, "counting stays whole"


def test_a_sweep_stops_reading_a_frame_once_it_has_all_it_will_keep():
    """Holding a frame to the ceiling is not the same as stopping at it.

    Keeping only five hundred still read every entry a frame handed over, so a
    frame answering with a million cost the sweep a million steps to keep the five
    hundred it was going to keep from the first five hundred. That is the same
    ceiling the assertion above checks, and the same ceiling this one checks: what
    differs is the work spent reaching it, and a sweep runs twice a second for the
    length of the check.

    The script no longer lets a page grow that list, which is where the ceiling is
    worth the most. This is that ceiling on the side the page does not compose,
    for the reason the retained count is checked here too: a bound that exists in
    one place only is one oversight away from not existing.
    """
    over = _MAX_RETAINED_ITEMS * 4
    handed = _Counted([{"text": "boom", "length": None}] * over)

    class Inflating(StubFrame):
        """Hands over four times the ceiling however little it was asked for."""

        def evaluate(self, script: str, kept: int | None = None) -> dict[str, Any]:
            answer = super().evaluate(script, kept)
            answer["errors"] = handed
            return answer

    scan = _scan_frames(cast("list[Any]", [Inflating(errors=over)]))

    assert len(scan.output_errors.items) == _MAX_RETAINED_ITEMS, "the ceiling still holds"
    assert handed.read <= _MAX_RETAINED_ITEMS + 1, f"read {handed.read} of {over} handed over"


def test_a_frame_that_claims_it_cut_nothing_cannot_hide_what_was_cut_here():
    """The size a message is reported at, when the frame declined to give one.

    A frame sends a length only for a message it cut, so no length means the
    whole of it arrived and its true size is the size of what arrived. A frame
    that says that untruthfully hands over more than the ceiling and calls it
    complete, and the cut on this side then removes text that nothing accounts
    for: the reader is shown a head of a message and told it is the message.
    What the frame withheld cannot be recovered, but what this side removes is
    still measured here, so the verdict says a message was cut and how much of
    it there was to cut.
    """
    over = _MAX_SCANNED_CHARS * 3

    class Silent(StubFrame):
        """Hands over an oversized message and reports no length for it."""

        def evaluate(self, script: str, kept: int | None = None) -> dict[str, Any]:
            answer = super().evaluate(script, kept)
            for error in answer["errors"]:
                error["length"] = None
            return answer

    honest = StubFrame(errors=1, chars=over).evaluate("", 500)["errors"][0]
    assert honest["length"] == over, "the control: this size is one an honest frame reports"

    scan = _scan_frames(cast("list[Any]", [Silent(errors=1, chars=over)]))

    (item,) = scan.output_errors.items
    assert len(item.text) == _MAX_SCANNED_CHARS
    assert item.written == over, "measured here, since the frame accounted for none of it"
    assert "truncated" in _printable(item.text, item.written)


def test_a_liveness_sweep_is_bounded_by_the_budget_but_never_to_nothing():
    """What one sweep may spend asking whether the page is still answering.

    Bounded by what is left of the budget, so a page cannot buy time by holding
    many frames rather than by wedging one: frames are asked in turn until the
    sweep's share is gone.

    Floored all the same, because the last sweep of a check runs at or past its
    own deadline - the settle window alone can carry it there - and a live frame
    asked to answer within the zero milliseconds remaining would be read as wedged,
    reporting an app that had rendered as one that never did.
    """
    now = time.monotonic()
    assert _sweep_millis(now + 60) == _RESPONSIVE_MILLIS
    assert _sweep_millis(now - 5) == _RESPONSIVE_FLOOR_MILLIS
    half = _sweep_millis(now + (_RESPONSIVE_MILLIS / 2) / 1000)
    assert _RESPONSIVE_FLOOR_MILLIS < half <= _RESPONSIVE_MILLIS / 2, half


def test_the_heartbeat_reports_no_more_often_than_its_interval():
    written: list[str] = []
    report = _heartbeat(15.0, written.append)
    for elapsed in (0.5, 5.0, 14.9, 15.0, 20.0, 30.0):
        report(elapsed)
    assert written == ["still waiting (15s elapsed)\n", "still waiting (30s elapsed)\n"]
