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
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright


class BrowserMissingError(RuntimeError):
    """Chromium is not installed for this playwright; the environment is unusable."""


class SelectorError(ValueError):
    """An --expect selector is not valid CSS."""


# The app runs in an iframe, so every frame is scanned; selectors evaluated only on
# the main frame silently match nothing. Outputs carrying shiny-output-error are the
# app saying a render failed; outputs with content are the render succeeding.
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
  return { bound: outs.length, rendered, errors };
}
"""

_POLL_MILLIS = 500
_SETTLE_MILLIS = 500
_NAVIGATION_TIMEOUT_MILLIS = 30_000

# Newlines and tabs survive because tracebacks need them; every other control
# character is dropped.
_KEEP_CONTROLS = frozenset("\n\t")

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
    and move the cursor, so a hostile or broken page could repaint the check's
    own verdict. Text is reported; control of the screen is not.

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
        if char in _KEEP_CONTROLS or not (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F)
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
class CheckResult:
    ok: bool
    url: str
    seconds: float
    outputs: int
    errors: list[str]
    # The number that OCCURRED, which is not len(errors) once the list is clipped.
    errors_total: int
    output_errors: list[str]
    output_errors_total: int
    expects: list[ExpectResult]
    failure: str | None
    screenshot: str | None


def run_check(
    url: str,
    *,
    timeout: float = 180.0,
    expects: tuple[str, ...] = (),
    screenshot: str | None = None,
    progress: Callable[[float], None] | None = None,
) -> CheckResult:
    """Load ``url``, wait for the app to render, and return the verdict.

    Passing means: the page loaded, no console or page error occurred, no Shiny
    output is in an error state, and either every ``expects`` selector became
    visible (when given) or at least one Shiny output rendered non-empty content
    (the default). ``screenshot`` is captured only on failure. ``progress`` is
    called with the elapsed seconds on every poll, for callers reporting a wait.
    """
    start = time.monotonic()
    errors: list[str] = []

    def result(
        *,
        ok: bool,
        outputs: int = 0,
        output_errors: list[str] | None = None,
        expect_state: dict[str, bool] | None = None,
        failure: str | None = None,
        shot: str | None = None,
    ) -> CheckResult:
        state = expect_state or dict.fromkeys(expects, False)
        page_errors = output_errors or []
        return CheckResult(
            ok=ok,
            url=url,
            seconds=round(time.monotonic() - start, 1),
            outputs=outputs,
            errors=_bounded(errors),
            errors_total=len(errors),
            output_errors=_bounded(page_errors),
            output_errors_total=len(page_errors),
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

        try:
            page = browser.new_page()
            page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )

            try:
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=_NAVIGATION_TIMEOUT_MILLIS
                )
            except PlaywrightError as err:
                errors.append(str(err))
                return result(ok=False, failure="navigation", shot=_shoot(page, screenshot))
            if response is not None and response.status >= 400:
                errors.append(f"HTTP {response.status} for {url}")
                return result(ok=False, failure="http-status", shot=_shoot(page, screenshot))

            deadline = start + timeout
            expect_state = dict.fromkeys(expects, False)
            outputs = 0
            output_errors: list[str] = []

            while True:
                outputs, output_errors = _scan_frames(page)
                _scan_expects(page, expect_state)
                rendered = all(expect_state.values()) if expects else outputs >= 1

                if errors:
                    failure = "error"
                elif output_errors:
                    failure = "output-error"
                elif rendered:
                    # Let the page settle briefly: a failure right after the first
                    # render still fails the check instead of racing it. Console
                    # errors arrive as events, but an output that fails inside the
                    # window is only visible to another scan, so the page is
                    # re-scanned rather than just re-read.
                    page.wait_for_timeout(_SETTLE_MILLIS)
                    settled, output_errors = _scan_frames(page)
                    outputs = max(outputs, settled)
                    if errors:
                        failure = "error"
                    elif output_errors:
                        failure = "output-error"
                    else:
                        failure = None
                elif time.monotonic() > deadline:
                    failure = "timeout"
                else:
                    if progress is not None:
                        progress(time.monotonic() - start)
                    page.wait_for_timeout(_POLL_MILLIS)
                    continue

                ok = failure is None
                return result(
                    ok=ok,
                    outputs=outputs,
                    output_errors=output_errors,
                    expect_state=expect_state,
                    failure=failure,
                    shot=None if ok else _shoot(page, screenshot),
                )
        finally:
            browser.close()


def _scan_frames(page: Page) -> tuple[int, list[str]]:
    """Rendered output count and output error texts, summed over every frame."""
    outputs = 0
    output_errors: list[str] = []
    for frame in page.frames:
        try:
            scan = frame.evaluate(_FRAME_SCAN)
        except PlaywrightError:
            continue  # the frame detached or is still navigating
        outputs += scan["rendered"]
        output_errors.extend(scan["errors"])
    return outputs, output_errors


def _scan_expects(page: Page, expect_state: dict[str, bool]) -> None:
    """Mark selectors that became visible in any frame; visibility is sticky."""
    for selector, satisfied in expect_state.items():
        if satisfied:
            continue
        for frame in page.frames:
            try:
                if frame.locator(selector).first.is_visible():
                    expect_state[selector] = True
                    break
            except PlaywrightError as err:
                if "selector" in str(err) and "parsing" in str(err):
                    raise SelectorError(f"invalid --expect selector: {selector}") from err
                continue


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
