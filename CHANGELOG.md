# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Exit code 5 and a matching `internal` error kind for a failure in the tool
  itself. Previously an unexpected error exited 1 with an empty stdout and a raw
  traceback, which a script cannot tell apart from the declared `check_failed`
  outcome, so a broken checker reported a broken app. The traceback still prints,
  above the JSON envelope, so the last line of stderr stays machine-readable.
- A progress line on stderr every 15 seconds while waiting for an app to render.
  A cold pyodide boot against the 180 second default timeout was previously
  silent for long enough to read as a hung job.
- Examples and an exit-code table in `--help`.

### Fixed

- A Shiny output that entered an error state during the 500 ms settle window was
  reported as a pass. Console errors arrive as events and were re-read, but an
  output failing inside the window is DOM state and was only visible to another
  scan, which is now performed.
- `--port` on a port already in use raised an uncaught `OSError`. It is now a
  usage error (exit 2) with a hint, since the caller can act on it.
- `--output json check TARGET` did not reach the `check` command, though the
  contract declares `--output` global and `check TARGET` equivalent to the bare
  form. All four flag spellings are now recognised ahead of the command keyword.
- A directory literally named `check` was unreachable as a target.
- A timeout reported the requested timeout rather than measured wall time, so a
  run could claim it gave up in less time than it actually took.
- A screenshot that could not be written (unwritable directory, full disk) raised
  `OSError` and discarded the verdict the caller came for.
- The server followed symlinks out of the export directory, so an export
  containing a link to a file elsewhere on the host served that file to the page.
- Text chosen by the checked page is stripped of control characters and indented
  under the verdict, so it cannot repaint the terminal or forge a headline.
- Reported page text is truncated at 10,000 characters, with the true length
  named in the marker. An app dumping a large repr into `console.error` used to
  choose the size of this tool's stdout: a 20 MB error produced a 20 MB JSON
  verdict, which is what a CI job stores and a script has to parse.

## [0.1.0] - 2026-08-20

### Added

- `shinylive-check <site-dir | url>`: serve an exported Shinylive site on a
  loopback port (or probe a deployed URL) and judge the boot in headless
  Chromium, failing on console and page errors, Shiny output errors, HTTP error
  statuses and apps that never render.
- `--expect` selectors that replace the default at-least-one-rendered-output
  condition, `--screenshot` for a full-page capture on failure, `--timeout`
  (default 180 seconds), and `--port` for exports that reference themselves by
  absolute URL.
- Output contract for scripts and agents: JSON verdict when stdout is piped
  (`-o auto|text|json`), one-line JSON error envelopes on stderr, exit code 1
  declared as a data outcome rather than an error, and a `schema` command that
  prints the full contract in The CLI Spec v0.3 dialect.
- End-to-end tests against real `shinylive export` output: a green fixture app
  passes, and fixtures broken in distinct ways (an exception at boot, an
  unresolvable package) each fail for their own reason.
