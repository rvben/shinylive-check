"""Serve an exported site directory on loopback for the duration of a check."""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import BinaryIO


class _ExportServer(ThreadingHTTPServer):
    # Pyodide fetches many packages together during a cold boot. The default
    # queue of five resets connections under that burst on macOS, making a
    # healthy export fail before its runtime can start.
    request_queue_size = 128


class _QuietHandler(SimpleHTTPRequestHandler):
    """Serves the export and nothing else, silently.

    The default handler logs every request to stderr, which the check does not
    want, and follows symlinks out of the served directory, which it must not:
    an export is untrusted content (in the CI flow this tool is built for it can
    trace back to a pull request), and the page it serves is running in a browser
    with network access. A link resolving outside the export is refused, so the
    check cannot be turned into a reader of arbitrary host files.
    """

    def log_message(self, format: str, *args: object) -> None:
        pass

    def send_head(self) -> BytesIO | BinaryIO | None:
        root = Path(self.directory).resolve()
        try:
            target = Path(self.translate_path(self.path)).resolve()
            served = target
            # SimpleHTTPRequestHandler chooses an index after translating a
            # directory. Validate that file too, before its open follows a link.
            if target.is_dir():
                for name in ("index.html", "index.htm"):
                    candidate = target / name
                    if candidate.is_file():
                        served = candidate.resolve()
                        break
        # A symlink loop is RuntimeError rather than OSError on every Python before
        # 3.13, and an uncaught one kills the handler thread: the connection closes
        # with no reply and the traceback lands on the server's stderr, which reads
        # as this tool crashing rather than as an export holding a link it may not
        # follow.
        except (OSError, RuntimeError):
            self.send_error(404, "File not found")
            return None
        if (target != root and root not in target.parents) or (
            served != root and root not in served.parents
        ):
            self.send_error(404, "File not found")
            return None
        return super().send_head()


def serve_directory(directory: Path, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Serve ``directory`` on 127.0.0.1 and return the server plus its URL.

    ``port`` 0 picks an ephemeral port. A fixed port exists for exports that
    reference themselves by absolute URL (a locally built wheel in
    requirements.txt, for example), which only work when served at the exact
    URL they were exported for.
    """
    handler = partial(_QuietHandler, directory=str(directory))
    server = _ExportServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"
