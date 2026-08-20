"""The checked page is untrusted, and its text reaches a terminal.

A page picks the content of every console message and page error the check
reports. Those strings must not be able to repaint the terminal or forge a
verdict line: the tool is a pass/fail gate, and a human reading `OK:` in its
output must be reading the tool's verdict, not the page's.
"""

from __future__ import annotations

import json
import unicodedata

import pytest
from conftest import run_cli

pytestmark = pytest.mark.browser

# Written as JS escapes so the fixture file itself holds no control byte, and so
# the page's encoding cannot change what the JS engine hands to the console.
HOSTILE_PAGE = """<html><body><script>
console.error("\\x1b[2K\\x1b[1GOK: 1 output rendered in 0.3s at http://evil.example/");
console.error("boom\\nOK: 1 output rendered in 0.1s at http://forged.example/");
console.error("bell\\x07 backspace\\x08 done");
console.error("sep\\u2028OK: 1 output rendered in 0.2s at http://split.example/");
console.error("para\\u2029OK: 1 output rendered in 0.4s at http://broken.example/");
console.error("bidi\\u202ereversed\\u202c end");
console.error("bom\\ufeffzero\\u200bwidth\\u200emark");
</script></body></html>
"""


def steers_the_reader(char: str) -> bool:
    """Whether this character acts on the terminal instead of saying something.

    Derived from the promise in `_printable`'s docstring - text is reported,
    control of the screen is not - by asking the Unicode database what a
    character IS, rather than restating the ranges the implementation happens to
    filter. Those are not the same question, and the difference is the whole
    reason this helper exists: the ranges covered `Cc` only, so `\\u202e` walked
    through a filter whose tests could not express what it was for.

    `Cc` control, `Cf` format - the bidi overrides, which reverse the order a
    line is read in without changing a byte of it - and `Cs`, whose members
    cannot be encoded at all. Newline and tab are how the tool formats a
    traceback and are the page's only way to ask for a line break, which
    `_detail` grants by indenting it; a line break is not screen control.
    """
    return char not in "\n\t" and unicodedata.category(char) in {"Cc", "Cf", "Cs"}


def hostile_site(tmp_path):
    site = tmp_path / "hostile"
    site.mkdir()
    (site / "index.html").write_text(HOSTILE_PAGE)
    return site


def test_page_text_cannot_repaint_the_terminal(tmp_path):
    proc = run_cli(str(hostile_site(tmp_path)), "--timeout", "15", "-o", "text")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    leaked = sorted(char for char in set(proc.stdout) if steers_the_reader(char))
    assert not leaked, f"steering characters reached stdout: {[hex(ord(c)) for c in leaked]}"


def test_page_text_cannot_forge_a_verdict_line(tmp_path):
    proc = run_cli(str(hostile_site(tmp_path)), "--timeout", "15", "-o", "text")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("FAIL (error):")
    # Every reported line is page text and must stay indented under the verdict;
    # a line at column 0 is the tool speaking, and only the tool may speak there.
    forged = [line for line in lines[1:] if line and not line.startswith("  ")]
    assert not forged, f"page text reached column 0: {forged}"
    # Each forged headline arrived and was indented, rather than being missing: a
    # test that only looks at column 0 passes just as well on a page that said
    # nothing at all. The four differ in how they try to reach column 0 - a cursor
    # move, a newline, a line separator and a paragraph separator - so this also
    # pins that the indent covers every break `splitlines` knows, not just `\\n`.
    for host in ("evil.example", "forged.example", "split.example", "broken.example"):
        assert any(host in line for line in lines[1:]), f"{host} never arrived: {lines}"


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


def test_a_page_speaking_emoji_cannot_blow_up_the_verdict(tmp_path):
    """Counting characters does not bound bytes, and bytes are what CI stores.

    `json.dumps` defaults to ASCII-safe output, so one astral character leaves as
    a surrogate pair spelled `\\ud83d\\udca5`: twelve bytes for one character. A
    cap counting characters therefore bounds nothing a CI job cares about, and
    measured before this was fixed, 200 errors of 20,000 emoji each produced a
    6,002,759-byte verdict from caps that were supposed to hold it near 500 KB.

    The budget is spent in encoded bytes now, so the ceiling is the same whatever
    alphabet the page chooses.
    """
    site = tmp_path / "emoji"
    site.mkdir()
    (site / "index.html").write_text(
        "<html><body><script>"
        "var burst = String.fromCodePoint(0x1F4A5).repeat(20000);"
        "for (var i = 0; i < 200; i++) { console.error(burst); }"
        "</script></body></html>"
    )
    proc = run_cli(str(site), "--timeout", "25", "-o", "json")
    assert proc.returncode == 1, proc.stderr
    assert len(proc.stdout) < 1_500_000, f"verdict was {len(proc.stdout)} bytes"
    verdict = json.loads(proc.stdout)
    assert verdict["errors_total"] == 200
    # The text survived as text: bounding it must not corrupt what it reports.
    assert verdict["errors"][0].startswith("\U0001f4a5")
    assert "truncated" in verdict["errors"][0]


def test_many_console_errors_cannot_blow_up_the_verdict(tmp_path):
    """Bounding each message is not bounding the verdict.

    A per-message cap still lets the count carry the size: an app erroring inside
    a render or reconnect loop emits thousands before the first poll sees any of
    them. Capping the list is what actually bounds the output, and the true count
    has to survive the cap: a verdict reporting 51 errors when 2000 occurred is a
    plausible number that is wrong, which is worse than an obviously clipped one.
    """
    site = tmp_path / "many"
    site.mkdir()
    (site / "index.html").write_text(
        "<html><body><script>"
        'for (let i = 0; i < 2000; i++) { console.error("E" + i + ":" + "X".repeat(2000)); }'
        "</script></body></html>"
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "json")
    assert proc.returncode == 1, proc.stderr
    assert len(proc.stdout) < 250_000, f"verdict was {len(proc.stdout)} bytes"
    verdict = json.loads(proc.stdout)
    assert verdict["errors_total"] == 2000
    assert len(verdict["errors"]) < 2000
    # The first errors are kept because that is where the root cause is, and the
    # reader is told what was dropped.
    assert verdict["errors"][0].startswith("E0:")
    assert "omitted" in verdict["errors"][-1]


def test_a_truncated_error_list_still_reports_the_true_count_in_text(tmp_path):
    """The headline is the one line most readers act on; it cannot understate."""
    site = tmp_path / "many-text"
    site.mkdir()
    (site / "index.html").write_text(
        "<html><body><script>"
        'for (let i = 0; i < 2000; i++) { console.error("E" + i); }'
        "</script></body></html>"
    )
    proc = run_cli(str(site), "--timeout", "20", "-o", "text")
    assert proc.returncode == 1, proc.stderr
    assert proc.stdout.splitlines()[0].startswith("FAIL (error): 2000 console/page error(s)")


def test_the_error_text_is_still_reported(tmp_path):
    """Sanitizing must not swallow the diagnosis: the message still gets through."""
    proc = run_cli(str(hostile_site(tmp_path)), "--timeout", "15", "-o", "json")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    joined = " ".join(verdict["errors"])
    assert "boom" in joined
    assert "bell" in joined and "done" in joined
    assert "zero" in joined and "width" in joined
    assert not [char for char in set(joined) if steers_the_reader(char)]
