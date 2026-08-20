# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
