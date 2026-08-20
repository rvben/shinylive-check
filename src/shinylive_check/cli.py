"""The shinylive-check command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
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


class UsageError(Exception):
    """A wrong invocation, rendered as a structured `usage` error."""


class _Parser(argparse.ArgumentParser):
    """Raises instead of exiting, so every error renders through one path."""

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="shinylive-check",
        description="Smoke-check an exported Shinylive site in headless Chromium.",
        epilog="`shinylive-check schema` prints the machine-readable contract.",
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
        help="seconds to wait for the app to render before failing (default: %(default)s)",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="CSS selector that must become visible; repeatable. When given, replaces "
        "the default at-least-one-rendered-output condition",
    )
    parser.add_argument(
        "--screenshot",
        metavar="PATH",
        help="write a full-page screenshot to this path on failure",
    )
    parser.add_argument(
        "-o",
        "--output",
        choices=("auto", "text", "json"),
        default="auto",
        help="verdict format on stdout; auto emits JSON when stdout is not a TTY "
        "(default: %(default)s)",
    )
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


def main(argv: list[str] | None = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv
    if args_list[:1] == ["schema"]:
        print(json.dumps(SCHEMA, indent=2))
        return EXIT_OK
    if args_list[:1] == ["check"]:
        args_list = args_list[1:]

    parser = build_parser()
    try:
        args = parser.parse_args(args_list)
    except UsageError as err:
        fmt = _resolve_format(_requested_format(args_list))
        return _render_error("usage", str(err), EXIT_USAGE, fmt=fmt, usage=parser.format_usage())
    fmt = _resolve_format(None if args.output == "auto" else args.output)

    server = None
    try:
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
            server, url = serve_directory(site, args.port)

        print(f"checking {url} (timeout {args.timeout:g}s)", file=sys.stderr)
        result = run_check(
            url,
            timeout=args.timeout,
            expects=tuple(args.expect),
            screenshot=args.screenshot,
        )
    except (UsageError, SelectorError) as err:
        return _render_error("usage", str(err), EXIT_USAGE, fmt=fmt, usage=parser.format_usage())
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


def render_text(result: CheckResult) -> str:
    """The one-glance verdict: a headline, then every reason it failed."""
    if result.ok:
        plural = "" if result.outputs == 1 else "s"
        line = (
            f"OK: {result.outputs} output{plural} rendered in {result.seconds:.1f}s at {result.url}"
        )
        if result.expects:
            line += f" ({len(result.expects)} expect selector(s) satisfied)"
        return line

    headlines = {
        "error": f"{len(result.errors)} console/page error(s)",
        "output-error": f"{len(result.output_errors)} Shiny output error(s)",
        "timeout": f"app did not render within {result.seconds:.0f}s",
        "navigation": "could not load the page",
        "http-status": "the page answered an HTTP error",
    }
    headline = headlines.get(result.failure or "", "check failed")
    lines = [f"FAIL ({result.failure}): {headline} at {result.url}"]
    lines += [f"  {error}" for error in result.errors]
    lines += [f"  output error: {text}" for text in result.output_errors]
    lines += [
        f"  expect not satisfied: {expect.selector}"
        for expect in result.expects
        if not expect.satisfied
    ]
    if result.screenshot:
        lines.append(f"  screenshot: {result.screenshot}")
    return "\n".join(lines)
