"""The loopback server that carries a directory target, without a browser.

The export directory is not trusted: in the CI flow this tool is built for, its
contents come from whatever was exported, which can trace back to a pull request.
So the server must serve the export and nothing else on the host.
"""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from shinylive_check.serve import serve_directory

CANARY = "TOP-SECRET-CANARY"


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
        # HTTPError owns the response too, so close it before returning.
        with err:
            return err.code, ""


def export_with_symlink(tmp_path: Path) -> Path:
    """An export directory holding a symlink that points outside it."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html><body>export</body></html>")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text(f"{CANARY}\n")
    (site / "evil-link.txt").symlink_to(secret)
    return site


def test_serves_the_export_itself(tmp_path):
    site = export_with_symlink(tmp_path)
    server, url = serve_directory(site)
    try:
        status, body = fetch(url + "index.html")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert "export" in body


def test_a_symlink_out_of_the_export_is_not_served(tmp_path):
    site = export_with_symlink(tmp_path)
    server, url = serve_directory(site)
    try:
        status, body = fetch(url + "evil-link.txt")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 404, f"symlink escape served the outside file: {body!r}"
    assert CANARY not in body


def test_a_symlink_inside_the_export_still_works(tmp_path):
    """Links that stay inside the export are ordinary content, not an escape."""
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html><body>export</body></html>")
    (site / "real.txt").write_text("inside\n")
    (site / "alias.txt").symlink_to(site / "real.txt")
    server, url = serve_directory(site)
    try:
        status, body = fetch(url + "alias.txt")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert "inside" in body


def test_a_symlink_loop_inside_the_export_is_refused(tmp_path):
    """A link that leads back to itself is a 404, not a dropped connection.

    Resolving one raises RuntimeError rather than OSError on every Python before
    3.13, and the containment guard caught only OSError. The request therefore
    escaped the 404 the guard exists to give it: the handler thread died with a
    traceback on the server's stderr and the connection closed with no reply at
    all, which reads to the caller as the tool crashing rather than as an export
    containing something it may not serve.

    On 3.13 and later `resolve` returns instead of raising and the open that
    follows fails with ELOOP, so the same 404 arrives by another road. This test
    is the whole matrix's, and only the older half of it can fail.
    """
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html><body>export</body></html>")
    (site / "loop.txt").symlink_to(site / "loop2.txt")
    (site / "loop2.txt").symlink_to(site / "loop.txt")
    server, url = serve_directory(site)
    try:
        status, _ = fetch(url + "loop.txt")
    except OSError as err:
        raise AssertionError(
            f"the server dropped the connection instead of refusing the link: {err!r}"
        ) from err
    finally:
        server.shutdown()
        server.server_close()
    assert status == 404


def test_parent_traversal_is_not_served(tmp_path):
    site = export_with_symlink(tmp_path)
    server, url = serve_directory(site)
    try:
        status, _ = fetch(url + "../outside/secret.txt")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 404


@pytest.mark.parametrize("directory", ["", "sub"])
@pytest.mark.parametrize("index_name", ["index.html", "index.htm"])
def test_a_directory_index_cannot_serve_an_outside_symlink(tmp_path, directory, index_name):
    site = tmp_path / "site"
    target = site / directory
    target.mkdir(parents=True)
    outside = tmp_path / "secret.txt"
    outside.write_text(CANARY)
    (target / index_name).symlink_to(outside)
    server, url = serve_directory(site)
    try:
        status, _ = fetch(url + (directory + "/" if directory else ""))
    finally:
        server.shutdown()
        server.server_close()
    assert status == 404


@pytest.mark.parametrize("index_name", ["index.html", "index.htm"])
def test_a_directory_index_symlink_inside_the_export_still_works(tmp_path, index_name):
    site = tmp_path / "site"
    site.mkdir()
    (site / "real.html").write_text("inside index")
    (site / index_name).symlink_to(site / "real.html")
    server, url = serve_directory(site)
    try:
        status, body = fetch(url)
    finally:
        server.shutdown()
        server.server_close()
    assert status == 200
    assert body == "inside index"


def test_a_cold_export_can_queue_a_burst_of_package_connections(tmp_path, monkeypatch):
    # A cold Pyodide boot opens many package connections together. Hold the
    # accept loop until the burst arrives, rather than relying on a timing race.
    ready = threading.Event()
    original = ThreadingHTTPServer.serve_forever

    def paused(server, poll_interval=0.5):
        ready.wait(timeout=10)
        original(server, poll_interval=poll_interval)

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", paused)
    (tmp_path / "index.html").write_text("export")
    server, _ = serve_directory(tmp_path)
    clients = []
    failures = []
    try:
        for _ in range(16):
            try:
                clients.append(
                    socket.create_connection(("127.0.0.1", server.server_port), timeout=0.1)
                )
            except OSError as err:
                failures.append(str(err))
        ready.set()
        for client in clients:
            client.settimeout(5)
            client.sendall(b"GET /index.html HTTP/1.0\r\nHost: localhost\r\n\r\n")
        for client in clients:
            with client.makefile("rb") as response:
                assert b" 200 " in response.readline()
                assert response.read().endswith(b"export")
    finally:
        ready.set()
        for client in clients:
            client.close()
        server.shutdown()
        server.server_close()
    assert not failures, f"cold-boot connections were dropped: {failures}"
