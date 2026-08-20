"""CLI contract without a browser: usage errors, --version, the schema command."""

import argparse
import json
from dataclasses import fields

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


def test_version_prints_a_version():
    proc = run_cli("--version")
    assert proc.returncode == 0
    assert proc.stdout.strip().count(".") == 2


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
    assert kinds == {"usage": 2, "unavailable": 4, "internal": 5, "interrupted": 130}
    assert set(check["errors"]) == set(kinds)
    outcomes = {outcome["name"]: outcome["code"] for outcome in schema["outcomes"]}
    assert outcomes == {"check_failed": 1}


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


def test_check_subcommand_is_an_alias_for_the_bare_target(tmp_path):
    # `shinylive-check check <target>` and `shinylive-check <target>` are the same
    # command; both reject a directory that is not an export, identically.
    bare = run_cli(str(tmp_path))
    spelled = run_cli("check", str(tmp_path))
    assert bare.returncode == spelled.returncode == 2
