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
    assert scan == {"bound": 2, "rendered": 1, "errors": ["bad"]}
