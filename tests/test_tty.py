"""The auto format on a real terminal.

`-o auto` picks text when stdout is a TTY, and a subprocess with a pipe can
never be one, so the branch every human sees is invisible to the rest of the
suite. This one hands the CLI a pty.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pty = pytest.importorskip("pty", reason="no pty on this platform")

pytestmark = pytest.mark.browser

# The schema's own worked example: a refused loopback connection is an instant,
# deterministic verdict that needs no export.
REFUSED = "http://127.0.0.1:1/"


def test_the_verdict_on_a_terminal_is_the_human_readable_form():
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, "-m", "shinylive_check", REFUSED, "--timeout", "20"],
        stdout=slave,
        stderr=subprocess.DEVNULL,
    )
    os.close(slave)
    chunks = []
    try:
        while True:
            chunk = os.read(master, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError:
        pass  # the pty raises EIO on Linux when the child end closes
    finally:
        os.close(master)
    assert proc.wait(timeout=120) == 1
    assert b"".join(chunks).decode().startswith("FAIL (navigation):")
