"""The checked page is untrusted, and its text reaches a terminal.

A page picks the content of every console message and page error the check
reports. Those strings must not be able to repaint the terminal or forge a
verdict line: the tool is a pass/fail gate, and a human reading `OK:` in its
output must be reading the tool's verdict, not the page's.
"""

from __future__ import annotations

import json

import pytest
from conftest import run_cli

pytestmark = pytest.mark.browser

# Written as JS escapes so the fixture file itself holds no control byte.
HOSTILE_PAGE = """<html><body><script>
console.error("\\x1b[2K\\x1b[1GOK: 1 output rendered in 0.3s at http://evil.example/");
console.error("boom\\nOK: 1 output rendered in 0.1s at http://forged.example/");
console.error("bell\\x07 backspace\\x08 done");
</script></body></html>
"""

CONTROL_BYTES = {chr(code) for code in range(0x20)} - {"\n", "\t"}
CONTROL_BYTES |= {chr(code) for code in range(0x7F, 0xA0)}


def hostile_site(tmp_path):
    site = tmp_path / "hostile"
    site.mkdir()
    (site / "index.html").write_text(HOSTILE_PAGE)
    return site


def test_page_text_cannot_repaint_the_terminal(tmp_path):
    proc = run_cli(str(hostile_site(tmp_path)), "--timeout", "15", "-o", "text")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    leaked = sorted(CONTROL_BYTES & set(proc.stdout))
    assert not leaked, f"control bytes reached stdout: {[hex(ord(c)) for c in leaked]}"


def test_page_text_cannot_forge_a_verdict_line(tmp_path):
    proc = run_cli(str(hostile_site(tmp_path)), "--timeout", "15", "-o", "text")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("FAIL (error):")
    # Every reported line is page text and must stay indented under the verdict;
    # a line at column 0 is the tool speaking, and only the tool may speak there.
    forged = [line for line in lines[1:] if line and not line.startswith("  ")]
    assert not forged, f"page text reached column 0: {forged}"


def test_an_enormous_console_error_cannot_blow_up_the_verdict(tmp_path):
    """The page chooses how long its errors are; the tool chooses how long its
    verdict is.

    An app dumping a large dataframe repr or a deep traceback into console.error
    is ordinary, not just hostile. Unbounded, that text is copied through
    Chromium, playwright, a Python str and the JSON encoder before landing in a
    CI log: a 20 MB error measured a 20 MB verdict and 1.2 GB peak RSS, which is
    an OOM on a small runner and an unusable verdict for whatever parses it.
    """
    site = tmp_path / "huge"
    site.mkdir()
    (site / "index.html").write_text(
        '<html><body><script>console.error("B" + "A".repeat(5 * 1024 * 1024));'
        "</script></body></html>"
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stderr
    assert len(proc.stdout) < 100_000, f"verdict was {len(proc.stdout)} bytes"
    reported = json.loads(proc.stdout)["errors"][0]
    # The head of the message is the diagnosis and has to survive intact...
    assert reported.startswith("BAAAA")
    # ...and the reader has to be told the rest was dropped, with the real size,
    # so a truncated error is never mistaken for a short one.
    assert "truncated" in reported
    assert "5242881" in reported


def test_the_error_text_is_still_reported(tmp_path):
    """Sanitizing must not swallow the diagnosis: the message still gets through."""
    proc = run_cli(str(hostile_site(tmp_path)), "--timeout", "15", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    joined = " ".join(verdict["errors"])
    assert "boom" in joined
    assert "bell" in joined and "done" in joined
    assert not CONTROL_BYTES & set(joined)
