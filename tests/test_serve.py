"""The loopback server that carries a directory target, without a browser.

The export directory is not trusted: in the CI flow this tool is built for, its
contents come from whatever was exported, which can trace back to a pull request.
So the server must serve the export and nothing else on the host.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from shinylive_check.serve import serve_directory

CANARY = "TOP-SECRET-CANARY"


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as err:
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


def test_parent_traversal_is_not_served(tmp_path):
    site = export_with_symlink(tmp_path)
    server, url = serve_directory(site)
    try:
        status, _ = fetch(url + "../outside/secret.txt")
    finally:
        server.shutdown()
        server.server_close()
    assert status == 404
