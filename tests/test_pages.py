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
