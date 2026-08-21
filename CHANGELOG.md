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
- `errors_total` and `output_errors_total` in the JSON verdict: the numbers that
  occurred, which exceed the list lengths once a list is clipped. The text
  headline reports these, so it cannot understate a failure with a number that
  looks real.
- `shiny_detected` in the JSON verdict: whether the Shiny runtime was observed on
  the page. A timeout that did not see it says so in the text form, so a check
  whose selectors all matched does not read as a bug in the tool.
- `--fail-on-resource-error`, with `resource_errors` and `resource_errors_total`
  in the verdict and a `resource-error` failure kind. Resources the browser could
  not load are reported on a pass as well as a failure, so ignoring one does not
  mean hiding it.

### Fixed

- A page whose main thread stopped answering was never judged at all. Reading the
  page is the one thing the check cannot do on its own clock: `evaluate` is the
  only playwright call that takes no timeout, so an app running an infinite loop,
  a long synchronous WASM step or a pathological layout blocked the scan inside
  the browser, where the poll loop's deadline could not reach it. A `--timeout 5`
  check against such a page was measured still running at 45 seconds, with no
  verdict and not even the progress line, since the loop that prints it was never
  re-entered. Naming an `--expect` selector cost the same way for a different
  reason: `is_visible` documents its own timeout argument as ignored and waits
  playwright's 30 second default instead, once per selector per frame per poll,
  which turned an 8 second budget into 65 seconds. Frames are now asked whether
  they are still answering through a call playwright bounds in the driver rather
  than in the page, and one that is not answering is reported as the timeout it
  is. That question is asked once for a whole sweep, and the sweep is bounded by
  what is left of the budget, so neither many frames nor many selectors can buy a
  page more time than the check was given.
- An app whose output kept appearing and disappearing ran forever, ignoring
  `--timeout` entirely. The check consulted its deadline only while waiting for a
  first render; the other way back to waiting, a settle window withdrawing a pass
  because the output went away again, returned to the top of the loop without
  ever asking the clock. A page in that state was measured still running 60
  seconds into a `--timeout 3` check, with nothing on stdout, which in CI means a
  runner held until the job timeout. Out of budget having never rendered stably
  is now reported as a timeout, like any other app that did not render.
- Ctrl-C did not stop a check. The interrupt unwound the connection to the
  browser and the teardown then asked that connection to close the browser, so
  the reply never came: the run hung until it was killed, which in CI means a
  runner held until the job timeout. It now stops at once, exits 130 with an
  `interrupted` error kind on stderr rather than a traceback, and leaves no
  browser behind. The code is deliberately not 1, so a wrapper cannot read an
  abandoned run as an app that was judged and found broken.
- `--` did not end the flags when the output format was worked out. A target
  named `-ojson` sitting after the marker was read as a format request as well as
  a target, so `shinylive-check -- -ojson extra` answered its usage error on a
  terminal as a JSON envelope with the usage block dropped, decided by what the
  directory happened to be called. The search now stops at the marker the way
  argparse does; a request made before it still stands.
- Piping a verdict into `head` was reported as a bug in the tool. A verdict is
  far larger than the 64 KB a pipe holds, so the reader leaves mid-transfer and
  the write itself fails: `shinylive-check site -o json | head` reached the
  catch-all, which printed a traceback and a JSON envelope asking the user to
  report a bug in shinylive-check, and exited 5. Output small enough to sit in
  the pipe buffer failed later and less visibly, at the interpreter's own flush,
  where `--help` and `schema` exited 120 with a complaint about an exception
  ignored on flushing stdout. Both paths now exit 141, the number a shell
  reports for a filter stopped by SIGPIPE, and say nothing at all, since whoever
  would have read it has gone. The code and a `broken_pipe` error kind are
  declared in the schema, in the README and in `--help`.
- A verdict about a page whose text stdout cannot encode was reported as a bug
  in the tool. Python opens stdout strict and stderr with escaping, so on a
  terminal or a redirect that is not UTF-8, an app erroring in emoji or in an
  accented language raised UnicodeEncodeError inside the check: exit 5 and an
  invitation to file a bug, in place of the exit 1 and the failing app it had
  correctly found. Characters stdout cannot spell are now escaped the way stderr
  already escapes them, so the diagnosis arrives whole and the verdict is the
  one the check reached.
- The `schema` command declared `usage` as the only way it can fail, while the
  contract it prints listed four more. It now declares every kind but
  `unavailable`, which needs a browser this command never launches.
- The README's exit-code table omitted 130, real since Ctrl-C was fixed. Both
  tables, the README's and the one in `--help`, are now held to the schema by a
  test, and the schema's own error kinds are held to the identifier spelling
  that The CLI Spec requires: a hyphen in one invalidates the whole contract
  while still scoring Excellent.
- `--expect` certified a genuinely dead app. An export serves its HTML shell
  before pyodide boots, so a selector naming anything in that shell was satisfied
  by a page the runtime never reached: against an app that raises at import,
  `--expect body` returned ok in 1.4 s, two seconds before the traceback a bare
  check catches. Passing now requires the Shiny runtime to be detected as well,
  which nothing in the static shell can fake, so reaching for `--expect` to be
  more specific no longer buys a weaker check.
- An output that rendered and then vanished inside the settle window was reported
  as a pass. The window re-scanned the page and then kept the larger of the two
  output counts, so the scan that saw the render decided the verdict and the scan
  that did not was discarded. The condition is now re-derived from the second
  scan, and a page that no longer meets it goes back to waiting.
- `--expect` selectors were satisfied one at a time and stayed satisfied, so
  `--expect "#loading" --expect "#chart"` passed on a page that showed a spinner
  and then failed. They must now be visible at the same moment, and the verdict
  reports the state at the end of the check rather than a high-water mark.
- A resource the browser could not load failed the check. Chromium writes a
  console error for every failed load, and every console error of that type was
  treated as the app being broken, so one missing sourcemap, webfont or image
  reddened CI for an app that boots perfectly. Failed loads are now told apart
  from the app's own errors by origin rather than by message text (the browser's
  carry no console arguments and are located at the URL that failed), reported as
  `resource_errors`, and left out of the verdict unless
  `--fail-on-resource-error` is given.
- `--timeout` did not govern loading the page. Navigation had its own hardcoded
  30 second limit, so a check pointed at a host that accepts the connection and
  then never answers took 30.7 s to give up on a `--timeout 3`, and no value
  below 30 could shorten it. `--timeout` is now the budget for the whole check,
  page load included. A `--timeout` that is not a positive, finite number of
  seconds is a usage error rather than a silent wait forever, which is how the
  browser reads a zero timeout. Finiteness is asked as well as size, because
  `nan` compares false against every bound and `inf` compares true against one
  it never reaches: a positivity test alone lets both through to the browser,
  where converting what is left of such a budget raises and reports the caller's
  typo as a bug in this tool.
- A Shiny output that entered an error state during the 500 ms settle window was
  reported as a pass. Console errors arrive as events and were re-read, but an
  output failing inside the window is DOM state and was only visible to another
  scan, which is now performed.
- `--port` on a port already in use raised an uncaught `OSError`. It is now a
  usage error (exit 2) with a hint, since the caller can act on it.
- `--output json check TARGET` did not reach the `check` command, though the
  contract declares `--output` global and `check TARGET` equivalent to the bare
  form. All four flag spellings are now recognised ahead of the command keyword.
- A directory literally named `check` was unreachable as a target. The first fix
  handled only the bare `shinylive-check check`; the keyword is now read as an
  alias whenever that leaves a valid invocation, and as a target when it does
  not, so `shinylive-check check --timeout 15` reaches the directory. With no
  such directory it reports the missing target rather than a path nobody typed.
  `schema` deliberately resolves the other way and always prints the contract,
  which cannot be allowed to depend on the working directory; it now says on
  stderr that `./schema` checks a directory of that name.
- A timeout reported the requested timeout rather than measured wall time, so a
  run could claim it gave up in less time than it actually took.
- A screenshot that could not be written (unwritable directory, full disk) raised
  `OSError` and discarded the verdict the caller came for.
- The server followed symlinks out of the export directory, so an export
  containing a link to a file elsewhere on the host served that file to the page.
  A link that points at itself is refused the same way rather than crashing the
  handler: resolving a loop raises rather than returning a path on every Python
  before 3.13, and an uncaught one closes the connection with no reply and
  prints a traceback that reads as this tool breaking rather than as an export
  holding a link it may not follow.
- Text chosen by the checked page is stripped of the characters that steer a
  reader and indented under the verdict, so it cannot repaint the terminal or
  forge a headline. What is dropped is decided by Unicode category rather than by
  a list of ranges: escape sequences and the rest of `Cc`, but also `Cf`, where
  the bidi overrides live - `U+202E` reverses the order the rest of a line is
  read in while leaving every byte of it intact - and `Cs`, whose members encode
  to neither UTF-8 nor JSON and would turn a page's console message into this
  tool's own crash.
- A malformed `--expect` selector was reported as the app failing. Chromium
  rejects a bad selector in more than one wording and only one was recognised, so
  `--expect "div:nonsense(1)"` ran the full timeout and returned
  `"failure": "timeout"` with `satisfied: false`: the caller's typo described as a
  dead app, exit 1 instead of exit 2, after 20 seconds. Selectors are now compiled
  against a blank page before the site is loaded, which asks whether they parse
  rather than what the rejection was called, so the answer arrives in under a
  second and does not depend on the site being reachable.
- The checked page could choose the size of this tool's stdout, which is what a
  CI job stores and a script has to parse. A 20 MB console error produced a 20 MB
  JSON verdict, 5,000 smaller ones produced 50 MB, and 200 errors written in
  emoji produced 6 MB because JSON escapes each one to twelve bytes. Messages are
  now truncated at 10,000 encoded bytes and the lists clipped to the earliest 50,
  each with a marker naming what was dropped, which bounds the verdict at roughly
  500 KB whatever the page says and in whatever alphabet.

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
