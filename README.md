# shinylive-check

Smoke-check an exported [Shinylive](https://shiny.posit.co/py/get-started/shinylive.html) site in headless Chromium: fail CI on boot failures, console errors and unrendered outputs.

*An independent project, not affiliated with or endorsed by Posit.*

[![PyPI](https://img.shields.io/pypi/v/shinylive-check)](https://pypi.org/project/shinylive-check/)
[![CI](https://github.com/rvben/shinylive-check/actions/workflows/ci.yml/badge.svg)](https://github.com/rvben/shinylive-check/actions/workflows/ci.yml)

```console
$ shinylive-check site
OK: 1 output rendered in 5.7s at http://127.0.0.1:54143/
```

And when the export is broken, the check fails with the reason, not a spinner:

```console
$ shinylive-check site
FAIL (error): 2 console/page error(s) at http://127.0.0.1:54318/
  preload error:Traceback (most recent call last):
  File "<exec>", line 239, in _start_app
  File "<exec>", line 121, in _install_requirements_from_dir
  ...
ValueError: Can't fetch metadata for 'shinylive-check-nonexistent-package-fixture'. Please make sure you have entered a correct package name and correctly specified index_urls (if you changed them).
$ echo $?
1
```

## Why

`shinylive export` turns a Shiny app into static files, and static files are easy to deploy: GitHub Pages, S3, any web server. That is the appeal, and it is also the trap. A static host can only fail by not serving files, so every check you already have reports success no matter what: the deploy job is green, `curl` gets a 200, the uptime monitor is happy. Everything that can actually go wrong happens later, inside the visitor's browser, after pyodide boots:

- a `requirements.txt` entry micropip cannot resolve (a typo, a package with no wasm-compatible wheel);
- a Python exception while the app starts (an import that only breaks under pyodide, a file the export did not include);
- a Shiny output that renders as an error;
- a JavaScript error from an asset or an extension package.

Any of these leaves the page a permanent spinner while every server-side signal says the deploy worked. The only observer that can tell the difference is a real browser, so `shinylive-check` boots one: it serves the export (or probes the deployed URL), opens it in headless Chromium, waits for the app to render, and exits non-zero with the actual traceback when it does not.

## Install

### Homebrew

```sh
brew install rvben/tap/shinylive-check
```

Install Chromium once after installing the formula:

```sh
"$(brew --prefix shinylive-check)/libexec/bin/playwright" install chromium
```

### Other installation methods

```sh
uv tool install shinylive-check   # or: pip install shinylive-check
playwright install chromium       # once, the browser it drives
```

Requires Python 3.10+. The only dependency is `playwright`.

## Use

The target is either an exported site directory, served on a loopback port for the duration of the check, or the http(s) URL of a site that is already deployed:

```sh
shinylive-check site
shinylive-check https://example.github.io/my-app/
```

Passing means: the page loaded, no console or page error occurred, no Shiny output is in an error state, the Shiny runtime was detected on the page, and at least one Shiny output rendered non-empty content. After the first successful render the page gets half a second to settle, so an error thrown right after the render still fails the check instead of racing it.

The runtime check is what stops a static shell from passing. An export serves its HTML before pyodide boots, so any selector naming something in that shell matches just as well on a dead app as on a live one; `window.Shiny` and the `shiny-bound-*` classes are added by JavaScript and cannot. `shiny_detected` in the verdict reports it.

- `--timeout SECONDS` (default 180): how long the app gets to render, and the budget for loading the page. A cold pyodide boot loads tens of megabytes of wasm, so the default is generous; a warm CI cache usually finishes in seconds.
- `--expect SELECTOR` (repeatable): CSS selectors that must be visible, replacing the default at-least-one-rendered-output condition. For apps whose UI is inputs only, or when "booted" means something specific: `--expect "#sales" --expect ".value-box"`. The runtime check still applies, and the selectors must be satisfied at the same moment, not one at a time.
- `--screenshot PATH`: write a full-page screenshot when the check fails, for the CI artifact that shows what the visitor would have seen.
- `--fail-on-resource-error`: also fail when the browser could not load a resource. Off by default, because a missing sourcemap, a stale cache-busted webfont or an image nobody deployed is not the app being broken, and a check that reddens CI for one is a check a team learns to ignore. Failed loads are reported either way, as `resource_errors`.
- `--ignore-console SUBSTR` (repeatable): do not fail on a console or page error containing this text, matched as a case-sensitive substring. For the library that prints an error that is not one, so the choice is exempting the message rather than switching off the check. What matches is still counted and still printed, as `ignored_errors`, on a pass as much as on a failure; an error the pattern does not match still fails as before. A pattern reaches what the app said and nothing else: a failed load keeps its own switch, and what the check itself reports about a site that would not load or that answered with an error status is never exempted, since that is the reason the check ended rather than something it ignored. An empty pattern is refused, since it would match everything.
- `--port PORT`: serve a directory target on a fixed port instead of an ephemeral one, for exports that reference themselves by absolute URL.
- `-o, --output auto|text|json` (default `auto`): the verdict format on stdout. `auto` emits JSON when stdout is not a TTY, the human-readable form otherwise.

Exit codes:

| code | meaning |
| --- | --- |
| 0 | the app booted and rendered |
| 1 | the check failed; stdout carries the verdict |
| 2 | usage error |
| 4 | environment error: Chromium is not installed for playwright |
| 5 | internal error: shinylive-check itself failed and reached no verdict |
| 130 | interrupted by SIGINT, Ctrl-C included, before a verdict |
| 141 | whatever was reading stdout closed it, as a pipe into `head` does |

Exit 5 is deliberately not 1. A crash in the tool and a verdict of "this app is broken" are different facts, and a CI job that cannot tell them apart reports a tooling failure as a failing app. On exit 5 stdout stays empty and stderr carries the traceback above the JSON envelope, so the last line is still machine-readable.

130 and 141 rest on the same distinction, and both are the number a shell reports for the signal involved. A run stopped by Ctrl-C was abandoned rather than judged, and a reader that stopped reading is not a fault at all: `shinylive-check site | head` ends like every other filter in that pipeline, silently, rather than with a traceback blaming the tool.

## In CI

The check slots between export and deploy, so a broken export never replaces a working site:

```yaml
- name: Export
  run: uvx shinylive export app site

- name: Check the export boots before it deploys
  run: |
    pip install shinylive-check
    playwright install --with-deps chromium
    shinylive-check site --screenshot boot-failure.png

- name: Keep the evidence when it fails
  if: failure()
  uses: actions/upload-artifact@v4
  with:
    name: boot-failure
    path: boot-failure.png
```

The same command probes the live site after the deploy, or from a scheduled workflow:

```sh
shinylive-check https://example.github.io/my-app/
```

## For scripts and agents

The CLI is built for machine consumption as well as terminals. Piped, the verdict is JSON:

```console
$ shinylive-check site | jq .
{
  "target": "site",
  "ok": true,
  "url": "http://127.0.0.1:54535/",
  "seconds": 4.7,
  "outputs": 1,
  "shiny_detected": true,
  "errors": [],
  "errors_total": 0,
  "output_errors": [],
  "output_errors_total": 0,
  "resource_errors": [],
  "resource_errors_total": 0,
  "ignored_errors": [],
  "ignored_errors_total": 0,
  "expects": [],
  "failure": null,
  "screenshot": null
}
```

The message lists are what the page said, and a page can say a great deal: long messages are truncated and each list is clipped to the earliest 50 entries, with a marker naming what was dropped. Every one of them has a `_total` beside it holding the number that actually occurred, so read that for a count and never `len(errors)`.

`failure` is `null` on a pass and one of `error`, `output-error`, `resource-error`, `timeout`, `navigation` or `http-status` otherwise. Exit code 1 is a declared outcome, not an error: the check ran and the verdict is on stdout, like `grep` finding nothing. Real errors (a wrong invocation, a missing browser, an internal failure) go to stderr as a one-line JSON envelope, `{"error": {"kind": ..., "message": ..., "hint": ...}}`, when the output format is JSON.

`shinylive-check schema` prints the whole contract, commands, arguments, output fields, error kinds and exit codes, as machine-readable JSON in [The CLI Spec](https://clispec.dev) v0.3 dialect. It means the contract in every working directory, so reading the interface never depends on where you happen to stand; a directory named `schema` is checked as `./schema`. The `check` keyword resolves the other way, since it is only ever an alias: `shinylive-check check` with no target after it checks a directory named `check`.

## How it works

- A directory target is served on `127.0.0.1` by a quiet HTTP server for the duration of the check; a URL target is loaded as-is. An HTTP status of 400 or above fails the check immediately.
- Headless Chromium loads the page with uncaught page errors and console errors collected from the first byte.
- Chromium writes a console error for every resource it could not load, so those are told apart from the app's own errors before the verdict is decided: the browser's carry no console arguments and are located at the URL that failed, where a real `console.error` carries its arguments and is located in the page's own script. Not a text match, so an app cannot silence itself by printing the browser's wording.
- Every frame is scanned, not just the main one, because Shinylive apps often run inside an iframe; a selector evaluated only on the top document would silently match nothing.
- A Shiny output counts as rendered when it has non-empty content and is not in an error state; outputs carrying `shiny-output-error` fail the check with the error text.
- The page is polled twice a second until the verdict is clear one way or the other, or the timeout passes.
- The screenshot, when requested, is captured only on failure and never masks the verdict.

## Limitations

- The default pass condition needs at least one Shiny output. An app whose UI is entirely inputs or static content never satisfies it; give `--expect` a selector that means "booted" for that app.
- It judges the boot, not the app's behavior. It does not click through the UI; for that, write Playwright tests against your app.
- Packages that are not bundled into the export are fetched by micropip at boot, exactly as they are for a real visitor, so checking such an export needs network access.
- Chromium only, by design: one engine, deterministic in CI.

## When the app outgrows a static export

Shinylive is a great fit for demos, docs and small tools, and its constraints are the flip side of the free hosting: the entire app ships to every visitor, so there are no secrets, no private data, no database connections and no server-side compute, and every package must have a wasm-compatible wheel. When an app crosses that line and needs a real server process, [ShinyHub](https://github.com/rvben/shinyhub) is a self-hosted platform for exactly that: deploy Shiny apps (Python and R) to your own server with one command, with OAuth/OIDC login, per-app access control, idle hibernation and scaling.

Related: [shiny-plotly](https://github.com/rvben/shiny-plotly) renders plotly figures in Shiny for Python without the shinywidgets layer, and runs under Shinylive; its Pages demo is deployed behind exactly this kind of pre-deploy boot check.

## Development

```sh
make sync         # uv sync --all-groups
make browsers     # playwright install chromium, once
make check        # lint, typecheck, CLI tests, browser tests, wheel check, floor check
make schema-check # score the schema against The CLI Spec (needs clispec on PATH)
```

`make test` runs the CLI contract without a browser: usage errors, exit codes, the JSON envelope, the schema command. `make test-browser` exports three fixture apps with a pinned shinylive and drives the CLI against them in headless Chromium: the green one passes, and each broken one (an exception at boot, an unresolvable package) fails for its own reason, so the failure detection is proven against real exports rather than assumed. `make check-wheel` installs the built wheel into a throwaway venv and tests that, so what ships is what was tested. `make check-floor` does the same with playwright at the oldest version `pyproject.toml` allows.

## License

MIT. See [LICENSE](LICENSE).
