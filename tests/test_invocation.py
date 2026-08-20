"""Invocation shapes that must not crash or misparse, without a browser.

Every failure here has to arrive as the documented envelope. A traceback on
stderr with exit 1 is indistinguishable, to a script, from `the app is broken`.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

from conftest import run_cli

from shinylive_check import cli


def export_dir(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<html><body>empty</body></html>")
    return site


def test_a_global_flag_may_precede_the_check_keyword(tmp_path):
    # The schema declares --output global and `check TARGET` equivalent to the bare
    # form, so the conventional ordering has to reach the same command.
    proc = run_cli("-o", "json", "check", str(tmp_path))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "index.html" in envelope["error"]["message"]


def test_an_attached_global_flag_may_precede_the_check_keyword(tmp_path):
    """`--output=json` and `-ojson` are the same flag as `-o json`.

    argparse accepts all three spellings, so the pre-parse scan that finds the
    command keyword has to skip all three. Missing one makes the flag itself get
    read as the target, and the error then names the flag rather than the export.
    """
    for spelling in ("--output=json", "-ojson"):
        proc = run_cli(spelling, "check", str(tmp_path))
        assert proc.returncode == 2, spelling + proc.stdout + proc.stderr
        envelope = json.loads(proc.stderr.strip().splitlines()[-1])
        assert "index.html" in envelope["error"]["message"], spelling


def test_a_hint_is_rendered_in_text_mode_too(tmp_path):
    """The hint is the actionable half of an error, so it cannot be JSON-only.

    A person hitting a busy port sees the text form; that is exactly the reader
    who needs to be told to pick a different one.
    """
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        proc = run_cli(str(export_dir(tmp_path)), "--port", str(port), "-o", "text")
    finally:
        holder.close()
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "hint: " in proc.stderr
    assert "--port" in proc.stderr


def test_a_directory_named_check_is_still_a_target(tmp_path):
    (tmp_path / "check").mkdir()
    proc = run_cli("check", cwd=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    # Reached the directory, not "no target given": the alias only applies when
    # something follows it.
    assert "index.html" in proc.stderr


def test_a_directory_named_check_is_still_a_target_when_flags_follow(tmp_path):
    """`check` names a directory as readily as it names the command.

    The alias may claim the token only when claiming it leaves an invocation that
    means something. `check --timeout 15` has no target after the keyword, so the
    directory is the only reading left; a guard asking merely whether anything
    follows the keyword answers this one wrong.
    """
    (tmp_path / "check").mkdir()
    proc = run_cli("check", "--timeout", "15", cwd=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "index.html" in proc.stderr


def test_the_check_keyword_without_a_target_says_the_target_is_missing(tmp_path):
    """The control for the test above: no such directory, so the keyword stands.

    Reading `check` as the target here would report that `check` is not a
    directory, which describes a path the caller never typed.
    """
    proc = run_cli("check", "--timeout", "15", cwd=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert "target" in envelope["error"]["message"]
    assert "not a directory" not in envelope["error"]["message"]


def test_the_schema_keyword_wins_over_a_directory_of_the_same_name(tmp_path):
    """The contract command means the contract in every working directory.

    Deliberate, and the opposite of the rule for `check`: an agent asking for the
    contract from a project that happens to have a `schema/` folder must not get
    a verdict about that folder instead. The directory stays reachable by path,
    and a note on stderr says so, where it cannot corrupt the JSON on stdout.
    """
    (tmp_path / "schema").mkdir()
    proc = run_cli("schema", cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["name"] == "shinylive-check"
    assert "./schema" in proc.stderr


def test_a_directory_named_schema_is_reachable_by_path(tmp_path):
    """The escape hatch the note points at has to actually work."""
    (tmp_path / "schema").mkdir()
    proc = run_cli("./schema", cwd=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "index.html" in proc.stderr


def test_the_help_names_the_escape_hatch_for_a_shadowed_directory():
    """The `schema` keyword wins by design, so the other reading needs a signpost.

    Rejecting the "make the directory win" reading is only defensible while the
    way to reach the directory is documented where someone hits the problem.
    """
    proc = run_cli("--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "./schema" in proc.stdout


def test_the_schema_command_rejects_an_unknown_flag():
    proc = run_cli("schema", "--this-is-not-a-flag")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "usage"


def test_the_schema_command_answers_help_with_help():
    proc = run_cli("schema", "--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "usage:" in proc.stdout
    assert "clispec" not in proc.stdout
    # The shared --output flag must describe what THIS command emits: `schema`
    # prints a contract, never a verdict, and help that says otherwise is a lie
    # about the one command whose whole job is describing the interface.
    assert "verdict" not in proc.stdout


def test_the_check_help_calls_the_output_a_verdict():
    """The control for the assertion above: `verdict` is the right word here."""
    proc = run_cli("--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "verdict format" in proc.stdout


def test_the_schema_command_is_reachable_after_a_global_flag():
    proc = run_cli("-o", "json", "schema")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["name"] == "shinylive-check"


def test_a_busy_port_is_a_structured_error_not_a_traceback(tmp_path):
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        proc = run_cli(str(export_dir(tmp_path)), "--port", str(port), "-o", "json")
    finally:
        holder.close()
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Traceback" not in proc.stderr
    envelope = json.loads(proc.stderr.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "usage"
    assert str(port) in envelope["error"]["message"]
    assert "--port" in envelope["error"]["hint"]


def test_an_unexpected_failure_renders_the_internal_envelope(tmp_path, monkeypatch, capsys):
    """Anything unanticipated still leaves stdout clean and stderr structured."""

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the tab crashed")

    monkeypatch.setattr(cli, "run_check", crash)
    code = cli.main([str(export_dir(tmp_path)), "-o", "json"])
    captured = capsys.readouterr()

    assert code == 5
    assert captured.out == ""
    envelope = json.loads(captured.err.strip().splitlines()[-1])
    assert envelope["error"]["kind"] == "internal"
    assert "the tab crashed" in envelope["error"]["message"]
    # The traceback is the whole value of an internal error; it goes to stderr,
    # above the envelope, so the last line stays machine-readable.
    assert "Traceback" in captured.err


def test_a_crash_still_releases_the_served_port(tmp_path, monkeypatch, capsys):
    """A leaked server outlives the process that reported the failure.

    The crash path is where cleanup is easiest to lose: the verdict never
    arrives, so nothing downstream would notice a port still bound. A CI job
    that retries the check would then fail on the port instead of the app.
    """

    def crash(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_check", crash)
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()

    assert cli.main([str(export_dir(tmp_path)), "--port", str(port), "-o", "json"]) == 5
    capsys.readouterr()

    rebind = socket.socket()
    rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    try:
        rebind.bind(("127.0.0.1", port))  # raises if the check left it bound
    finally:
        rebind.close()
