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
    output_errors: list[str]
    expects: list[ExpectResult]
    failure: str | None
    screenshot: str | None


def run_check(
    url: str,
    *,
    timeout: float = 180.0,
    expects: tuple[str, ...] = (),
    screenshot: str | None = None,
) -> CheckResult:
    """Load ``url``, wait for the app to render, and return the verdict.

    Passing means: the page loaded, no console or page error occurred, no Shiny
    output is in an error state, and either every ``expects`` selector became
    visible (when given) or at least one Shiny output rendered non-empty content
    (the default). ``screenshot`` is captured only on failure.
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
        return CheckResult(
            ok=ok,
            url=url,
            seconds=round(time.monotonic() - start, 1),
            outputs=outputs,
            errors=list(errors),
            output_errors=list(output_errors or []),
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
                    # Let the page settle briefly: an error thrown right after the
                    # first render still fails the check instead of racing it.
                    page.wait_for_timeout(_SETTLE_MILLIS)
                    failure = "error" if errors else None
                elif time.monotonic() > deadline:
                    failure = "timeout"
                else:
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
    """Capture the failure screenshot when asked; never let it mask the verdict."""
    if path is None:
        return None
    try:
        page.screenshot(path=path, full_page=True)
    except PlaywrightError:
        return None
    return path
