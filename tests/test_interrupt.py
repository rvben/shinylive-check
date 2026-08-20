"""Ctrl-C has to stop the check.

The tool drives a browser, which is a child process holding a pipe, so stopping
is not automatic: a run that ignores an interrupt has to be hunted down with
`kill -9`, and in CI it holds a runner until the job timeout. The interrupt is
delivered to the process group, which is what a terminal does.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

_PAGE = b"<html><body><div id='ready'>hi</div></body></html>"


def _page_server(requested: threading.Event) -> tuple[ThreadingHTTPServer, str]:
    """Serve one page and announce when the browser asks for it.

    The check has to be interrupted while the browser session is live, which is
    the state the teardown has to survive. Sleeping for a plausible number of
    seconds would only usually be that state; a request for the page is the
    browser saying it has launched and navigated.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.set()
            kind = "text/javascript" if self.path.endswith(".js") else "text/html"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(_PAGE)))
            self.end_headers()
            self.wfile.write(_PAGE)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


@dataclass
class Interrupted:
    """What became of a run that was interrupted mid-session."""

    returncode: int
    stdout: str
    stderr: str
    killed: bool
    group: int
    # Where the test's own server was listening, which is the target for a URL run
    # and only the page's script source for a directory run.
    beacon: str


def interrupt_a_live_run(site: Path | None = None) -> Interrupted:
    """Start a check against a page that never renders, and Ctrl-C it.

    The run gets its own session, so the signal reaches the whole group the way a
    terminal sends it, and so what the group still contains afterwards is a
    question that can be asked.

    Given a ``site``, the check serves that directory on a loopback port of its
    own, which the interrupt then has to unwind through as well. The page there
    pulls a script from this server, so the browser still announces itself even
    though the page it loaded came from somewhere else.
    """
    requested = threading.Event()
    server, url = _page_server(requested)
    try:
        if site is None:
            target = url
        else:
            (site / "index.html").write_text(
                f'<html><body><script src="{url}beacon.js"></script></body></html>'
            )
            target = str(site)
        proc = subprocess.Popen(
            [sys.executable, "-m", "shinylive_check", target, "--timeout", "120"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        group = os.getpgid(proc.pid)
        killed = False
        assert requested.wait(90), "the browser never asked for the page"
        os.killpg(group, signal.SIGINT)
        try:
            out, err = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            killed = True
            os.killpg(group, signal.SIGKILL)
            out, err = proc.communicate()
    finally:
        server.shutdown()
        server.server_close()
    return Interrupted(proc.returncode, out, err, killed, group, url)


def test_an_interrupted_run_stops_instead_of_having_to_be_killed():
    """The page never renders, so the run would otherwise wait out its timeout."""
    run = interrupt_a_live_run()
    assert not run.killed, "the run ignored SIGINT for 20 seconds and needed SIGKILL"
    # 128 + SIGINT, which is what a shell reports for a command stopped this way.
    assert run.returncode == 130, f"exit {run.returncode}\n{run.stderr}"
    # An interrupt is something the caller did, not a crash: no traceback, and no
    # verdict either, because the check has no answer to report.
    assert "Traceback" not in run.stderr, run.stderr
    assert run.stdout.strip() == "", run.stdout
    # The kind, not just the code, because 130 alone is what any killed process
    # returns: the envelope is where the tool says it stopped on purpose. The code
    # this kind carries is declared in the contract and pinned in `test_cli.py`.
    # Reading it off the LAST line also pins the promise the envelope rests on -
    # that a script can parse that line - which the browser driver's shutdown
    # chatter was arriving after and taking away.
    envelope = json.loads(run.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "interrupted"


def test_a_directory_check_stops_on_an_interrupt_too(tmp_path):
    """The ordinary target is a directory, which the tool serves itself.

    That loopback server is started before the browser and shut down on the way
    out, so it sits on the unwind path the interrupt now takes. A shutdown that
    waited on its own serving thread would hang exactly where the browser close
    used to, and the URL target above would never show it.
    """
    run = interrupt_a_live_run(tmp_path)
    # The tool announces the URL it is checking, and for a directory that is a URL
    # it invented: seeing this test's own server there instead would mean the run
    # never served anything and the whole point of this case was skipped.
    assert run.beacon not in run.stderr, run.stderr
    assert "checking http://127.0.0.1:" in run.stderr, run.stderr
    assert not run.killed, "the run ignored SIGINT for 20 seconds and needed SIGKILL"
    assert run.returncode == 130, f"exit {run.returncode}\n{run.stderr}"
    envelope = json.loads(run.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "interrupted"


def test_an_interrupted_run_leaves_no_browser_behind():
    """Stopping fast is worth nothing if Chromium stays.

    The interrupt path exits without unwinding, on the reasoning that the driver
    is a child process reading a pipe that closes when this one exits, and takes
    the browser with it. That is a claim about a process tree, so it is asked of
    the process tree: signal 0 to the group the run led says whether anything is
    still in it. A leaked browser is invisible in CI until the runner does, and it
    is exactly what skipping cleanup risks.
    """
    run = interrupt_a_live_run()
    assert not run.killed, "the run had to be killed, so the group says nothing"
    deadline = time.monotonic() + 15
    while True:
        try:
            os.killpg(run.group, 0)
        except ProcessLookupError:
            return
        assert time.monotonic() < deadline, "something is still running 15s after the interrupt"
        time.sleep(0.1)
