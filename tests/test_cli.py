"""CLI contract without a browser: usage errors, --version, the schema command."""

import json

from conftest import run_cli


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
    assert kinds == {"usage": 2, "unavailable": 4}
    outcomes = {outcome["name"]: outcome["code"] for outcome in schema["outcomes"]}
    assert outcomes == {"check_failed": 1}


def test_check_subcommand_is_an_alias_for_the_bare_target(tmp_path):
    # `shinylive-check check <target>` and `shinylive-check <target>` are the same
    # command; both reject a directory that is not an export, identically.
    bare = run_cli(str(tmp_path))
    spelled = run_cli("check", str(tmp_path))
    assert bare.returncode == spelled.returncode == 2
