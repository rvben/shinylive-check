"""The shinylive-check command line interface."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections.abc import Callable
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn

from .check import BrowserMissingError, CheckResult, SelectorError, run_check
from .schema import SCHEMA
from .serve import serve_directory

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_ENVIRONMENT = 4
EXIT_INTERNAL = 5

# A cold pyodide boot is tens of megabytes of wasm and the default timeout is three
# minutes, so the wait gets a pulse: silence for that long reads as a hung job.
_HEARTBEAT_SECONDS = 15.0

_EPILOG = """\
examples:
  shinylive-check site                      check an exported directory
  shinylive-check https://example.com/app/  probe a deployed site
  shinylive-check site --expect "#plot"     require a specific element to appear

exit codes:
  0  the app booted and rendered
  1  the check failed; stdout carries the verdict
  2  usage error
  4  environment error: Chromium is not installed for playwright
  5  internal error: shinylive-check itself failed

`shinylive-check schema` prints the machine-readable contract; pass ./schema to
check a directory of that name.
"""


class UsageError(Exception):
    """A wrong invocation, rendered as a structured `usage` error."""

    def __init__(self, message: str, *, hint: str | None = None, usage: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint
        self.usage = usage


class _Parser(argparse.ArgumentParser):
    """Raises instead of exiting, so every error renders through one path."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, usage=self.format_usage())


def _add_output_flag(parser: argparse.ArgumentParser, what: str) -> None:
    """The one global flag, so every command accepts it in the same spelling.

    ``what`` names the thing the command actually emits. The flag is shared, but
    a command's help has to describe that command: `schema` prints a contract and
    never a verdict, and help saying otherwise misdescribes the one command whose
    job is describing the interface.
    """
    parser.add_argument(
        "-o",
        "--output",
        choices=("auto", "text", "json"),
        default="auto",
        help=f"{what} format on stdout; auto emits JSON when stdout is not a TTY "
        "(default: %(default)s)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="shinylive-check",
        description="Smoke-check an exported Shinylive site in headless Chromium.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "target",
        help="exported site directory, or the http(s) URL of a deployed site",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        metavar="SECONDS",
        help="seconds the whole check may take, loading the page included, before "
        "failing (default: %(default)s)",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="CSS selector that must be visible; repeatable. When given, replaces the "
        "default at-least-one-rendered-output condition; the Shiny runtime must be "
        "detected either way",
    )
    parser.add_argument(
        "--screenshot",
        metavar="PATH",
        help="write a full-page screenshot to this path on failure",
    )
    parser.add_argument(
        "--fail-on-resource-error",
        action="store_true",
        help="fail the check when the browser could not load a resource (a 404 asset, "
        "a missing sourcemap); reported but ignored by default",
    )
    _add_output_flag(parser, "verdict")
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="PORT",
        help="serve a directory target on this fixed port (default: ephemeral); for "
        "exports that reference themselves by absolute URL",
    )
    parser.add_argument(
        "--version", action="version", version=f"shinylive-check {version('shinylive-check')}"
    )
    return parser


def _requested_format(args_list: list[str]) -> str | None:
    """The explicitly requested output format, readable even when parsing failed."""
    fmt = None
    for position, token in enumerate(args_list):
        if token in ("-o", "--output") and position + 1 < len(args_list):
            fmt = args_list[position + 1]
        elif token.startswith("--output="):
            fmt = token.split("=", 1)[1]
        elif token.startswith("-o") and len(token) > 2 and not token.startswith("--"):
            fmt = token[2:]
    return fmt if fmt in ("text", "json") else None


def _leading_globals(args_list: list[str]) -> int:
    """How many leading tokens are the global --output flag and its value.

    --output is global, so the conventional `--output json check TARGET` ordering
    has to reach the same command as `check TARGET`. The command keyword is looked
    for after the global flags rather than at position zero.
    """
    index = 0
    while index < len(args_list):
        token = args_list[index]
        if token in ("-o", "--output") and index + 1 < len(args_list):
            index += 2
        elif token.startswith("--output=") or (
            token.startswith("-o") and len(token) > 2 and not token.startswith("--")
        ):
            index += 1
        else:
            break
    return index


def _resolve_format(explicit: str | None) -> str:
    if explicit in ("text", "json"):
        return explicit
    return "text" if sys.stdout.isatty() else "json"


def _render_error(
    kind: str,
    message: str,
    exit_code: int,
    *,
    fmt: str,
    hint: str | None = None,
    usage: str | None = None,
) -> int:
    if fmt == "json":
        error: dict[str, str] = {"kind": kind, "message": message}
        if hint:
            error["hint"] = hint
        print(json.dumps({"error": error}, separators=(",", ":")), file=sys.stderr)
    else:
        if usage:
            sys.stderr.write(usage)
        print(f"shinylive-check: error: {message}", file=sys.stderr)
        if hint:
            print(f"hint: {hint}", file=sys.stderr)
    return exit_code


def _heartbeat(every: float, write: Callable[[str], object]) -> Callable[[float], None]:
    """A progress line at most once every ``every`` seconds, for long waits."""
    last = 0.0

    def report(elapsed: float) -> None:
        nonlocal last
        if elapsed - last < every:
            return
        last = elapsed
        write(f"still waiting ({elapsed:.0f}s elapsed)\n")

    return report


def _parse_check(args_list: list[str], lead: int) -> argparse.Namespace:
    """Parse the invocation, deciding whether a leading `check` is the keyword.

    `check` is this command's own name and an ordinary directory name, and a
    directory is what the command takes. The keyword reading wins whenever it
    leaves a valid invocation, which is every normal use, so an explicit target
    is never second-guessed. When it does not - the keyword with no target after
    it, whatever flags follow - a directory of that name is the only reading left
    that means anything, so it is used. With no such directory the keyword
    reading's error stands, because a caller who typed `check --timeout 15` in an
    empty directory needs to be told the target is missing, not that `check` is
    not a directory.
    """
    stripped = args_list[:lead] + args_list[lead + 1 :]
    try:
        return build_parser().parse_args(stripped)
    except UsageError:
        if not Path(args_list[lead]).is_dir():
            raise
        return build_parser().parse_args(args_list)


def _note_shadowed_schema_directory() -> None:
    """Say so when `schema` could just as well have named a directory here.

    The contract command has to mean the contract in every working directory: an
    agent reading the interface from a project that happens to have a `schema/`
    folder must not get a verdict about that folder instead. So the keyword wins
    unconditionally here, unlike `check`, and the ambiguity is answered with a
    note naming the way to reach the directory rather than with a guess. It goes
    to stderr, so the contract on stdout stays parseable.
    """
    if Path("schema").is_dir():
        print(
            "note: printing the contract; ./schema checks the directory of that name",
            file=sys.stderr,
        )


def _schema_command(args_list: list[str]) -> int:
    """Print the contract, after validating the invocation like any other command."""
    parser = _Parser(
        prog="shinylive-check schema",
        description="Print the machine-readable CLI contract as JSON.",
        epilog="The contract itself is always JSON; --output selects the format of errors.",
    )
    _add_output_flag(parser, "error")
    parser.parse_args(args_list)
    print(json.dumps(SCHEMA, indent=2))
    return EXIT_OK


def _serve(site: Path, port: int) -> tuple[ThreadingHTTPServer, str]:
    """Serve the export, reporting a port that cannot be bound as a usage error."""
    try:
        return serve_directory(site, port)
    except OSError as err:
        where = f"port {port}" if port else "an ephemeral port"
        raise UsageError(
            f"cannot serve {site} on {where}: {err}",
            hint="pick a free --port, or omit --port for an ephemeral one" if port else None,
        ) from err


def main(argv: list[str] | None = None) -> int:
    """Run the CLI, and let nothing out of here except a documented exit code."""
    args_list = sys.argv[1:] if argv is None else argv
    try:
        return _dispatch(args_list)
    except Exception as err:
        # The traceback is the whole value of an unexpected failure, so it goes to
        # stderr above the envelope, leaving the last line machine-readable.
        traceback.print_exc()
        return _render_error(
            "internal",
            f"{type(err).__name__}: {err}",
            EXIT_INTERNAL,
            fmt=_resolve_format(_requested_format(args_list)),
            hint="this is a bug in shinylive-check; please report it with the command you ran",
        )


def _dispatch(args_list: list[str]) -> int:
    fmt = _resolve_format(_requested_format(args_list))
    server = None
    try:
        lead = _leading_globals(args_list)
        keyword = args_list[lead] if lead < len(args_list) else None
        if keyword == "schema":
            _note_shadowed_schema_directory()
            return _schema_command(args_list[:lead] + args_list[lead + 1 :])
        if keyword == "check":
            args = _parse_check(args_list, lead)
        else:
            args = build_parser().parse_args(args_list)
        fmt = _resolve_format(None if args.output == "auto" else args.output)

        if args.timeout <= 0:
            raise UsageError(f"--timeout must be greater than zero, got {args.timeout:g}")

        if args.target.startswith(("http://", "https://")):
            if args.port:
                raise UsageError("--port applies only to a directory target")
            url = args.target
        else:
            site = Path(args.target)
            if not site.is_dir():
                raise UsageError(f"{args.target} is not a directory or http(s) URL")
            if not (site / "index.html").is_file():
                raise UsageError(f"{args.target} has no index.html; is it a shinylive export?")
            server, url = _serve(site, args.port)

        print(f"checking {url} (timeout {args.timeout:g}s)", file=sys.stderr)
        result = run_check(
            url,
            timeout=args.timeout,
            expects=tuple(args.expect),
            screenshot=args.screenshot,
            fail_on_resource_error=args.fail_on_resource_error,
            progress=_heartbeat(_HEARTBEAT_SECONDS, sys.stderr.write),
        )
    except (UsageError, SelectorError) as err:
        usage = err.usage if isinstance(err, UsageError) else None
        hint = err.hint if isinstance(err, UsageError) else None
        return _render_error("usage", str(err), EXIT_USAGE, fmt=fmt, hint=hint, usage=usage)
    except BrowserMissingError as err:
        return _render_error(
            "unavailable",
            f"Chromium is not installed for playwright: {err}",
            EXIT_ENVIRONMENT,
            fmt=fmt,
            hint="run `playwright install chromium`",
        )
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()

    if fmt == "json":
        print(json.dumps({"target": args.target, **asdict(result)}, indent=2))
    else:
        print(render_text(result))
    return EXIT_OK if result.ok else EXIT_FAILED


def _detail(text: str, label: str = "") -> list[str]:
    """Indent every line of page-chosen text under the verdict.

    A multi-line console error would otherwise put its later lines at column 0,
    where `OK: 1 output rendered ...` reads as this tool's own headline. Column 0
    belongs to the verdict; everything the page said is indented under it.
    """
    return [f"  {line}" for line in f"{label}{text}".splitlines() or [""]]


def _resource_lines(result: CheckResult) -> list[str]:
    """Failed loads, always shown, labelled by whether they decided the verdict.

    Ignored must not mean hidden: a 404 nobody can see is a 404 nobody fixes, and
    the flag that would have failed on it is named here rather than in the manual.
    """
    if not result.resource_errors_total:
        return []
    plural = "" if result.resource_errors_total == 1 else "s"
    count = f"{result.resource_errors_total} resource error{plural}"
    if result.failure == "resource-error":
        lines = [f"  {count}:"]
    else:
        lines = [f"  {count} ignored (--fail-on-resource-error to fail on these):"]
    for text in result.resource_errors:
        lines += _detail(text, "resource: ")
    return lines


def render_text(result: CheckResult) -> str:
    """The one-glance verdict: a headline, then every reason it failed."""
    if result.ok:
        plural = "" if result.outputs == 1 else "s"
        line = (
            f"OK: {result.outputs} output{plural} rendered in {result.seconds:.1f}s at {result.url}"
        )
        if result.expects:
            line += f" ({len(result.expects)} expect selector(s) satisfied)"
        return "\n".join([line, *_resource_lines(result)])

    headlines = {
        # The counts are what OCCURRED, not what is listed below: the lists are
        # clipped for a page that errors in a loop, and a headline reading the
        # clipped length would understate the damage with a plausible number.
        "error": f"{result.errors_total} console/page error(s)",
        "output-error": f"{result.output_errors_total} Shiny output error(s)",
        # Wall time, not the requested timeout: it includes the browser launch, so
        # claiming a window the number then exceeds reads as a bug in the tool.
        "timeout": f"app did not render (waited {result.seconds:.1f}s)",
        "navigation": "could not load the page",
        "http-status": "the page answered an HTTP error",
        "resource-error": f"{result.resource_errors_total} resource(s) failed to load",
    }
    headline = headlines.get(result.failure or "", "check failed")
    lines = [f"FAIL ({result.failure}): {headline} at {result.url}"]
    if result.failure == "timeout" and not result.shiny_detected:
        # Without this, a timeout on a page whose every selector matched reads as a
        # bug in the tool, and the user retunes the selector that already worked.
        lines.append("  no Shiny runtime detected: window.Shiny absent and nothing bound")
    for error in result.errors:
        lines += _detail(error)
    for text in result.output_errors:
        lines += _detail(text, "output error: ")
    lines += _resource_lines(result)
    lines += [
        f"  expect not satisfied: {expect.selector}"
        for expect in result.expects
        if not expect.satisfied
    ]
    if result.screenshot:
        lines.append(f"  screenshot: {result.screenshot}")
    return "\n".join(lines)
