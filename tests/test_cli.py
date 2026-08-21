"""CLI contract without a browser: usage errors, --version, the schema command."""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

from conftest import run_cli

from shinylive_check.check import CheckResult
from shinylive_check.cli import build_parser
from shinylive_check.schema import SCHEMA


def test_no_target_is_a_usage_error():
    proc = run_cli()
    assert proc.returncode == 2
    assert "target" in proc.stderr.lower()


def test_usage_errors_are_json_envelopes_when_piped():
    proc = run_cli()
    assert proc.returncode == 2
    envelope = json.loads(proc.stderr)
    assert envelope["error"]["kind"] == "usage"
    assert "target" in envelope["error"]["message"]


def test_explicit_text_format_wins_for_usage_errors():
    proc = run_cli("-o", "text")
    assert proc.returncode == 2
    assert "usage:" in proc.stderr
    assert "error:" in proc.stderr


def test_nonexistent_path_is_a_usage_error(tmp_path):
    proc = run_cli(str(tmp_path / "missing"))
    assert proc.returncode == 2


def test_directory_without_index_html_is_a_usage_error(tmp_path):
    proc = run_cli(str(tmp_path))
    assert proc.returncode == 2
    assert "index.html" in proc.stderr


def test_port_with_url_target_is_a_usage_error():
    proc = run_cli("http://127.0.0.1:9/", "--port", "8000")
    assert proc.returncode == 2
    assert "--port" in proc.stderr


def test_a_timeout_that_is_not_a_finite_number_is_a_usage_error():
    """`nan` and `inf` are floats that a positivity test cannot reject.

    `nan` compares false against every bound, including the one that rejects zero,
    and `inf` compares true against a limit it can never reach, so both passed
    validation and reached the browser. Turning what was left of such a budget into
    a playwright timeout then raised inside the tool: a flag typed wrong was
    reported as exit 5, a traceback, and an invitation to file a bug against
    shinylive-check, which is precisely the outcome that exit code exists to keep
    apart from the user's own mistakes.

    The target is a port nothing answers on, which is the point: a budget that is
    not a number is refused before anything is launched or dialled, so no site has
    to exist for this to be a usage error.

    `-inf` is the value both questions answer, and it decides which one is asked
    first. "Not greater than zero" is true of it and sends the reader looking for a
    larger number; what is actually wrong is that it is not a duration, so that is
    what it has to be told. It is spelled with an `=` because argparse reads a bare
    `-inf` as a flag and answers "expected one argument" instead - a usage error
    naming `--timeout`, which is close enough to this one to pass a test that only
    asked for those two things.
    """
    for value in ("--timeout=nan", "--timeout=inf", "--timeout=-inf"):
        proc = run_cli("http://127.0.0.1:9/", value)
        assert proc.returncode == 2, f"{value}: " + proc.stdout + proc.stderr
        envelope = json.loads(proc.stderr)
        assert envelope["error"]["kind"] == "usage", (value, envelope)
        message = envelope["error"]["message"]
        assert "--timeout" in message, (value, envelope)
        assert "finite" in message, (value, envelope)


def test_version_prints_a_version():
    proc = run_cli("--version")
    assert proc.returncode == 0
    assert proc.stdout.strip().count(".") == 2


def test_the_installed_console_script_runs():
    """`shinylive-check` is the name people type; the suite runs the module.

    `python -m shinylive_check` works from a checkout with nothing installed, so
    an entry point that was never written, or that points at a function which has
    since moved, is invisible to every other test here and immediately visible to
    anyone who installed the package. Under `make check-wheel` this runs against
    the built wheel, which is where that promise is actually kept.
    """
    script = Path(sys.executable).parent / "shinylive-check"
    assert script.exists(), f"no console script next to the interpreter at {script}"
    proc = subprocess.run([str(script), "--version"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Byte for byte what the module form says, so the two ways in cannot drift.
    assert proc.stdout == run_cli("--version").stdout


def _run_unread(*args: str) -> tuple[int, str]:
    """Run the CLI writing to a pipe nothing will ever read from.

    `| head -1` is this situation with a race in it: whether the first write fits
    in the pipe buffer before the reader leaves decides whether the tool notices
    at all, so a small output usually gets away with it and a 200 KB verdict does
    not. Closing the read end up front removes the race and asks the question
    directly, of whichever write comes first.
    """
    read, write = os.pipe()
    os.close(read)
    try:
        with subprocess.Popen(
            [sys.executable, "-m", "shinylive_check", *args],
            stdout=write,
            stderr=subprocess.PIPE,
            text=True,
        ) as proc:
            assert proc.stderr is not None
            stderr = proc.stderr.read()
    finally:
        os.close(write)
    return proc.returncode, stderr


def test_a_reader_that_stops_early_is_not_reported_as_a_bug_in_the_tool():
    """`| head` is a thing people do, not a defect to report.

    Every stdout writer is asked, because they are different code paths: argparse
    writes the help, and the tool writes the contract and the verdict. A failure
    here does not stay quiet - it lands as `internal`, which prints a traceback
    and tells the user to file a bug against this tool for having used `head`.
    """
    for args in (["--help"], ["schema"], ["--version"]):
        code, stderr = _run_unread(*args)
        # 128 + SIGPIPE, which is what a shell reports for a pipeline stopped this
        # way, and what every other filter in that pipeline would have returned.
        assert code == 141, f"{args} exited {code}\n{stderr}"
        assert "Traceback" not in stderr, f"{args}\n{stderr}"
        assert "bug in shinylive-check" not in stderr, f"{args}\n{stderr}"
        # Nothing is said at all: the one who would have read it has gone, and an
        # envelope on every `| head` is noise rather than news.
        assert stderr == "", f"{args}\n{stderr}"


def test_schema_is_a_valid_clispec_contract():
    proc = run_cli("schema")
    assert proc.returncode == 0
    schema = json.loads(proc.stdout)
    assert schema["clispec"] == "0.3"
    assert schema["name"] == "shinylive-check"
    assert schema["version"].count(".") == 2
    assert schema["output"] == {"tty": "text", "piped": "json"}
    assert [command["name"] for command in schema["commands"]] == ["check", "schema"]
    check = next(command for command in schema["commands"] if command["name"] == "check")
    assert check["effects"] == "read_only"
    assert check["cardinality"] == "single"
    # The example lets conformance tooling exercise the command without a real
    # export: a refused loopback connection is an instant, deterministic verdict.
    assert check["example"]["args"][0] == "check"
    fields = {field["name"] for field in check["output_fields"]}
    assert fields >= {"target", "ok", "url", "seconds", "outputs", "errors", "failure"}
    kinds = {error["kind"]: error["exit_code"] for error in schema["errors"]}
    # Every way the tool can fail without a verdict is declared, including the
    # catch-all: an undeclared crash is indistinguishable from a judged failure.
    assert kinds == {
        "usage": 2,
        "unavailable": 4,
        "internal": 5,
        "interrupted": 130,
        "broken_pipe": 141,
    }
    assert set(check["errors"]) == set(kinds)
    # The schema command declares the same list minus the one kind that genuinely
    # cannot reach it: printing a contract needs no browser. The rest are not
    # about what a command does - a bug, an interrupt and a reader walking away
    # are the process's business, and any command can meet them.
    contract = next(command for command in schema["commands"] if command["name"] == "schema")
    assert set(contract["errors"]) == set(kinds) - {"unavailable"}
    outcomes = {outcome["name"]: outcome["code"] for outcome in schema["outcomes"]}
    assert outcomes == {"check_failed": 1}
    # Both are identifiers in the dialect rather than prose, and clispec rejects
    # the whole contract over one that is not - which is worth a single point out
    # of twenty-four, so an invalid schema still grades Excellent and only this
    # notices. A hyphen is the easy way to write one, and reads fine.
    for name in [*kinds, *outcomes]:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), f"{name} is not a clispec identifier"


def test_every_verdict_field_is_declared_in_the_schema_and_no_others():
    """The schema is the contract, so it has to describe the verdict exactly.

    Equality, not containment: the failure this guards is a field added to the
    verdict and forgotten in the schema, which an agent reading the contract
    then cannot discover, and a stale schema entry for a field no longer emitted,
    which is worse because it reads as a promise. Both directions were live risks
    when errors_total and output_errors_total were added by hand to each file.

    `target` is the one verdict key that is not a CheckResult field; the CLI adds
    it when serialising, so it is named here rather than derived.
    """
    declared = {
        field["name"]
        for command in SCHEMA["commands"]
        if command["name"] == "check"
        for field in command["output_fields"]
    }
    emitted = {field.name for field in fields(CheckResult)} | {"target"}
    assert declared == emitted


def test_every_argument_the_parser_accepts_is_declared_in_the_schema_and_no_others():
    """The same equality the verdict gets, for the other half of the contract.

    An argument added to the parser and forgotten in the schema is a flag an agent
    reading the contract cannot discover, and a schema entry for an argument the
    parser rejects is worse, because it reads as a promise. `--help` and
    `--version` are argparse's own and are not part of the declared surface.
    """
    declared = {arg["name"] for arg in SCHEMA["global_args"]}
    declared |= {
        arg["name"]
        for command in SCHEMA["commands"]
        if command["name"] == "check"
        for arg in command["args"]
    }
    # _actions is the only way argparse exposes what it accepts.
    accepted = set()
    for action in build_parser()._actions:
        if isinstance(action, argparse._HelpAction | argparse._VersionAction):
            continue
        long_form = next((s for s in action.option_strings if s.startswith("--")), None)
        accepted.add(
            long_form or (action.option_strings[0] if action.option_strings else action.dest)
        )
    assert declared == accepted


def test_the_readme_sample_verdict_shows_every_field(readme):
    """The README's JSON sample is a promise about what a script will receive.

    A field added to the verdict and forgotten here leaves the sample quietly
    wrong, and the sample is where someone writing a script looks first. Equality
    for the same reason the schema test uses it: a stale key reads as a field that
    still exists.
    """
    _, _, after = readme.partition("$ shinylive-check site | jq .\n")
    sample, fence, _ = after.partition("\n```")
    assert fence, "the README no longer contains the piped-verdict sample"
    documented = set(json.loads(sample))
    emitted = {field.name for field in fields(CheckResult)} | {"target"}
    assert documented == emitted


def _declared_exit_codes() -> set[int]:
    """Every code the contract promises.

    0 is the one no contract declares, because success is the absence of an
    outcome and of an error, so it is named rather than derived.
    """
    return (
        {0}
        | {outcome["code"] for outcome in SCHEMA["outcomes"]}
        | {error["exit_code"] for error in SCHEMA["errors"]}
    )


def _tabulated_codes(text: str, heading: str) -> set[int]:
    """The codes listed under `heading`, in a markdown table or a plain one.

    Both write the code first on its line and stop at the first line without one,
    which is all the parsing this needs. A heading that moved is an assertion
    rather than an empty result, since a table nobody found reads as agreement.
    """
    _, marker, rest = text.partition(heading)
    assert marker, f"the exit-code table no longer follows {heading!r}"
    codes: set[int] = set()
    for line in rest.splitlines():
        # A markdown row opens with the pipe that separates its cells; a plain
        # line may also contain one, and there it is prose about `| head`.
        cell = line.split("|")[1] if line.lstrip().startswith("|") else line
        first = cell.split(maxsplit=1)
        if first and first[0].isdigit():
            codes.add(int(first[0]))
        elif codes:
            break
    return codes


def test_the_readme_lists_every_exit_code_the_contract_declares(readme):
    """The README's table is where a person reads the exit codes.

    The schema is where a script reads them, and the two drift apart one code at
    a time: each arrives in its own change, which touches the schema because the
    tests hold it there, and the table only if someone remembers. A code that is
    real but undocumented is one nobody handles.
    """
    assert _tabulated_codes(readme, "Exit codes:\n") == _declared_exit_codes()


def test_the_help_lists_every_exit_code_the_contract_declares():
    """The same table again, for the reader who runs `--help` instead.

    Two surfaces documenting one contract is two chances to fall behind it, and
    the one nobody is looking at is the one that does.
    """
    proc = run_cli("--help")
    assert proc.returncode == 0
    assert _tabulated_codes(proc.stdout, "exit codes:\n") == _declared_exit_codes()


def test_check_subcommand_is_an_alias_for_the_bare_target(tmp_path):
    # `shinylive-check check <target>` and `shinylive-check <target>` are the same
    # command; both reject a directory that is not an export, identically.
    bare = run_cli(str(tmp_path))
    spelled = run_cli("check", str(tmp_path))
    assert bare.returncode == spelled.returncode == 2
