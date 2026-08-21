"""Drive an exported Shinylive site in headless Chromium and judge the boot.

Shinylive failures are silent on the server side: the export succeeds, the deploy
succeeds, every asset serves with a 200, and the page is dead anyway because
something broke inside the browser (a pyodide crash, a micropip resolution
failure, an app exception at boot). Only a real browser observes that, so this
module boots one and watches three surfaces: uncaught page errors, console
errors, and the Shiny output elements inside the app frame.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import ConsoleMessage, Frame, Page, Response, sync_playwright
from playwright.sync_api import Error as PlaywrightError


class BrowserMissingError(RuntimeError):
    """Chromium is not installed for this playwright; the environment is unusable."""


class SelectorError(ValueError):
    """An --expect selector is not valid CSS."""


# An export may put the app in an iframe, so every frame is scanned; selectors
# evaluated only on the main frame would silently match nothing there. Outputs
# carrying shiny-output-error are the app saying a render failed; outputs with
# content are the render succeeding.
#
# `runtime` and the two element counts are the evidence that the Shiny runtime is
# actually alive. Shiny defines window.Shiny and adds every shiny-bound-* class from
# JavaScript, so none of the three can appear in the static shell an export serves
# before pyodide boots: they separate a booting app from a dead one, which nothing
# in the shell's own HTML can do.
_FRAME_SCAN = """
() => {
  const outs = Array.from(document.querySelectorAll(".shiny-bound-output"));
  const rendered = outs.filter(
    (el) => !el.classList.contains("shiny-output-error") && el.innerHTML.trim().length > 0
  ).length;
  const errors = [];
  for (const el of document.querySelectorAll(".shiny-output-error")) {
    const text = (el.textContent || "").trim();
    if (text) errors.push(text);
  }
  return {
    bound: outs.length,
    inputs: document.querySelectorAll(".shiny-bound-input").length,
    rendered,
    errors,
    runtime: typeof window.Shiny !== "undefined",
  };
}
"""

_POLL_MILLIS = 500
_SETTLE_MILLIS = 500

# How long one sweep may spend proving the page is still answering before it is
# treated as wedged. A frame that is alive answers in single-digit milliseconds,
# so this is three orders of magnitude more room than the question needs.
_RESPONSIVE_MILLIS = 2000

# The least a sweep is ever given, however little of the budget is left. The last
# sweep of a check runs at or past its own deadline, and asking a live page to
# answer within the zero milliseconds remaining would report an app that had
# rendered as one that never did.
_RESPONSIVE_FLOOR_MILLIS = 250

# Newlines and tabs survive because tracebacks need them, and because a line
# break is the one piece of formatting the page is allowed to ask for: `_detail`
# grants it by indenting the new line, so it cannot reach column 0.
_KEEP_CONTROLS = frozenset("\n\t")

# What is dropped is decided by what a character IS, not by the ranges anyone
# thought to list. `Cc` control, `Cf` format - which is where the bidi overrides
# live, and reversing the order a line reads in is screen control even though
# every byte of the text is intact - and `Cs`, whose members encode to neither
# UTF-8 nor JSON and would make a page's console message this tool's own crash.
_STEERING_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

# The page decides how long its errors are, so the tool has to decide how long its
# verdict is. Comfortably more than any real traceback, far less than a CI log
# budget. The head is kept because that is where the diagnosis lives.
#
# Spent in ENCODED bytes, not characters: the verdict is JSON, which escapes
# non-ASCII, so one emoji costs twelve bytes and a character cap bounds nothing a
# CI job actually stores.
_MAX_REPORTED_BYTES = 10_000

# Bounding each message is not bounding the verdict: an app erroring inside a
# render or reconnect loop emits thousands before the first poll sees any of them,
# and the count then carries the size the length cap just removed. The earliest
# errors are kept because a cascade's root cause is at its start.
_MAX_REPORTED_ITEMS = 50


def _json_cost(char: str) -> int:
    """Bytes this character costs once JSON-encoded, ASCII-safe.

    Exact for the alphabet that survives ``_printable``'s stripping, which is
    what it is used on. The three characters where this would disagree with
    ``json.dumps`` (backspace, form feed, DEL) are all stripped before costing,
    and a test pins that agreement across the whole surviving range.
    """
    code = ord(char)
    if code > 0xFFFF:
        # Encoded as a surrogate pair, so two \uXXXX escapes.
        return 12
    if code > 0x7F:
        return 6
    if char in '"\\':
        return 2
    if code < 0x20:
        return 2
    return 1


def _printable(text: str) -> str:
    """Strip control characters from text the checked page chose, and bound it.

    Console messages and page errors are written by the page, and the verdict
    they land in is read in a terminal. Escape sequences there can erase lines
    and move the cursor, and a bidi override can reverse the order the rest of a
    line is read in without touching its bytes, so a hostile or broken page could
    repaint the check's own verdict. Text is reported; control of the screen is
    not.

    Size is bounded for the same reason: an app that dumps a large repr into
    console.error would otherwise choose the size of this tool's stdout, and the
    verdict is what a CI job stores and a script parses. The budget is spent in
    encoded bytes, so the ceiling does not depend on the alphabet the page picked.
    The reported size is the size before truncation, so a clipped error is never
    read as a short one.
    """
    clean = "".join(
        char
        for char in text
        if char in _KEEP_CONTROLS or unicodedata.category(char) not in _STEERING_CATEGORIES
    )
    spent = 0
    for index, char in enumerate(clean):
        spent += _json_cost(char)
        if spent > _MAX_REPORTED_BYTES:
            return f"{clean[:index]}... [truncated, {len(clean)} characters total]"
    return clean


def _bounded(items: list[str]) -> list[str]:
    """Sanitize a page-controlled list, keeping its head and naming what was cut.

    The companion count (``errors_total``) is what keeps this honest: a reader
    must never mistake the length of a clipped list for the number of things that
    actually went wrong.
    """
    kept = [_printable(item) for item in items[:_MAX_REPORTED_ITEMS]]
    dropped = len(items) - len(kept)
    if dropped:
        kept.append(f"... [{dropped} further omitted, {len(items)} total]")
    return kept


@dataclass
class ExpectResult:
    selector: str
    satisfied: bool


@dataclass
class _Said:
    """One message about the page, with where the browser located it.

    ``origin`` is the URL a console error was located at, and is set only when the
    error carried no console arguments. That pair is the shape Chromium
    synthesises for a load it could not complete: no arguments, located at the
    resource rather than in the page's own script. Anything the app printed itself
    carries what console.error was called with, so the two never collide.

    Deliberately not a text match. Chromium's wording for a failed load is fixed,
    so a page that printed that sentence could otherwise silence its own errors.

    ``from_page`` is false for the two messages the CHECK writes rather than the
    page: a navigation that failed and an error status. They travel in this list
    because the verdict's ``errors`` is where a reader looks for why a check
    failed, but they are this tool's own words about a page that never ran, so no
    exemption may move them. ``net::ERR_`` is a plausible pattern to write for a
    third-party beacon, and it appears verbatim in the message about the site
    itself being unreachable.
    """

    text: str
    origin: str | None = None
    from_page: bool = True


@dataclass
class _Scan:
    """What one sweep of every frame saw."""

    outputs: int
    output_errors: list[str]
    # window.Shiny, a bound input or a bound output: any one of them is the runtime
    # having executed, and none of them can come from the static shell.
    shiny_detected: bool


@dataclass
class CheckResult:
    ok: bool
    url: str
    seconds: float
    outputs: int
    shiny_detected: bool
    errors: list[str]
    # The number that OCCURRED, which is not len(errors) once the list is clipped.
    errors_total: int
    output_errors: list[str]
    output_errors_total: int
    resource_errors: list[str]
    resource_errors_total: int
    ignored_errors: list[str]
    ignored_errors_total: int
    expects: list[ExpectResult]
    failure: str | None
    screenshot: str | None


def run_check(
    url: str,
    *,
    timeout: float = 180.0,
    expects: tuple[str, ...] = (),
    screenshot: str | None = None,
    fail_on_resource_error: bool = False,
    ignore_console: tuple[str, ...] = (),
    progress: Callable[[float], None] | None = None,
) -> CheckResult:
    """Load ``url``, wait for the app to render, and return the verdict.

    Passing means: the page loaded, no console or page error occurred, no Shiny
    output is in an error state, the Shiny runtime was detected on the page, and
    either every ``expects`` selector was visible (when given) or at least one
    Shiny output rendered non-empty content (the default). A resource the browser
    could not load is reported but does not fail the check unless
    ``fail_on_resource_error`` is set. A console or page error containing one of
    the ``ignore_console`` substrings is reported separately and does not fail the
    check either. ``screenshot`` is captured only on failure. ``progress`` is
    called with the elapsed seconds on every poll, for callers reporting a wait.

    An empty ``ignore_console`` pattern raises ``ValueError``. The CLI refuses one
    with a hint before it launches a browser; this keeps the same promise for
    anyone calling ``run_check`` directly, since the alternative is that importing
    this function is a way around the invariant it exists to hold.
    """
    if any(not pattern for pattern in ignore_console):
        # Every string contains the empty one, so accepting it would silently turn
        # a named exemption into "ignore every console error", and a broken app
        # would then be reported as healthy rather than as unchecked.
        raise ValueError(
            "ignore_console patterns must not be empty; the empty string is in every "
            "message and would exempt every console error"
        )

    start = time.monotonic()
    said: list[_Said] = []
    # Every request that actually failed, by URL: a load with no response at all
    # (a refused connection, a dead host) and a response that carried an error
    # status are both failed loads, and only one of them produces a response.
    failed_loads: set[str] = set()

    def result(
        *,
        ok: bool,
        outputs: int = 0,
        shiny_detected: bool = False,
        output_errors: list[str] | None = None,
        expect_state: dict[str, bool] | None = None,
        failure: str | None = None,
        shot: str | None = None,
    ) -> CheckResult:
        state = expect_state or dict.fromkeys(expects, False)
        page_errors = output_errors or []
        app_errors, resource_errors, ignored_errors = _partition(said, failed_loads, ignore_console)
        return CheckResult(
            ok=ok,
            url=url,
            seconds=round(time.monotonic() - start, 1),
            outputs=outputs,
            shiny_detected=shiny_detected,
            errors=_bounded(app_errors),
            errors_total=len(app_errors),
            output_errors=_bounded(page_errors),
            output_errors_total=len(page_errors),
            resource_errors=_bounded(resource_errors),
            resource_errors_total=len(resource_errors),
            ignored_errors=_bounded(ignored_errors),
            ignored_errors_total=len(ignored_errors),
            expects=[ExpectResult(sel, done) for sel, done in state.items()],
            failure=failure,
            screenshot=shot,
        )

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as err:
            if "playwright install" in str(err) or "Executable doesn't exist" in str(err):
                raise BrowserMissingError(str(err)) from err
            raise

        interrupted = False
        try:
            page = browser.new_page()

            def on_console(msg: ConsoleMessage) -> None:
                if msg.type != "error":
                    return
                said.append(_Said(msg.text, None if msg.args else msg.location.get("url")))

            def on_response(response: Response) -> None:
                if response.status >= 400:
                    failed_loads.add(response.url)

            page.on("pageerror", lambda err: said.append(_Said(f"pageerror: {err}")))
            page.on("console", on_console)
            page.on("requestfailed", lambda request: failed_loads.add(request.url))
            page.on("response", on_response)

            _validate_selectors(page, expects)

            deadline = start + timeout
            try:
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=_remaining_millis(deadline)
                )
            except PlaywrightError as err:
                said.append(_Said(str(err), from_page=False))
                return result(ok=False, failure="navigation", shot=_shoot(page, screenshot))
            if response is not None and response.status >= 400:
                said.append(_Said(f"HTTP {response.status} for {url}", from_page=False))
                return result(ok=False, failure="http-status", shot=_shoot(page, screenshot))

            expect_state = dict.fromkeys(expects, False)
            scan = _Scan(outputs=0, output_errors=[], shiny_detected=False)

            while True:
                answering = _answering(page, deadline)
                scan = _scan_frames(answering)
                _scan_expects(answering, expect_state)
                failure = _fault(
                    said, failed_loads, scan, strict=fail_on_resource_error, ignore=ignore_console
                )

                if failure is None and _satisfied(scan, expect_state, expects):
                    # Let the page settle briefly: a failure right after the first
                    # render still fails the check instead of racing it. Console
                    # errors arrive as events, but an output that fails inside the
                    # window is only visible to another scan, so the page is
                    # re-scanned rather than just re-read.
                    page.wait_for_timeout(_SETTLE_MILLIS)
                    answering = _answering(page, deadline)
                    scan = _scan_frames(answering)
                    _scan_expects(answering, expect_state)
                    failure = _fault(
                        said,
                        failed_loads,
                        scan,
                        strict=fail_on_resource_error,
                        ignore=ignore_console,
                    )
                    if failure is None and not _satisfied(scan, expect_state, expects):
                        # It held, then stopped holding. That is not a pass, and it
                        # is not yet a failure either: the app may simply be
                        # mid-rerender, so the condition has to be met again -
                        # while there is budget left to meet it in. Waiting again
                        # without asking the clock is how a page that renders and
                        # unrenders on every scan outlasted its own timeout.
                        if time.monotonic() <= deadline:
                            continue
                        failure = "timeout"
                elif failure is None:
                    if time.monotonic() > deadline:
                        failure = "timeout"
                    else:
                        if progress is not None:
                            progress(time.monotonic() - start)
                        page.wait_for_timeout(_POLL_MILLIS)
                        continue

                ok = failure is None
                return result(
                    ok=ok,
                    outputs=scan.outputs,
                    shiny_detected=scan.shiny_detected,
                    output_errors=scan.output_errors,
                    expect_state=expect_state,
                    failure=failure,
                    shot=None if ok else _shoot(page, screenshot),
                )
        except KeyboardInterrupt:
            # Closing the browser is a request-and-reply over the connection the
            # interrupt just unwound, so the reply never comes and Ctrl-C hangs
            # instead of stopping the check. Leaving is safe: the driver exits
            # with this process and takes the browser with it.
            interrupted = True
            raise
        finally:
            if not interrupted:
                browser.close()


def _remaining_millis(deadline: float) -> int:
    """What is left of the caller's budget, as a playwright timeout.

    Never zero: playwright reads a zero timeout as "wait forever", so a budget
    that has just run out would become an unbounded wait, which is the opposite of
    what a timeout means. The CLI rejects a non-positive ``--timeout`` outright;
    this keeps the same promise for anyone calling ``run_check`` directly.
    """
    return max(1, int((deadline - time.monotonic()) * 1000))


def _partition(
    said: list[_Said], failed_loads: set[str], ignore: tuple[str, ...] = ()
) -> tuple[list[str], list[str], list[str]]:
    """Split what the page said into the app's own errors, failed loads, and exemptions.

    Matched by URL when a verdict is wanted rather than as each message arrives,
    so a network event landing between the console error and that verdict is
    still in evidence. The slack is not what makes the classification safe. A
    console error for a failed load is written by the renderer after it is told
    the load failed, and both events reach this process over one ordered
    connection, so the network event is seen first: measured at 250 of 250 failed
    resources, 404s (which arrive as a response) and refused connections (which
    arrive as `requestfailed`) alike. The slack covers the case where that ceases
    to hold, and covers it only as far as the next verdict, because a message
    still unmatched then is read as the app's own.

    Shape alone would not do instead of the URL match. A blocked inline script is
    the same shape, no console arguments and located at a URL, and no network
    event ever arrives to explain it, which is correct: the browser refused
    something in the page rather than failing to fetch it.

    ``ignore`` is read only against what the page itself said, and only against
    what is left after that split. A pattern written for the app's own words
    cannot reach a failed load: those have their own switch, and the message they
    carry is the browser's wording rather than the app's. Nor can it reach the
    check's own account of a navigation failure or an error status, which is not
    a console error and is never ignored, since it is what ended the check. The
    match is a plain case-sensitive substring, so what is exempted is legible
    from the command line that exempted it.
    """
    app: list[str] = []
    resource: list[str] = []
    ignored: list[str] = []
    for item in said:
        if item.origin is not None and item.origin in failed_loads:
            resource.append(item.text)
        elif item.from_page and any(pattern in item.text for pattern in ignore):
            ignored.append(item.text)
        else:
            app.append(item.text)
    return app, resource, ignored


def _fault(
    said: list[_Said],
    failed_loads: set[str],
    scan: _Scan,
    *,
    strict: bool,
    ignore: tuple[str, ...] = (),
) -> str | None:
    """The failure the page is already showing, if any, in order of directness.

    An error the app raised says more about what is wrong than an output in an
    error state, which says more than a resource that would not load. A failed
    load only decides the verdict under ``strict``: a missing sourcemap, webfont
    or image is not the app being broken, and failing on one teaches a team to
    ignore the check.

    A message matching ``ignore`` decides nothing, which is the whole of what the
    flag does: it is still counted and still reported, and an output in an error
    state or a failed load under ``strict`` still fails the check around it.
    """
    app_errors, resource_errors, _ = _partition(said, failed_loads, ignore)
    if app_errors:
        return "error"
    if scan.output_errors:
        return "output-error"
    if strict and resource_errors:
        return "resource-error"
    return None


def _validate_selectors(page: Page, expects: tuple[str, ...]) -> None:
    """Reject a malformed --expect selector on the blank page, before anything loads.

    Inside the poll loop a bad selector cannot be told from a frame that detached
    mid-navigation: both arrive as a PlaywrightError, and matching on the message
    text misses every rejection Chromium words differently (an invalid XPath and
    an unsupported pseudo-class both come back as `SyntaxError: Failed to execute
    ...`). Such a selector then simply never matched, so the check waited out the
    full timeout and reported that the app had not rendered.

    Asking once, here, replaces the text match with a question that has no other
    answer. The page is still about:blank, so nothing can be transient and no
    frame can detach: an error is the selector. Being before navigation also
    means the caller's mistake is reported even when the site is unreachable,
    where the navigation failure would otherwise arrive first and bury it.
    """
    for selector in expects:
        try:
            page.locator(selector).first.is_visible()
        except PlaywrightError as err:
            raise SelectorError(f"invalid --expect selector: {selector}") from err


def _satisfied(scan: _Scan, expect_state: dict[str, bool], expects: tuple[str, ...]) -> bool:
    """Has the app met the pass condition?

    Every pass is gated on ``shiny_detected`` first, whatever the condition. An
    export serves its shell before pyodide boots, so a selector naming anything in
    that shell is satisfied by a page the runtime never reached: without this gate,
    ``--expect`` certified a fixture that raises at import. The default condition
    already implies it, since a rendered output is itself Shiny having run; making
    it explicit is what stops the two conditions from meaning different things.
    """
    if not scan.shiny_detected:
        return False
    if expects:
        return all(expect_state.values())
    return scan.outputs >= 1


def _answering(page: Page, deadline: float) -> list[Frame]:
    """The frames still answering, asked once for the whole sweep.

    ``evaluate`` is the one playwright call that takes no timeout, so a page
    running an infinite loop - or any long synchronous main-thread work, a heavy
    WASM step or a pathological layout - blocked the scan inside the browser,
    where the poll loop's deadline could not reach it. A ``--timeout 5`` check
    against such a page was measured still running at 45 seconds, with no verdict
    and not even the progress line, because the loop that prints it was never
    re-entered.

    Playwright enforces its timeouts in the driver rather than in the page, so a
    call that accepts one is bounded even when the page never answers. This asks
    the cheapest question there is through such a call. The predicate is constant,
    so a live frame resolves it on the first evaluation and is not slowed by being
    asked.

    Asked once per sweep, and not once per question the sweep goes on to ask.
    Gating each selector lookup on its own liveness check left the cost multiplied
    by however many selectors the caller had named: twenty of them turned an 8
    second budget into 46 seconds, so describing the page in more detail bought a
    weaker guarantee about how long checking that description would take.

    The sweep as a whole is bounded by what is left of the budget, which is what
    keeps a page from buying time by holding many frames rather than by wedging
    one. Frames are asked in order until that is spent; whatever is left unasked
    could not have been read in the time remaining anyway.

    This bounds the wait, which is what the budget promises; it does not make
    reading the page atomic. A frame that stops answering in the moment between
    this question and the scan that follows blocks that scan, because playwright
    offers no bounded way to read a value back. A page wedges once and stays
    wedged, so that window costs a check nothing it can notice.
    """
    ends = time.monotonic() + _sweep_millis(deadline) / 1000
    answering: list[Frame] = []
    for frame in page.frames:
        left = int((ends - time.monotonic()) * 1000)
        if left <= 0:
            break
        try:
            frame.wait_for_function("() => true", timeout=left)
        except PlaywrightError:
            continue  # wedged: nothing can be read from it, and reading is unbounded
        answering.append(frame)
    return answering


def _sweep_millis(deadline: float) -> int:
    """How long one liveness sweep may take, in the budget's terms."""
    return max(_RESPONSIVE_FLOOR_MILLIS, min(_RESPONSIVE_MILLIS, _remaining_millis(deadline)))


def _scan_frames(frames: list[Frame]) -> _Scan:
    """One sweep of every frame that is answering, summed."""
    outputs = 0
    output_errors: list[str] = []
    shiny_detected = False
    for frame in frames:
        try:
            scan = frame.evaluate(_FRAME_SCAN)
        except PlaywrightError:
            continue  # the frame detached or is still navigating
        outputs += scan["rendered"]
        output_errors.extend(scan["errors"])
        shiny_detected = shiny_detected or bool(scan["runtime"] or scan["bound"] or scan["inputs"])
    return _Scan(outputs=outputs, output_errors=output_errors, shiny_detected=shiny_detected)


def _scan_expects(frames: list[Frame], expect_state: dict[str, bool]) -> None:
    """Record which selectors are visible in some frame, as of now.

    Deliberately not sticky. Naming several selectors is a caller describing a
    state the app reaches, and a state is one moment: sticky satisfaction let
    ``--expect "#loading" --expect "#chart"`` be satisfied by a page that showed a
    spinner and then failed, because each had been true at some point. The verdict
    reports the last scan for the same reason, so a selector listed as satisfied is
    one that was satisfied when the check ended.

    Every selector reaching here parses, having been checked before the page was
    loaded, so an error is a frame that went away and nothing else.

    Only frames known to be answering are asked, for the same reason the scan
    skips the rest: ``is_visible`` documents its own ``timeout`` argument as
    ignored, so against a page whose main thread never yields it waits
    playwright's 30 second default - per selector, per frame - which is a limit
    the caller's budget does not govern and cannot shorten.
    """
    for selector in expect_state:
        visible = False
        for frame in frames:
            try:
                if frame.locator(selector).first.is_visible():
                    visible = True
                    break
            except PlaywrightError:
                continue  # the frame detached or is still navigating
        expect_state[selector] = visible


def _shoot(page: Page, path: str | None) -> str | None:
    """Capture the failure screenshot when asked; never let it mask the verdict.

    Playwright writes the file itself, so a full disk or an unwritable directory
    raises OSError rather than a PlaywrightError. Either way the verdict is what
    the caller came for, and losing the screenshot must not cost them that.
    """
    if path is None:
        return None
    try:
        page.screenshot(path=path, full_page=True)
    except (PlaywrightError, OSError):
        return None
    return path
