"""The CLI against real shinylive exports in headless Chromium.

The broken fixtures are the built-in mutation proof: each one must fail for its
own reason, so a checker that waves everything through cannot pass this suite.
"""

from __future__ import annotations

import http.server
import json
import threading
from functools import partial

import pytest
from conftest import run_cli

pytestmark = pytest.mark.browser


def test_green_export_passes(ok_site):
    # No -o flag: stdout is piped, so the auto format must emit JSON.
    proc = run_cli(str(ok_site))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["ok"] is True
    assert verdict["outputs"] >= 1
    assert verdict["errors"] == []
    assert verdict["failure"] is None
    assert verdict["screenshot"] is None


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


class _JoiningServer(http.server.ThreadingHTTPServer):
    """Joins handler threads on close; leaked sockets are errors under this suite."""

    daemon_threads = False
    block_on_close = True


def test_green_export_over_http(ok_site):
    handler = partial(_QuietHandler, directory=str(ok_site))
    server = _JoiningServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        proc = run_cli(url, "-o", "text")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "OK" in proc.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_expect_selector_satisfied(ok_site):
    proc = run_cli(str(ok_site), "--expect", "#txt")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The control for test_expect_cannot_certify_a_dead_export: a real export must
    # still be recognised as a live runtime, or that gate is a blanket refusal.
    assert json.loads(proc.stdout)["shiny_detected"] is True


def test_expect_selector_unsatisfied_fails(ok_site):
    proc = run_cli(str(ok_site), "--expect", "#does-not-exist", "--timeout", "25", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["ok"] is False
    assert verdict["failure"] == "timeout"
    unsatisfied = [e["selector"] for e in verdict["expects"] if not e["satisfied"]]
    assert unsatisfied == ["#does-not-exist"]


def test_boot_error_fails_with_the_message_and_a_screenshot(boot_error_site, tmp_path):
    shot = tmp_path / "failure.png"
    proc = run_cli(str(boot_error_site), "--screenshot", str(shot), "--timeout", "90", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["ok"] is False
    assert verdict["failure"] == "error"
    joined = " ".join(verdict["errors"] + verdict["output_errors"])
    assert "boot_error fixture" in joined
    assert shot.exists() and shot.stat().st_size > 0
    assert verdict["screenshot"] == str(shot)


def test_expect_cannot_certify_a_dead_export(boot_error_site):
    """The configuration the suite never exercised, against the app it knows is dead.

    Every other boot_error assertion runs without `--expect`, which is precisely
    the one configuration where the check held. With `--expect` naming anything the
    static shell already provides, the same dead export was certified ok, and the
    caller who reached for the flag to be MORE specific got a weaker check.
    """
    proc = run_cli(str(boot_error_site), "--expect", "body", "--timeout", "90", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["ok"] is False
    assert "boot_error fixture" in " ".join(verdict["errors"] + verdict["output_errors"])


def test_missing_package_fails_with_the_package_name(missing_package_site):
    proc = run_cli(str(missing_package_site), "--timeout", "90", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["ok"] is False
    assert verdict["failure"] == "error"
    joined = " ".join(verdict["errors"] + verdict["output_errors"])
    assert "nonexistent-package" in joined


def test_a_boot_cut_short_by_the_timeout_is_not_a_broken_app(ok_site):
    """Running out of budget is the tool's answer, never a verdict about the app.

    A real export cut off mid-boot is the case only this fixture can produce:
    pyodide is still fetching a runtime, and the app it will eventually run is
    known-good. Whatever comes back must therefore be about the budget running
    out, never about the app, which rules out `error`, `resource-error` and
    `http-status` alike.

    Which wait verdict it is - `navigation` for a page that never finished
    loading, `timeout` for one that loaded and never rendered - is the machine's
    business rather than this test's, because the budget covers launching the
    browser too. Pinning one of them meant guessing a number that lands between
    the phases, and there is no such number: `--timeout 1` flipped run to run on
    this laptop, and `--timeout 3` still failed under the floor venv, where a
    loaded machine spent the whole budget before the page was up. Both classes
    are already pinned where they can be asked deterministically: `timeout`
    against this same export with a generous budget and an `--expect` that is
    never satisfied, and `navigation` against a host that never answers.
    """
    proc = run_cli(str(ok_site), "--timeout", "3", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["ok"] is False
    assert verdict["failure"] in {"timeout", "navigation"}, verdict
    # And the budget was honoured: this is a check that stopped, not one that ran
    # long and happened to be judged. Generous, since the poll granularity and a
    # browser that has to be closed both land on top of the budget.
    assert verdict["seconds"] < 10, verdict["seconds"]


def test_missing_chromium_is_an_environment_error(ok_site, tmp_path):
    proc = run_cli(
        str(ok_site),
        env={"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "nowhere")},
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    # The last stderr line is the structured envelope; the progress line precedes it.
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "unavailable"
    assert "playwright install chromium" in envelope["error"]["hint"]
