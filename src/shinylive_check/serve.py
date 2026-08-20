"""Serve an exported site directory on loopback for the duration of a check."""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _QuietHandler(SimpleHTTPRequestHandler):
    """The default handler logs every request to stderr; the check needs silence."""

    def log_message(self, format: str, *args: object) -> None:
        pass


def serve_directory(directory: Path, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Serve ``directory`` on 127.0.0.1 and return the server plus its URL.

    ``port`` 0 picks an ephemeral port. A fixed port exists for exports that
    reference themselves by absolute URL (a locally built wheel in
    requirements.txt, for example), which only work when served at the exact
    URL they were exported for.
    """
    handler = partial(_QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"
