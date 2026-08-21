"""Judgement calls against synthetic pages, in a real browser.

The shinylive fixtures in test_e2e.py prove the checker against real exports,
but they can only produce the failures pyodide happens to produce: both broken
ones fail as console errors, so the output-error branch, the settle window, the
HTTP and navigation branches and the multi-selector condition are invisible to
them. A hand-written page can be exactly one thing at a time, which is what
these need.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import run_cli
from playwright.sync_api import sync_playwright

from shinylive_check.check import _FRAME_SCAN

pytestmark = pytest.mark.browser

RENDERED = '<div class="shiny-bound-output" id="one">ready</div>'


def page(tmp_path: Path, body: str, name: str = "site") -> Path:
    site = tmp_path / name
    site.mkdir()
    (site / "index.html").write_text(f"<html><body>\n{body}\n</body></html>\n")
    return site


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _NotFound(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_error(404, "File not found")

    def log_message(self, format: str, *args: object) -> None:
        pass


class _JoiningServer(http.server.ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True


def test_a_page_that_renders_an_output_passes(tmp_path):
    """The control for every failing page below: this shape can pass."""
    proc = run_cli(str(page(tmp_path, RENDERED)), "--timeout", "20", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["outputs"] == 1


def test_a_pass_writes_no_screenshot(tmp_path):
    """`--screenshot` is documented as the picture of what the visitor would have
    seen, taken only when the check fails.

    A run that shoots every time is still green, and the file it leaves is the
    one a CI job uploads and a person reads as evidence of a failure.
    """
    shot = tmp_path / "shot.png"
    proc = run_cli(
        str(page(tmp_path, RENDERED)), "--screenshot", str(shot), "--timeout", "20", "-o", "json"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not shot.exists(), "a passing check left a failure screenshot behind"
    assert json.loads(proc.stdout)["screenshot"] is None


def test_the_verdict_echoes_the_target_and_times_the_run(tmp_path):
    """Two fields nothing else asserts against a real run.

    `target` is the directory as the user gave it, which is what a script keys a
    report by, and `seconds` is how long the check took. Both are the kind of
    field that can quietly become a resolved path or a zero without any other
    test noticing, since every other assertion is about the verdict.

    The target is given relative, from the directory beside it: an absolute path
    is already its own resolved form, so echoing it back proves nothing about
    which of the two the verdict carries.
    """
    page(tmp_path, RENDERED)
    started = time.monotonic()
    proc = run_cli("site", "--timeout", "20", "-o", "json", cwd=tmp_path)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["target"] == "site"
    assert verdict["url"].startswith("http://127.0.0.1:")
    # Bounded on both sides: above zero, since a missing duration is not a fast
    # check, and no larger than the wall time of the process that measured it.
    assert 0 < verdict["seconds"] <= elapsed


def test_an_http_error_status_fails_as_http_status(tmp_path):
    server = _JoiningServer(("127.0.0.1", 0), _NotFound)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        proc = run_cli(url, "--timeout", "20", "-o", "json")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "http-status"
    assert "404" in " ".join(verdict["errors"])


class _Blackhole:
    """Accepts connections and never answers: a load that hangs rather than fails.

    A refused connection fails instantly and so exercises nothing about waiting.
    This is the shape that actually costs time in CI - a proxy, a load balancer or
    an origin that holds the socket open with nothing behind it.
    """

    def __init__(self) -> None:
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._held.append(conn)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        for conn in self._held:
            conn.close()
        self._listener.close()


def test_the_timeout_governs_loading_the_page_too(tmp_path):
    """`--timeout` is the budget for the whole check, navigation included.

    Loading the page had its own hardcoded 30 second limit, so `--timeout 3`
    against a server that accepts and never answers took 31.4 seconds measured end
    to end. A timeout flag a run can exceed by an order of magnitude is not a
    timeout: the number exists so a CI job can bound its own wall time.
    """
    hole = _Blackhole()
    try:
        started = time.monotonic()
        proc = run_cli(f"http://127.0.0.1:{hole.port}/", "--timeout", "3", "-o", "json")
        elapsed = time.monotonic() - started
    finally:
        hole.close()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["failure"] == "navigation"
    # Generous: the budget is 3s and a browser launch is about a second. The
    # behaviour this excludes took 31s.
    assert elapsed < 20, f"a 3s timeout took {elapsed:.1f}s"


def test_a_timeout_of_zero_is_a_usage_error(tmp_path):
    """Zero is the one value playwright reads as "wait forever".

    Passing the remaining budget straight through would turn the smallest possible
    timeout into an unbounded wait, which is the opposite of what was asked for.
    Rejecting it at the edge is clearer than clamping it silently.
    """
    proc = run_cli(str(page(tmp_path, RENDERED, name="zero")), "--timeout", "0")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "usage"
    assert "--timeout" in envelope["error"]["message"]


def test_a_refused_connection_fails_as_navigation():
    # The schema's own worked example: instant, self-contained, no export needed.
    proc = run_cli("http://127.0.0.1:1/", "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["failure"] == "navigation"


def test_a_navigation_failure_is_never_an_ignored_console_error():
    """What the check says about a dead site is not what the page said.

    `net::ERR_` is a plausible thing to exempt: a third-party beacon that will
    not load writes it to the console on every run. The message the check writes
    when the site ITSELF will not load carries the same text, so matching it
    there moved the only diagnosis the verdict had, leaving `failure:
    "navigation"` beside an empty `errors` list and the reason for it filed as an
    ignored console error - which it was not, and which had not been ignored,
    since it is what ended the check.
    """
    proc = run_cli(
        "http://127.0.0.1:1/", "--ignore-console", "net::ERR_", "--timeout", "20", "-o", "json"
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "navigation"
    assert verdict["ignored_errors"] == []
    # Asserted by prefix: Chromium words a refused connection and a blocked port
    # differently, and which one a host answers with is not this test's subject.
    assert "net::ERR_" in " ".join(verdict["errors"])


def test_an_error_status_is_never_an_ignored_console_error(tmp_path):
    """The same rule for the other verdict the check words itself.

    A pattern is read against what the app said, and an HTTP status is the check
    reporting what it was served. Exempting it would leave a verdict that failed
    on a status it also claimed to be ignoring.
    """
    server = _JoiningServer(("127.0.0.1", 0), _NotFound)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        proc = run_cli(url, "--ignore-console", "HTTP 404", "--timeout", "20", "-o", "json")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "http-status"
    assert verdict["ignored_errors"] == []
    assert "404" in " ".join(verdict["errors"])


def test_an_output_in_an_error_state_fails_as_output_error(tmp_path):
    """No console error anywhere: only the output element says the app is broken."""
    site = page(tmp_path, '<div class="shiny-bound-output shiny-output-error">boom in output</div>')
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "output-error"
    assert verdict["output_errors"] == ["boom in output"]


def test_an_output_that_fails_during_the_settle_window_still_fails(tmp_path):
    site = page(
        tmp_path,
        RENDERED
        + """
<div class="shiny-bound-output" id="two"></div>
<script>
setTimeout(() => {
  const two = document.getElementById("two");
  two.classList.add("shiny-output-error");
  two.textContent = "late output failure";
}, 300);
</script>""",
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "output-error"
    assert "late output failure" in " ".join(verdict["output_errors"])


def test_an_error_thrown_during_the_settle_window_still_fails(tmp_path):
    site = page(
        tmp_path,
        RENDERED + '\n<script>setTimeout(() => { throw new Error("late boom"); }, 300);</script>',
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "error"
    assert "late boom" in " ".join(verdict["errors"])


def test_a_missing_sub_resource_does_not_fail_a_healthy_app(tmp_path):
    """A 404 on an asset is not the app being broken.

    Chromium writes a console error of type `error` for every resource it could
    not load, and every one of those failed the check. A missing sourcemap, a
    stale cache-busted webfont or an image nobody deployed then reddened CI for an
    app that boots perfectly, which is the failure mode that teaches a team to
    ignore the check. They are reported, and they do not decide the verdict.
    """
    site = page(tmp_path, f'{RENDERED}\n<img src="missing.png" alt="">', name="missing-asset")
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["errors"] == []
    assert verdict["resource_errors_total"] == 1
    assert "404" in " ".join(verdict["resource_errors"])


def test_a_sub_resource_that_never_answers_is_a_resource_error_too(tmp_path):
    """The other half of a failed load: no HTTP status at all, just a dead host.

    A refused connection never produces a response, so it is only visible as a
    failed request. Classifying on the response alone would leave this one looking
    like an error the app itself raised.
    """
    site = page(
        tmp_path,
        f'{RENDERED}\n<img src="http://127.0.0.1:1/nope.png" alt="">',
        name="refused-asset",
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["errors"] == []
    assert verdict["resource_errors_total"] == 1


def test_an_app_console_error_is_not_a_resource_error(tmp_path):
    """The control: the app's own console.error still fails the check.

    This is the boundary the split has to get right. A resource error is
    synthesised by the browser, carries no console arguments and is located at the
    URL that failed; a real console.error carries its arguments and is located in
    the page's own script.
    """
    site = page(
        tmp_path,
        f'{RENDERED}\n<script>console.error("something broke");</script>',
        name="app-console-error",
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "error"
    assert "something broke" in " ".join(verdict["errors"])
    assert verdict["resource_errors"] == []


def test_an_app_error_that_impersonates_a_resource_error_still_fails(tmp_path):
    """The page does not get to talk its way out of failing the check.

    Chromium's message for a failed load is fixed text, so classifying on the text
    would let any app silence its own errors by printing that sentence. The origin
    is what decides, and an app cannot forge it.
    """
    impersonation = "Failed to load resource: the server responded with a status of 404 (Not Found)"
    site = page(
        tmp_path,
        f"{RENDERED}\n<script>console.error({json.dumps(impersonation)});</script>",
        name="impersonation",
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "error"
    assert verdict["resource_errors"] == []


def test_fail_on_resource_error_makes_a_missing_asset_fail(tmp_path):
    """The opt-in for a project where every asset is meant to be there."""
    site = page(tmp_path, f'{RENDERED}\n<img src="missing.png" alt="">', name="strict-asset")
    proc = run_cli(str(site), "--fail-on-resource-error", "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "resource-error"
    assert "404" in " ".join(verdict["resource_errors"])


def test_an_ignored_resource_error_is_still_visible_on_a_pass(tmp_path):
    """Ignored must not mean hidden: a 404 nobody can see is one nobody fixes."""
    site = page(tmp_path, f'{RENDERED}\n<img src="missing.png" alt="">', name="visible-asset")
    proc = run_cli(str(site), "--timeout", "20", "-o", "text")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("OK:")
    assert "resource error" in proc.stdout
    assert "--fail-on-resource-error" in proc.stdout


def test_an_ignored_console_error_does_not_fail_the_check(tmp_path):
    """The escape hatch for a message a team has already decided about.

    Some libraries print an error that is not one, and until now the only ways
    past it were to stop running the check or to stop believing it. The exemption
    is per message and named on the command line, so what is being tolerated is
    written down where the next person can read it.
    """
    site = page(
        tmp_path,
        f'{RENDERED}\n<script>console.error("ResizeObserver loop limit exceeded");</script>',
        name="ignored-console",
    )
    proc = run_cli(
        str(site), "--ignore-console", "ResizeObserver loop", "--timeout", "20", "-o", "json"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["errors"] == []
    assert verdict["ignored_errors_total"] == 1
    assert "ResizeObserver loop limit exceeded" in " ".join(verdict["ignored_errors"])


def test_a_console_error_no_pattern_matches_still_fails(tmp_path):
    """The control: the exemption has to be the substring doing the work.

    Same page, a pattern that does not match it. An implementation that ignored
    every console error whenever the flag was given would pass this too, and would
    have turned the flag into the blanket switch it is deliberately not.
    """
    site = page(
        tmp_path,
        f'{RENDERED}\n<script>console.error("ResizeObserver loop limit exceeded");</script>',
        name="unmatched-console",
    )
    proc = run_cli(str(site), "--ignore-console", "something else", "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "error"
    assert verdict["ignored_errors"] == []


def test_only_the_matching_error_is_exempted(tmp_path):
    """A page saying two things gets one answer per thing.

    The exemption is per message, so an app that prints a known-harmless message
    and then really breaks still fails, and the verdict says which was which.
    """
    site = page(
        tmp_path,
        f"{RENDERED}\n<script>console.error('known noise'); console.error('real boom');</script>",
        name="two-console-errors",
    )
    proc = run_cli(str(site), "--ignore-console", "known noise", "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "error"
    assert "real boom" in " ".join(verdict["errors"])
    assert "known noise" not in " ".join(verdict["errors"])
    assert "known noise" in " ".join(verdict["ignored_errors"])


def test_an_uncaught_page_error_can_be_ignored_too(tmp_path):
    """A thrown error reaches the same list, so the flag has to reach it as well.

    Console errors and uncaught exceptions are one field in the verdict and one
    headline in the text. Exempting only half of that would make the flag work or
    not depending on how the noisy library happened to report itself.
    """
    site = page(
        tmp_path,
        RENDERED + '\n<script>setTimeout(() => { throw new Error("known throw"); }, 100);</script>',
        name="ignored-pageerror",
    )
    proc = run_cli(str(site), "--ignore-console", "known throw", "--timeout", "20", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["errors"] == []
    assert "known throw" in " ".join(verdict["ignored_errors"])


def test_ignore_console_does_not_reach_a_failed_load(tmp_path):
    """The two exemptions stay apart, so neither can be turned off by the other.

    A failed load has its own switch and its own list. Letting a console pattern
    match one as well would mean `--ignore-console 404` silently disarming
    `--fail-on-resource-error`, and the message a resource error carries is written
    by the browser, so the pattern that disarmed it would not even be the app's
    own words.
    """
    site = page(tmp_path, f'{RENDERED}\n<img src="missing.png" alt="">', name="ignore-vs-resource")
    proc = run_cli(
        str(site),
        "--fail-on-resource-error",
        "--ignore-console",
        "Failed to load resource",
        "--timeout",
        "20",
        "-o",
        "json",
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "resource-error"
    assert verdict["ignored_errors"] == []


def test_an_ignored_console_error_is_still_visible_on_a_pass(tmp_path):
    """Ignored must not mean hidden, end to end and in the text a person reads."""
    site = page(
        tmp_path,
        f'{RENDERED}\n<script>console.error("ResizeObserver loop limit exceeded");</script>',
        name="visible-ignored",
    )
    proc = run_cli(
        str(site), "--ignore-console", "ResizeObserver loop", "--timeout", "20", "-o", "text"
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("OK:")
    assert "ResizeObserver loop limit exceeded" in proc.stdout
    assert "--ignore-console" in proc.stdout


def test_an_unhandled_promise_rejection_fails_the_check(tmp_path):
    """The shape a pyodide boot failure actually takes.

    Loading the wasm runtime and resolving packages are asynchronous, so a real
    failure surfaces as a rejected promise nobody caught, not as a synchronous
    throw. An output is already on the page, so the pass condition is otherwise
    met: if rejections were not watched this page would be reported OK, which is
    the exact false pass this tool exists to prevent. Both spellings are checked
    because micropip rejects with a plain string, not an Error.
    """
    rejections = {
        "async-throw": 'async function boot() { throw new Error("boot failed"); } boot();',
        "bare-reject": 'Promise.reject("could not resolve nosuchpkg");',
    }
    for name, script in rejections.items():
        site = page(tmp_path, f"{RENDERED}\n<script>{script}</script>", name=name)
        proc = run_cli(str(site), "--timeout", "20", "-o", "json")
        assert proc.returncode == 1, name + proc.stdout + proc.stderr
        verdict = json.loads(proc.stdout)
        assert verdict["failure"] == "error", name
        # The output DID render: the verdict is not a timeout in disguise.
        assert verdict["outputs"] == 1, name


def test_a_static_shell_with_no_shiny_runtime_never_passes(tmp_path):
    """`--expect` must not certify a page the Shiny runtime never touched.

    A shinylive export serves its shell before pyodide boots, and every element in
    that shell matches a selector exactly as well as a rendered app does. So an
    `--expect` naming a shell element is satisfied by a genuinely dead app:
    measured against the boot_error fixture, `--expect body` returned ok in 1.4s,
    two seconds before the traceback a bare check catches. Satisfying the
    condition needs positive evidence that the runtime is alive, not evidence that
    HTML exists.
    """
    site = page(tmp_path, '<div id="ready">shell</div>', name="shell-only")
    proc = run_cli(str(site), "--expect", "#ready", "--timeout", "5", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "timeout"
    assert verdict["shiny_detected"] is False
    # The selector really was satisfied: the check failed on the runtime, and the
    # verdict has to say which, or the user retunes the selector that already works.
    assert verdict["expects"] == [{"selector": "#ready", "satisfied": True}]


def test_a_bound_element_is_evidence_the_runtime_ran(tmp_path):
    """The first control for the gate above: same selector, plus a bound element.

    Shiny adds `shiny-bound-*` classes from JavaScript, so their presence in the
    DOM is the runtime having run, which is what the gate is really asking about.
    """
    site = page(tmp_path, RENDERED + '<div id="ready">shell</div>', name="shell-bound")
    proc = run_cli(str(site), "--expect", "#ready", "--timeout", "20", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["shiny_detected"] is True


def test_the_shiny_runtime_alone_is_evidence_enough(tmp_path):
    """The second control: a live app that has not bound an output yet.

    `--expect` exists so a caller can name their own readiness signal, which is
    often not an output element. A booted runtime with nothing bound must still be
    able to satisfy it, or the gate would quietly narrow the flag to outputs.
    """
    site = page(
        tmp_path,
        '<div id="ready">up</div><script>window.Shiny = {};</script>',
        name="runtime-only",
    )
    proc = run_cli(str(site), "--expect", "#ready", "--timeout", "20", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["shiny_detected"] is True
    assert verdict["outputs"] == 0


def test_a_render_that_vanishes_during_the_settle_window_is_not_a_pass(tmp_path):
    """The settle window has to be able to withdraw a pass, not only add a failure.

    It re-scanned the page and then took the larger of the two output counts, so a
    render that appeared and went away again still counted: the one scan that saw
    it decided the verdict and the scan that did not was discarded. That is the
    opposite of what a settle window is for. An app whose output flickers and
    disappears is exactly the flaky boot this tool exists to catch.
    """
    site = page(
        tmp_path,
        RENDERED + '\n<script>setTimeout(() => { document.getElementById("one").innerHTML = ""; },'
        " 100);</script>",
        name="vanishing",
    )
    proc = run_cli(str(site), "--timeout", "5", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "timeout"
    assert verdict["outputs"] == 0
    # The element is still bound, so this is not the runtime gate firing: the
    # output really was there and really went away.
    assert verdict["shiny_detected"] is True


def test_an_output_that_keeps_flickering_cannot_outlast_the_timeout(tmp_path):
    """The budget has to bound the settle window too, or it bounds nothing.

    Withdrawing a pass sends the loop back to waiting, and that path did not
    consult the deadline: only the branch for a page that has not rendered did.
    A page that is satisfied at the top of every pass and unsatisfied after every
    settle therefore looped forever, 500 ms at a time, and `--timeout 3` was
    measured still running after 60 seconds with nothing on stdout. That is the
    held-until-the-job-timeout failure this tool exists to prevent, arriving from
    the tool itself.

    The flicker is made exact rather than raced: reading the element's markup is
    what alternates it, so every scan sees the opposite of the scan before,
    whatever the machine's timing. A real app cannot usually align that precisely,
    but the point is that nothing bounded it, and a bound that holds only for
    pages that flicker at the wrong frequency is not a bound.
    """
    site = page(
        tmp_path,
        """
<div id="one" class="shiny-bound-output"></div>
<script>
  var reads = 0;
  Object.defineProperty(document.getElementById("one"), "innerHTML", {
    get: function () { reads += 1; return reads % 2 === 1 ? "rendered" : ""; }
  });
</script>""",
        name="flickering",
    )
    try:
        # Eight seconds rather than the three that reproduced it: the budget covers
        # launching Chromium, and a loaded machine can spend three seconds there
        # alone, which answers `navigation` before the loop under test is reached.
        # A page that never renders spends the whole budget whatever it is, so the
        # room is free here in a way it is not for a real export.
        proc = run_cli(str(site), "--timeout", "8", "-o", "json", timeout=90)
    except subprocess.TimeoutExpired as expired:
        raise AssertionError("the check never returned: --timeout did not bound it") from expired
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    # Never stably rendered is what a timeout means, and the budget is the budget:
    # one settle window and the browser's own teardown land on top of it.
    assert verdict["failure"] == "timeout", verdict
    assert verdict["seconds"] < 15, verdict["seconds"]


def test_a_page_that_wedges_its_own_main_thread_cannot_outlast_the_timeout(tmp_path):
    """A page that stops answering must be judged, not waited on forever.

    Reading the page is the one thing the check cannot do without the page's
    cooperation: `frame.evaluate` is the single playwright call that takes no
    timeout, so a site whose main thread never yields again blocked the scan
    inside the browser, where the poll loop's own deadline could not reach it. A
    `--timeout 5` check against this page was measured still running at 45
    seconds, with no verdict and not even the 15 second progress line, because
    the loop that prints it was never re-entered.

    An infinite loop is the honest version of a broad class: any long synchronous
    call on the main thread - a heavy WASM step, a pathological layout - stops the
    page answering for as long as it runs. The verdict is `timeout` because that
    is what happened. The app never rendered, and a page that cannot answer
    cannot be distinguished from one that has nothing to say.
    """
    site = page(
        tmp_path,
        """
<div id="one" class="shiny-bound-output"></div>
<script>setTimeout(function () { while (true) {} }, 400);</script>""",
        name="wedged",
    )
    try:
        # Eight seconds for the same reason as the flicker case above: launching
        # Chromium is inside the budget, and a loaded machine can spend seconds
        # there. The wedge is armed on a timer rather than inline so the page
        # finishes loading first, which puts the hang in the scan loop under test
        # rather than in navigation.
        proc = run_cli(str(site), "--timeout", "8", "-o", "json", timeout=90)
    except subprocess.TimeoutExpired as expired:
        raise AssertionError(
            "the check never returned: a wedged page outlasted --timeout"
        ) from expired
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "timeout", verdict
    assert verdict["seconds"] < 15, verdict["seconds"]


def test_a_wedged_page_holding_a_second_frame_is_still_bounded(tmp_path):
    """The sweep runs out of budget mid-page, and what it cannot ask it must skip.

    An export puts the app in an iframe, and a same-origin iframe shares the main
    thread with the page holding it, so a single infinite loop wedges both frames
    at once. The first then spends the whole sweep and leaves nothing for the
    second - and playwright reads a zero timeout as "wait forever", so handing the
    remainder along instead of stopping restores the unbounded wait the bound
    exists to prevent, on the very page shape a shinylive export has.
    """
    site = page(
        tmp_path,
        """
<div id="one" class="shiny-bound-output"></div>
<iframe src="child.html"></iframe>
<script>setTimeout(function () { while (true) {} }, 400);</script>""",
        name="wedged-frames",
    )
    (site / "child.html").write_text("<html><body><p>child</p></body></html>\n")
    try:
        proc = run_cli(str(site), "--timeout", "8", "-o", "json", timeout=90)
    except subprocess.TimeoutExpired as expired:
        raise AssertionError(
            "the check never returned: a frame the sweep could not reach outlasted --timeout"
        ) from expired
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "timeout", verdict
    assert verdict["seconds"] < 20, verdict["seconds"]


def test_expect_selectors_do_not_extend_the_budget_on_a_wedged_page(tmp_path):
    """The other way a page that stops answering costs a caller their timeout.

    Looking a selector up is a second question put to the page, and
    `is_visible` documents its own `timeout` argument as ignored: it waits
    playwright's 30 second default instead, once per selector per frame per poll.
    Measured against the page below on a `--timeout 8` budget, two selectors took
    65 seconds before anything gated the lookup.

    Gating each lookup on its own liveness question only made the multiplier
    smaller. The wait per selector per frame fell from 30 seconds to 2, so the
    same 8 second budget bought 46 seconds once twenty selectors were named, and
    more with more. A caller who describes the page in detail must not pay for
    describing it, so liveness is now asked once per sweep and every selector is
    answered from that one question.

    So this is not the same defect as the scan above wearing a different hat. That
    one never returned at all; this one returns, having spent several times the
    budget it was given, which is the failure `--timeout` exists to make
    impossible.
    """
    site = page(
        tmp_path,
        """
<div id="one" class="shiny-bound-output"></div>
<script>setTimeout(function () { while (true) {} }, 400);</script>""",
        name="wedged-expect",
    )
    # Enough selectors that a per-selector wait is unmistakable in the wall time,
    # which two were not: two passed this bound while still costing 9 seconds.
    named = [flag for i in range(20) for flag in ("--expect", f"#missing-{i}")]
    proc = run_cli(str(site), *named, "--timeout", "8", "-o", "json", timeout=300)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "timeout", verdict
    # Comfortably between the two measurements, 46 seconds against 9, so neither a
    # slow machine nor a fast one decides the outcome.
    assert verdict["seconds"] < 20, verdict["seconds"]


def test_expect_selectors_must_be_satisfied_at_the_same_moment(tmp_path):
    """Two selectors that are never visible together do not add up to a pass.

    Satisfaction used to be sticky: once a selector had been seen it stayed seen,
    so `--expect "#loading" --expect "#chart"` was satisfied by a page that showed
    a spinner and then failed. Naming several selectors is a caller asking for a
    state the app reaches, and a state is a single moment.
    """
    site = page(
        tmp_path,
        RENDERED
        + """
<div id="a">a</div>
<script>
setTimeout(() => { document.getElementById("a").style.display = "none"; }, 1000);
setTimeout(() => {
  const b = document.createElement("div");
  b.id = "b";
  b.textContent = "b";
  document.body.appendChild(b);
}, 2500);
</script>""",
        name="never-together",
    )
    proc = run_cli(str(site), "--expect", "#a", "--expect", "#b", "--timeout", "6", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "timeout"
    # #a is the one that was seen and then withdrawn: the verdict must report the
    # state now, not the high-water mark.
    assert [e["selector"] for e in verdict["expects"] if not e["satisfied"]] == ["#a"]


def test_expect_selectors_visible_together_pass(tmp_path):
    """The control: simultaneity is the requirement, not arriving at the same time."""
    site = page(
        tmp_path,
        RENDERED
        + """
<div id="a">a</div>
<script>
setTimeout(() => {
  const b = document.createElement("div");
  b.id = "b";
  b.textContent = "b";
  document.body.appendChild(b);
}, 700);
</script>""",
        name="together",
    )
    proc = run_cli(str(site), "--expect", "#a", "--expect", "#b", "--timeout", "20", "-o", "json")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert all(e["satisfied"] for e in json.loads(proc.stdout)["expects"])


def test_every_expect_selector_must_be_satisfied(tmp_path):
    # One of the two is present. All must be, or the condition is not met.
    proc = run_cli(
        str(page(tmp_path, RENDERED)),
        "--expect",
        "#one",
        "--expect",
        "#two",
        "--timeout",
        "5",
        "-o",
        "json",
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "timeout"
    assert [e["selector"] for e in verdict["expects"] if not e["satisfied"]] == ["#two"]


def test_an_invalid_expect_selector_is_a_usage_error(tmp_path):
    proc = run_cli(str(page(tmp_path, RENDERED)), "--expect", "###not[[[valid", "--timeout", "20")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "usage"
    assert "###not[[[valid" in envelope["error"]["message"]


def test_a_selector_chromium_rejects_in_its_own_words_is_a_usage_error_too(tmp_path):
    """Malformedness is not a wording.

    Chromium reports an unsupported pseudo-class and an invalid XPath as
    `SyntaxError: Failed to execute ...`, not as a parsing error, so telling a bad
    selector from a frame that detached mid-navigation by matching the message
    text let these through. The selector then never matched, the check waited out
    the whole timeout, and the verdict blamed the app for never rendering.
    """
    site = str(page(tmp_path, RENDERED))
    for selector in ("div:nonsense(1)", "xpath=///"):
        proc = run_cli(site, "--expect", selector, "--timeout", "20")
        assert proc.returncode == 2, selector + proc.stdout + proc.stderr
        envelope = json.loads(proc.stderr.strip().splitlines()[-1])
        assert envelope["error"]["kind"] == "usage", selector
        assert selector in envelope["error"]["message"], selector


def test_an_invalid_selector_is_answered_before_the_page_is_loaded():
    """The selector is the caller's mistake, so it is answered without the app.

    Judged inside the poll loop, a bad selector is noticed only once the page is
    up, and never when the page is down: the navigation failure arrives first and
    the invocation error is lost behind a verdict about the site.
    """
    proc = run_cli("http://127.0.0.1:1/", "--expect", "div:nonsense(1)", "--timeout", "20")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "usage"
    assert "div:nonsense(1)" in envelope["error"]["message"]


def test_a_fixed_port_is_the_port_the_export_is_served_on(tmp_path):
    port = free_port()
    proc = run_cli(str(page(tmp_path, RENDERED)), "--port", str(port), "--timeout", "20")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["url"] == f"http://127.0.0.1:{port}/"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_a_screenshot_that_cannot_be_written_does_not_mask_the_verdict(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    site = page(tmp_path, '<div class="shiny-bound-output shiny-output-error">boom</div>')
    try:
        proc = run_cli(
            str(site), "--screenshot", str(locked / "shot.png"), "--timeout", "20", "-o", "json"
        )
    finally:
        locked.chmod(0o755)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["failure"] == "output-error"
    assert verdict["screenshot"] is None


def test_the_frame_scan_script_is_valid_and_returns_its_documented_shape(tmp_path):
    """The scan is a JS blob evaluated in every frame; a typo in it degrades to a
    timeout that blames the app. This fails on the blob itself instead."""
    site = page(tmp_path, RENDERED + '<div class="shiny-bound-output shiny-output-error">bad</div>')
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            tab = browser.new_page()
            tab.goto((site / "index.html").as_uri())
            scan = tab.evaluate(_FRAME_SCAN)
        finally:
            browser.close()
    assert scan == {"bound": 2, "inputs": 0, "rendered": 1, "errors": ["bad"], "runtime": False}


def test_a_verdict_too_big_for_the_pipe_does_not_become_a_bug_report(tmp_path):
    """`shinylive-check site -o json | head`, which is how a person looks at a verdict.

    The other half of this is covered without a browser in test_cli.py, where a
    pipe closed up front makes the very first write fail. This is the branch only
    a real check reaches: a verdict far larger than the 64 KB a pipe holds cannot
    be handed over in one write, so the reader leaves mid-transfer and the failure
    lands inside the write rather than in the flush after it. That one used to
    reach the catch-all, which printed a traceback and asked the user to report a
    bug in this tool, for having used `head`.
    """
    noisy = (
        "<script>for (let i = 0; i < 200; i++) "
        '{ console.error("E".repeat(4000) + " #" + i); }</script>'
    )
    site = page(tmp_path, RENDERED + noisy)
    with subprocess.Popen(
        [sys.executable, "-m", "shinylive_check", str(site), "--timeout", "30", "-o", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as tool:
        assert tool.stdout is not None and tool.stderr is not None
        with subprocess.Popen(
            ["head", "-c", "100"], stdin=tool.stdout, stdout=subprocess.DEVNULL
        ) as reader:
            # This process holds a copy of the read end too, and while it does the
            # writer never learns that head has gone.
            tool.stdout.close()
            reader.wait()
        stderr = tool.stderr.read()
    # 128 + SIGPIPE, the same answer the fast test gets from the other branch.
    assert tool.returncode == 141, f"exited {tool.returncode}\n{stderr}"
    assert "Traceback" not in stderr, stderr
    assert "bug in shinylive-check" not in stderr, stderr
