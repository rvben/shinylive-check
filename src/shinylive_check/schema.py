"""The machine-readable CLI contract, printed by ``shinylive-check schema``.

The dialect is The CLI Spec v0.3 (https://clispec.dev). ``make schema-check``
scores the built CLI against it with ``clispec score``.
"""

from importlib.metadata import version

FAILURE_KINDS = ["error", "output-error", "timeout", "navigation", "http-status"]

_VERDICT_FIELDS = [
    {"name": "target", "type": "string", "description": "The directory or URL as given"},
    {"name": "ok", "type": "boolean", "description": "True when the app booted and rendered"},
    {"name": "url", "type": "string", "description": "The URL the browser actually loaded"},
    {"name": "seconds", "type": "number", "description": "Wall time of the check"},
    {
        "name": "outputs",
        "type": "integer",
        "description": "Shiny outputs that rendered non-empty content",
    },
    {
        "name": "shiny_detected",
        "type": "boolean",
        "description": (
            "True when the Shiny runtime was observed on the page: window.Shiny "
            "defined, or an element Shiny bound. Every pass requires it, so a "
            "static shell cannot satisfy --expect"
        ),
    },
    {
        "name": "errors",
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Console errors and uncaught page errors, in order. Long messages are "
            "truncated and the list is clipped to the earliest 50, so use "
            "errors_total for the count"
        ),
    },
    {
        "name": "errors_total",
        "type": "integer",
        "description": "Console and page errors that occurred, which may exceed len(errors)",
    },
    {
        "name": "output_errors",
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Messages of Shiny outputs in an error state, truncated and clipped "
            "like errors; use output_errors_total for the count"
        ),
    },
    {
        "name": "output_errors_total",
        "type": "integer",
        "description": "Shiny outputs in an error state, which may exceed len(output_errors)",
    },
    {
        "name": "expects",
        "type": "array",
        "items": {
            "type": "object",
            "fields": [
                {"name": "selector", "type": "string"},
                {"name": "satisfied", "type": "boolean"},
            ],
        },
        "description": "One entry per --expect selector",
    },
    {
        "name": "failure",
        "type": "string",
        "nullable": True,
        "enum": FAILURE_KINDS,
        "description": "Why the check failed; null when it passed",
    },
    {
        "name": "screenshot",
        "type": "string",
        "nullable": True,
        "description": "Path of the failure screenshot, when one was captured",
    },
]

SCHEMA = {
    "clispec": "0.3",
    "name": "shinylive-check",
    "version": version("shinylive-check"),
    "description": (
        "Smoke-check an exported Shinylive site in headless Chromium: "
        "fail on boot failures, console errors and unrendered outputs."
    ),
    "output": {"tty": "text", "piped": "json"},
    "global_args": [
        {
            "name": "--output",
            "short": "-o",
            "type": "string",
            "enum": ["auto", "text", "json"],
            "default": "auto",
            "description": (
                "Output format. auto emits JSON when stdout is not a TTY, human-readable otherwise."
            ),
        }
    ],
    "commands": [
        {
            "name": "check",
            "description": (
                "Serve an exported site directory (or probe a deployed URL) and judge "
                "the boot in headless Chromium. The bare form `shinylive-check TARGET` "
                "is this command."
            ),
            "effects": "read_only",
            "cardinality": "single",
            "args": [
                {
                    "name": "target",
                    "type": "string",
                    "required": True,
                    "description": (
                        "Exported site directory, or the http(s) URL of a deployed site"
                    ),
                },
                {
                    "name": "--timeout",
                    "type": "number",
                    "default": 180,
                    "description": "Seconds to wait for the app to render before failing",
                },
                {
                    "name": "--expect",
                    "type": "string[]",
                    "description": (
                        "CSS selector that must become visible; repeatable. When given, "
                        "replaces the default at-least-one-rendered-output condition"
                    ),
                },
                {
                    "name": "--screenshot",
                    "type": "path",
                    "description": "Write a full-page screenshot to this path on failure",
                },
                {
                    "name": "--port",
                    "type": "integer",
                    "default": 0,
                    "description": (
                        "Serve a directory target on this fixed port (0 = ephemeral); "
                        "for exports that reference themselves by absolute URL"
                    ),
                },
            ],
            "output_fields": _VERDICT_FIELDS,
            "errors": ["usage", "unavailable", "internal"],
            "outcomes": ["check_failed"],
            # Self-contained probe for conformance tooling: a refused loopback
            # connection produces a normal failure verdict (the check_failed
            # outcome) instantly, without needing a real shinylive export.
            "example": {"args": ["check", "http://127.0.0.1:1/", "--timeout", "5"]},
        },
        {
            "name": "schema",
            "description": (
                "Print this contract as JSON. The contract is always JSON; the "
                "global --output selects the format of errors."
            ),
            "effects": "read_only",
            "cardinality": "single",
            "output_fields": [
                {"name": "clispec", "type": "string", "description": "The CLI Spec dialect"},
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "description", "type": "string"},
                {
                    "name": "output",
                    "type": "object",
                    "description": "Default format on a TTY and when piped",
                },
                {"name": "global_args", "type": "array", "items": {"type": "object"}},
                {"name": "commands", "type": "array", "items": {"type": "object"}},
                {"name": "errors", "type": "array", "items": {"type": "object"}},
                {"name": "outcomes", "type": "array", "items": {"type": "object"}},
            ],
            "errors": ["usage"],
        },
    ],
    "errors": [
        {
            "kind": "usage",
            "exit_code": 2,
            "retryable": False,
            "description": (
                "The invocation was wrong: a missing or invalid target, an unknown "
                "flag, or a flag that does not apply. Nothing was checked."
            ),
        },
        {
            "kind": "unavailable",
            "exit_code": 4,
            "retryable": False,
            "description": (
                "Chromium is not installed for playwright; run `playwright install chromium`."
            ),
        },
        {
            "kind": "internal",
            "exit_code": 5,
            "retryable": False,
            "description": (
                "shinylive-check itself failed and reached no verdict: an unexpected "
                "error in the tool or the browser. stderr carries the traceback above "
                "the envelope. Distinct from exit 1, which means the app was judged "
                "and found broken."
            ),
        },
    ],
    "outcomes": [
        {
            "code": 1,
            "name": "check_failed",
            "description": (
                "The app did not boot cleanly: console or page errors, Shiny output "
                "errors, an HTTP failure, or no render within the timeout. stdout "
                "carries the verdict."
            ),
        }
    ],
}
