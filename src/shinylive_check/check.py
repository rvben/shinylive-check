"""Drive an exported Shinylive site in headless Chromium and judge the boot.

Shinylive failures are silent on the server side: the export succeeds, the deploy
succeeds, every asset serves with a 200, and the page is dead anyway because
something broke inside the browser (a pyodide crash, a micropip resolution
failure, an app exception at boot). Only a real browser observes that, so this
module boots one and watches three surfaces: uncaught page errors, console
errors, and the Shiny output elements inside the app frame.
"""

from __future__ import annotations

import hashlib
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from playwright.sync_api import ConsoleMessage, Frame, Page, Request, Response, sync_playwright
from playwright.sync_api import Error as PlaywrightError


class BrowserMissingError(RuntimeError):
    """Chromium is not installed for this playwright; the environment is unusable."""


class SelectorError(ValueError):
    """An --expect selector is not valid CSS."""


# An export may put the app in an iframe, so every frame is scanned; selectors
# evaluated only on the main frame would silently match nothing there. Outputs
# carrying shiny-output-error are the app saying a render failed; outputs with
# content are the render succeeding.
#
# `runtime` and the two element counts are the evidence that the Shiny runtime is
# actually alive. Shiny defines window.Shiny and adds every shiny-bound-* class from
# JavaScript, so none of the three can appear in the static shell an export serves
# before pyodide boots: they separate a booting app from a dead one, which nothing
# in the shell's own HTML can do.
#
# What comes back is bounded in the page, not after it arrives: an app can hold
# any number of failed outputs carrying any amount of text, and every character of
# that would otherwise cross the browser connection and be held here to report
# fifty of them. Each error that is cut is handed over with the length it was cut
# from, since this side can no longer measure what it was never sent, and one that
# is not cut is handed over with no length at all: a length reported for text that
# arrived whole would be describing a cut that never happened.
#
# That length is in UTF-16 code units, which a verdict is not read in. A Python
# string is measured in code points, so 40,000 emoji are 80,000 units and a reader
# was told the message ran to twice what anyone had written. Counting the code
# points here instead means walking the whole string, which is the cost this bound
# exists to avoid: 200.1 ms against 1.4 ms on 50 MB, paid on text the page chose
# the length of. Asking whether the two counts differ is free, because a surrogate
# pair is the only thing that makes them differ and one regex answers it without
# allocating, early-exiting on the first pair. So the length travels with whether
# it is a character count, and is reported as a bound when it is not one.
#
# The answer is the page's account either way, as the length always was. A page
# that lies about either buys itself a wrong number beside its own error, which is
# not worth the walk.
#
# How many is asked for rather than fixed here, because the page chooses how many
# frames it holds as well as what each one says: a ceiling written into the script
# is one every frame is given, and the sweep pays their sum. The caller passes
# what the frames before this one left.
_FRAME_SCAN_TEMPLATE = """
(kept) => {
  const outs = Array.from(document.querySelectorAll(".shiny-bound-output"));
  const rendered = outs.filter(
    (el) => !el.classList.contains("shiny-output-error") && el.innerHTML.trim().length > 0
  ).length;
  // The two bounds this script keeps are the length of each error and the number
  // of them, and both are kept with methods the page could otherwise replace: an
  // overridden `push` appends as many times as it likes after the room has been
  // checked, and an overridden `slice` hands back the whole string it was asked
  // to cut. `_FRAME_SCAN_INIT` takes them before the page runs and puts them out
  // of its reach. Appending through `defineProperty` also settles a setter on
  // `Array.prototype`, which an indexed assignment would consult and which can
  // grow the very array it intercepts.
  //
  // Both are CALLED rather than reached through: a call consults no property, so
  // there is nothing on the way to the method for the page to own. Reaching one
  // through a property leaves the capture worth nothing, since a captured
  // function is an ordinary mutable object and holding a reference to it is not
  // holding its `call`, which the page may give it an own copy of; the cut is
  // therefore bound to its argument while the page has not yet run.
  //
  // The fallback is what this script did before the capture existed, and is
  // reachable only if the init script did not run, which is not something a page
  // can arrange. It is here so that a frame the capture never reached is still
  // swept, rather than throwing and being skipped in silence.
  const safe = window.%(safe)s || {
    cut: Function.prototype.call.bind(String.prototype.slice),
    define: Object.defineProperty,
    paired: (text) => /[\\uD800-\\uDBFF]/.test(text),
  };
  const errors = [];
  const keep = (value) => {
    safe.define(errors, errors.length, {
      value: value, writable: true, enumerable: true, configurable: true,
    });
  };
  let errorTotal = 0;
  for (const el of document.querySelectorAll(".shiny-output-error")) {
    const text = (el.textContent || "").trim();
    if (!text) continue;
    errorTotal += 1;
    if (errors.length < kept) {
      const oversize = text.length > %(chars)d;
      keep({
        text: safe.cut(text, 0, %(chars)d),
        length: oversize ? text.length : null,
        exact: !oversize || !safe.paired(text),
      });
    }
  }
  return {
    bound: outs.length,
    inputs: document.querySelectorAll(".shiny-bound-input").length,
    rendered,
    errors,
    errorTotal,
    runtime: typeof window.Shiny !== "undefined",
  };
}
"""

_POLL_MILLIS = 500
_SETTLE_MILLIS = 500

# How long one sweep may spend proving the page is still answering before it is
# treated as wedged. A frame that is alive answers in single-digit milliseconds,
# so this is three orders of magnitude more room than the question needs.
_RESPONSIVE_MILLIS = 2000

# The least a sweep is ever given, however little of the budget is left. The last
# sweep of a check runs at or past its own deadline, and asking a live page to
# answer within the zero milliseconds remaining would report an app that had
# rendered as one that never did.
_RESPONSIVE_FLOOR_MILLIS = 250

# Newlines and tabs survive because tracebacks need them, and because a line
# break is the one piece of formatting the page is allowed to ask for: `_detail`
# grants it by indenting the new line, so it cannot reach column 0.
_KEEP_CONTROLS = frozenset("\n\t")

# What is dropped is decided by what a character IS, not by the ranges anyone
# thought to list. `Cc` control, `Cf` format - which is where the bidi overrides
# live, and reversing the order a line reads in is screen control even though
# every byte of the text is intact - and `Cs`, whose members encode to neither
# UTF-8 nor JSON and would make a page's console message this tool's own crash.
_STEERING_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})

# The page decides how long its errors are, so the tool has to decide how long its
# verdict is. Comfortably more than any real traceback, far less than a CI log
# budget. The head is kept because that is where the diagnosis lives.
#
# Spent in ENCODED bytes, not characters: the verdict is JSON, which escapes
# non-ASCII, so one emoji costs twelve bytes and a character cap bounds nothing a
# CI job actually stores.
_MAX_REPORTED_BYTES = 10_000

# Bounding each message is not bounding the verdict: an app erroring inside a
# render or reconnect loop emits thousands before the first poll sees any of them,
# and the count then carries the size the length cap just removed. The earliest
# errors are kept because a cascade's root cause is at its start.
_MAX_REPORTED_ITEMS = 50

# How much of one message is kept at all, and so how much of it is ever read.
# Cutting it takes one slice; deciding what survives takes a category lookup per
# character, which is why the cut happens as the message arrives and the deciding
# waits for the verdict. Reading a 50 MB console.error whole cost 2743 MB and
# seconds of a budget the caller set for the app to boot.
#
# Twice the reported ceiling. A character that survives costs at least one byte of
# that ceiling, so ordinary text reaches it first and the slack is there for text
# with control characters through it; a page reaching this bound is padding with
# characters that are stripped, which buys it no more of the verdict than it would
# have had.
_MAX_SCANNED_CHARS = 20_000

# How many messages are kept with their text, against the fifty a verdict prints.
# The margin is what keeps the classification in `_partition` deferred to the
# verdict for any page that is merely noisy; past it a message is classified as it
# arrives and counted rather than kept, so the totals stay exact while the memory
# stops following the page's lead.
_MAX_RETAINED_ITEMS = 500

# How many failed loads are remembered, so that a console error can be recognised
# as one. At sixteen bytes a load this is about 22 MB held, against the 1,066 MB
# the browser spent to fail 50,000 of them; the bound is here because memory has
# to be bounded by something this check promises rather than by what a browser
# happens to cost today.
#
# Past it the check can no longer tell the browser's report of a failed load from
# the app's own error, and says the failed load, which is what an unbounded set
# would have said: what fills this is failed loads, so what arrives past it is
# overwhelmingly one of them. Reading them as the app's instead makes the bound
# decide the verdict, failing a check that asked not to fail on failed loads and
# printing the browser's sentence about a 404 as the app's diagnosis. Nothing is
# hidden either way, since a failed load is still counted, still listed, and still
# fails the check for a caller who asked about failed loads.
_MAX_TRACKED_LOADS = 250_000

# How much of a URL is used to remember it by. A URL is text the page chose the
# length of, like any other, and a rejected request carries whatever it asked for.
# Applied on both sides of the match, so it changes no comparison unless two URLs
# agree for two thousand characters and differ after.
_MAX_URL_CHARS = 2_048

# Where the captured methods are kept. Non-writable and non-configurable, so the
# page cannot put its own object there, and non-enumerable so that a page walking
# `window` is not handed the name.
_SAFE_BUILTINS = "__shinylive_check_safe__"

# Run in every frame of the page before anything the page ships, which is what
# makes the three methods it takes the real ones. Everything the scan is built
# from belongs to the page, and a bound kept with a method the page supplies is a
# bound the page sets: replacing `slice` alone took the same ten failed outputs
# from 3.8 s and 1117 MB to 12.0 s and 3183 MB, reproducing through the cut the
# cost the cut was added to remove. What each override is worth multiplies by the
# number of errors kept, which is another thing the page picks.
#
# Only the methods that decide what crosses the connection are taken: the two the
# bounds are kept with, and the one that says whether a reported size is a size.
# The rest of what the scan touches costs the page its own time and nothing here,
# and is bounded by the sweep's responsiveness deadline instead.
#
# Each is bound to its argument here rather than reached for later, so that the
# scan calls it instead of looking it up. Freezing this object freezes the
# references and not the functions they name, and a function is an ordinary object
# whose `call` the page may give it an own copy of; every property on the way to
# the method is therefore read here, while the page has not yet run, and what is
# stored can only be called. Reached through its `call` instead, the captured
# method leaves the page the whole of what it had before it was captured: 3.8 s
# and 1117 MB became 13.5 s and 3188 MB on the same ten outputs, against a page
# whose `slice` was untouched.
#
# `test` is the one capture that is not enough on its own, because it does not do
# the matching: it looks `exec` up on the regex it was handed and calls that, so a
# page owning `RegExp.prototype.exec` answers every question this asks. The regex
# is built here and given its own `exec` that cannot be replaced, which is the
# same defence one level down. The regex object itself is reachable from nowhere
# but the bound function, which does not hand it back.
_FRAME_SCAN_INIT_TEMPLATE = """
(() => {
  const cut = Function.prototype.call.bind(String.prototype.slice);
  const define = Object.defineProperty;
  const pairs = /[\\uD800-\\uDBFF]/;
  define(pairs, "exec", {
    value: RegExp.prototype.exec,
    writable: false,
    enumerable: false,
    configurable: false,
  });
  const paired = Function.prototype.call.bind(RegExp.prototype.test, pairs);
  if (window.%(safe)s) return;
  define(window, "%(safe)s", {
    value: Object.freeze({ cut: cut, define: define, paired: paired }),
    writable: false,
    enumerable: false,
    configurable: false,
  });
})();
"""

_FRAME_SCAN_INIT = _FRAME_SCAN_INIT_TEMPLATE % {"safe": _SAFE_BUILTINS}
_FRAME_SCAN = _FRAME_SCAN_TEMPLATE % {"chars": _MAX_SCANNED_CHARS, "safe": _SAFE_BUILTINS}


def _json_cost(char: str) -> int:
    """Bytes this character costs once JSON-encoded, ASCII-safe.

    Exact for the alphabet that survives ``_printable``'s stripping, which is
    what it is used on. The three characters where this would disagree with
    ``json.dumps`` (backspace, form feed, DEL) are all stripped before costing,
    and a test pins that agreement across the whole surviving range.
    """
    code = ord(char)
    if code > 0xFFFF:
        # Encoded as a surrogate pair, so two \uXXXX escapes.
        return 12
    if code > 0x7F:
        return 6
    if char in '"\\':
        return 2
    if code < 0x20:
        return 2
    return 1


def _printable(text: str, written: int | None = None, *, exact: bool = True) -> str:
    """Strip control characters from text the checked page chose, and bound it.

    Console messages and page errors are written by the page, and the verdict
    they land in is read in a terminal. Escape sequences there can erase lines
    and move the cursor, and a bidi override can reverse the order the rest of a
    line is read in without touching its bytes, so a hostile or broken page could
    repaint the check's own verdict. Text is reported; control of the screen is
    not.

    Size is bounded for the same reason: an app that dumps a large repr into
    console.error would otherwise choose the size of this tool's stdout, and the
    verdict is what a CI job stores and a script parses. The budget is spent in
    encoded bytes, so the ceiling does not depend on the alphabet the page picked.

    Reading is bounded as well as reporting, in one pass that stops at whichever
    ceiling comes first, because deciding what survives costs a category lookup
    per character: stripping a 50 MB console.error whole spent seconds of the
    caller's boot budget and 2.7 GB of memory to produce ten kilobytes of verdict.
    Messages normally arrive here already cut to that length, and this repeats the
    cut rather than trusting it, since one caller's oversight would otherwise be
    the whole of the bound.

    ``written`` is how long the message was where it was written, for text that
    was cut short before it got here. The reported size is always that size rather
    than what arrived, so a clipped error is never read as a short one.

    ``exact`` is false when that size was counted in UTF-16 code units over text
    holding a surrogate pair, which is how a page counts and not how this reads: it
    is then an upper bound on the characters rather than a count of them, and says
    so. Reporting it as a count told a reader a 40,000 character error ran to
    80,000; reporting nothing would throw away the magnitude, which is the part
    worth having.
    """
    size = len(text) if written is None else written
    kept: list[str] = []
    spent = 0
    clipped = size > len(text) or len(text) > _MAX_SCANNED_CHARS
    for char in text[:_MAX_SCANNED_CHARS]:
        if char not in _KEEP_CONTROLS and unicodedata.category(char) in _STEERING_CATEGORIES:
            continue
        spent += _json_cost(char)
        if spent > _MAX_REPORTED_BYTES:
            clipped = True
            break
        kept.append(char)
    clean = "".join(kept)
    if clipped:
        bound = "" if exact else "at most "
        return f"{clean}... [truncated, {bound}{size} characters total]"
    return clean


@dataclass
class _Bucket:
    """Messages of one kind, and how many of that kind there were.

    The two travel together because they diverge: the verdict reports the earliest
    of them and a flooding page is counted without being kept, so the length of
    ``items`` answers a question nobody asked. Every field of the verdict that
    holds page-controlled text is rendered from one of these, which is what makes
    the ``_total`` companions exact by construction rather than by remembering.
    """

    items: list[_Said]
    total: int


def _bounded(bucket: _Bucket) -> list[str]:
    """Render a bucket, keeping its head and naming what was cut.

    The companion count (``errors_total``) is what keeps this honest: a reader
    must never mistake the length of a clipped list for the number of things that
    actually went wrong. That count is the bucket's own rather than the length of
    what it carries, because a message can be counted without being kept.

    Sanitising happens here rather than when a message arrives, and is the reason
    the two are separable at all: it is a category lookup per character, so paying
    it on everything a flooding page says is paying it on ninety thousand messages
    to print fifty. What arrives is cut to a length instead, which is one slice.
    """
    kept = [
        _printable(item.text, item.written, exact=item.exact)
        for item in bucket.items[:_MAX_REPORTED_ITEMS]
    ]
    dropped = bucket.total - len(kept)
    if dropped > 0:
        kept.append(f"... [{dropped} further omitted, {bucket.total} total]")
    return kept


@dataclass
class ExpectResult:
    selector: str
    satisfied: bool


def _load_key(url: str) -> bytes:
    """How a failed load is remembered, and how a console error is matched to one.

    A digest rather than the URL, because a URL is text the page chose the length
    of, like any other: a request carries whatever it asked for, and a rejected one
    is remembered here. Sixteen bytes each instead of up to two thousand cost
    100,000 failed loads 8.7 MB where the URLs cost 203 MB, and the page no longer
    picks which. What is stored is never read back, only compared, so nothing is
    lost by storing something unreadable.

    Truncated before hashing, so a page cannot make one entry cost more by asking
    for a longer URL. Applied to both sides of the match, so it changes no
    comparison unless two URLs agree for two thousand characters and differ after.
    """
    return hashlib.blake2b(
        url[:_MAX_URL_CHARS].encode("utf-8", "surrogatepass"), digest_size=16
    ).digest()


@dataclass
class _Said:
    """One message about the page, with where the browser located it.

    ``origin`` is a digest of the URL a console error was located at, and is set
    only when the error carried no console arguments. That pair is the shape Chromium
    synthesises for a load it could not complete: no arguments, located at the
    resource rather than in the page's own script. Anything the app printed itself
    carries what console.error was called with, so the two never collide.

    Deliberately not a text match. Chromium's wording for a failed load is fixed,
    so a page that printed that sentence could otherwise silence its own errors.

    ``from_page`` is false for the two messages the CHECK writes rather than the
    page: a navigation that failed and an error status. They travel in this list
    because the verdict's ``errors`` is where a reader looks for why a check
    failed, but they are this tool's own words about a page that never ran, so no
    exemption may move them. ``net::ERR_`` is a plausible pattern to write for a
    third-party beacon, and it appears verbatim in the message about the site
    itself being unreachable.

    ``text`` is as much of the message as is kept and ``written`` is how long it
    was when the page wrote it, so a message cut short still reports the size it
    really was. ``exact`` is whether that size is a count of characters: it is
    false only for a page's account of text it cut, counted in the code units a
    surrogate pair takes two of, and it makes the size read as the bound it is.

    ``exempt`` is whether an ``--ignore-console`` pattern was in the message, and
    is decided when the message arrives rather than at the verdict, because
    ``text`` by then is only the head of it: a pattern the caller named must still
    reach a message that was too long to keep whole. Deciding it early costs
    nothing that the URL match's slack costs, since it depends on the text and the
    patterns and neither changes afterwards.
    """

    text: str
    written: int
    origin: bytes | None = None
    from_page: bool = True
    exempt: bool = False
    exact: bool = True


class _Heard:
    """Everything the page has said, and every load that failed under it.

    Both are lists a page writes and this process holds, so both are bounded here
    rather than at the verdict: clipping what is printed leaves what is kept
    following the page's lead, which is how a render loop's 91801 console errors
    became 1809 MB resident and 23.6 s spent against a 10 s budget.

    Past the retention cap a message is counted instead of kept, so what a reader
    is told to trust - the ``_total`` beside each list - stays exactly what
    occurred while the memory stops following the page's lead. Which total it
    joins is still decided at the verdict, like the kept ones: dropping the text
    is what the cap is for, and dropping the second reading with it would let the
    cap decide which check fails.

    The cap is shared, but each answer a message can arrive with holds a reserve
    of it that the others cannot spend, because the kinds do not arrive evenly: a
    broken export fails hundreds of loads while the app raises the one error saying
    why, and one budget spent in arrival order is spent on the loads. The reserve
    is as large as the verdict prints, so no kind can be starved of anything a
    reader would have been shown. A located message no failed load explains yet
    draws on a reserve of its own rather than the app's, since a flood whose
    network events trail its console messages reads as the app's own on the way in
    and would otherwise spend the share held for the one error worth reading.

    What a message costs on the way in is a slice, a substring search and a set
    lookup, all of which the interpreter does at C speed. That is the whole reason
    the bound holds under a flood: anything per-character here is paid ninety
    thousand times, and the check's budget is spent on it while the app is still
    trying to boot.
    """

    def __init__(self, ignore: tuple[str, ...] = ()) -> None:
        self._ignore = ignore
        self._kept: list[_Said] = []
        # How much of the shared cap each reserve has drawn. Keyed by the answer a
        # message ARRIVED with, which includes "pending" for a located message no
        # failed load explains yet, because that one may still join either of two
        # lists and a reserve it spends is a reserve the list it leaves cannot get
        # back. Migrating the count when the load fails would be exact and would
        # also let a page refill the app's reserve on demand, one failed URL at a
        # time, until nothing bounds what is kept.
        self._held = {"app": 0, "resource": 0, "ignored": 0, "pending": 0}
        self._counted = {"app": 0, "resource": 0, "ignored": 0}
        # Every request that actually failed, by URL: a load with no response at
        # all (a refused connection, a dead host) and a response that carried an
        # error status are both failed loads, and only one of them produces a
        # response.
        self._failed: set[bytes] = set()
        # Network failures are the evidence, even when a worker's failed fetch
        # produces no console message. Keep only the diagnostics we can print;
        # count every failed attempt without retaining its URL indefinitely.
        self._load_total = 0
        self._load_messages: list[_Said] = []
        # Whether the set above is all of them. Once it is not, a URL missing from
        # it no longer means the load did not fail, so the match it backs stops
        # being evidence and every classification that would have leant on it is
        # decided the other way.
        self._loads_complete = True
        # Messages counted rather than kept whose kind is not settled yet, by the
        # URL and the exemption that will settle it. A located message reads as
        # the app's own until a failed load explains it, and the two arrive in
        # whichever order the driver hands them over, so the kept messages are
        # read again at the verdict against every load known by then. Tallying
        # this one under the kind it arrived with would make its provisional
        # answer final, and the answer that gets frozen is the one that fails a
        # check. What is remembered is the URL, at 146 bytes; the text is what
        # was expensive, and the text is still dropped.
        self._unsettled: dict[tuple[bytes, bool], int] = {}

    def load_failed(self, url: str, status: int | None = None) -> None:
        self._load_total += 1
        if len(self._load_messages) < _MAX_REPORTED_ITEMS:
            prefix = "Failed to load resource: "
            if status is not None:
                prefix += f"HTTP {status}: "
            text = prefix + url[:_MAX_SCANNED_CHARS]
            self._load_messages.append(_Said(text, len(prefix) + len(url), from_page=False))
        key = _load_key(url)
        if len(self._failed) < _MAX_TRACKED_LOADS:
            self._failed.add(key)
        elif key not in self._failed:
            # One already remembered costs nothing to drop, and a page failing the
            # same load repeatedly is the ordinary shape of a retry loop.
            self._loads_complete = False

    def resources(self) -> _Bucket:
        """Failed network attempts, independent of console diagnostics."""
        return _Bucket(self._load_messages, self._load_total)

    def said(self, text: str, origin: str | None = None, *, from_page: bool = True) -> None:
        key = None if origin is None else _load_key(origin)
        exempt = from_page and any(pattern in text for pattern in self._ignore)
        # Classified against what is known now rather than at the verdict, because
        # the decision it drives has to be made now: a console error whose network
        # event has not arrived yet reads as the app's own, which is the stricter
        # reading. It decides only which reserve this message draws on. Nothing
        # here is a verdict, since a kept message is partitioned again at the
        # verdict and a counted one is settled there too, against every failed load
        # known by then.
        kind = _kind(key, exempt, self._failed, loads_complete=self._loads_complete)
        # What is still owed an answer, or None once this message has one for good.
        # A message the page located nowhere is the app's own or an exemption for
        # good, and one already matched to a failed load cannot become anything
        # else, since the set it matched in only grows. Everything else is one
        # network event away from a different answer.
        pending = None if key is None or kind == "resource" else (key, exempt)
        reserve = kind if pending is None else "pending"
        if len(self._kept) < _MAX_RETAINED_ITEMS or self._held[reserve] < _MAX_REPORTED_ITEMS:
            self._held[reserve] += 1
            self._kept.append(_Said(text[:_MAX_SCANNED_CHARS], len(text), key, from_page, exempt))
        elif pending is None:
            self._counted[kind] += 1
        elif pending in self._unsettled or len(self._unsettled) < _MAX_TRACKED_LOADS:
            self._unsettled[pending] = self._unsettled.get(pending, 0) + 1
        else:
            # The same bound as the failed loads, and the same way out of it: what
            # cannot be settled is read as the load it is shaped like rather than
            # promoted to the app's own error.
            self._counted["resource"] += 1

    def buckets(self) -> tuple[_Bucket, _Bucket, _Bucket]:
        """The app's own errors, the failed loads, and the exemptions."""
        app, resource, ignored = _partition(
            self._kept, self._failed, loads_complete=self._loads_complete
        )
        counted = dict(self._counted)
        for (key, exempt), times in self._unsettled.items():
            counted[_kind(key, exempt, self._failed, loads_complete=self._loads_complete)] += times
        return (
            _Bucket(app, len(app) + counted["app"]),
            _Bucket(resource, len(resource) + counted["resource"]),
            _Bucket(ignored, len(ignored) + counted["ignored"]),
        )


@dataclass
class _Scan:
    """What one sweep of every frame saw."""

    outputs: int
    output_errors: _Bucket
    # window.Shiny, a bound input or a bound output: any one of them is the runtime
    # having executed, and none of them can come from the static shell.
    shiny_detected: bool


@dataclass
class CheckResult:
    ok: bool
    url: str
    seconds: float
    outputs: int
    shiny_detected: bool
    errors: list[str]
    # The number that OCCURRED, which is not len(errors) once the list is clipped.
    errors_total: int
    output_errors: list[str]
    output_errors_total: int
    resource_errors: list[str]
    resource_errors_total: int
    ignored_errors: list[str]
    ignored_errors_total: int
    expects: list[ExpectResult]
    failure: str | None
    screenshot: str | None


def run_check(
    url: str,
    *,
    timeout: float = 180.0,
    expects: tuple[str, ...] = (),
    screenshot: str | None = None,
    fail_on_resource_error: bool = False,
    ignore_console: tuple[str, ...] = (),
    progress: Callable[[float], None] | None = None,
) -> CheckResult:
    """Load ``url``, wait for the app to render, and return the verdict.

    Passing means: the page loaded, no console or page error occurred, no Shiny
    output is in an error state, the Shiny runtime was detected on the page, and
    either every ``expects`` selector was visible (when given) or at least one
    Shiny output rendered non-empty content (the default). A resource the browser
    could not load is reported but does not fail the check unless
    ``fail_on_resource_error`` is set. A console or page error containing one of
    the ``ignore_console`` substrings is reported separately and does not fail the
    check either. ``screenshot`` is captured only on failure. ``progress`` is
    called with the elapsed seconds on every poll, for callers reporting a wait.

    An empty ``ignore_console`` pattern raises ``ValueError``. The CLI refuses one
    with a hint before it launches a browser; this keeps the same promise for
    anyone calling ``run_check`` directly, since the alternative is that importing
    this function is a way around the invariant it exists to hold.
    """
    if any(not pattern for pattern in ignore_console):
        # Every string contains the empty one, so accepting it would silently turn
        # a named exemption into "ignore every console error", and a broken app
        # would then be reported as healthy rather than as unchecked.
        raise ValueError(
            "ignore_console patterns must not be empty; the empty string is in every "
            "message and would exempt every console error"
        )

    start = time.monotonic()
    heard = _Heard(ignore_console)

    def result(
        *,
        ok: bool,
        outputs: int = 0,
        shiny_detected: bool = False,
        output_errors: _Bucket | None = None,
        expect_state: dict[str, bool] | None = None,
        failure: str | None = None,
        shot: str | None = None,
    ) -> CheckResult:
        state = expect_state or dict.fromkeys(expects, False)
        page_errors = output_errors or _Bucket([], 0)
        app_errors, _, ignored_errors = heard.buckets()
        resource_errors = heard.resources()
        return CheckResult(
            ok=ok,
            url=url,
            seconds=round(time.monotonic() - start, 1),
            outputs=outputs,
            shiny_detected=shiny_detected,
            errors=_bounded(app_errors),
            errors_total=app_errors.total,
            output_errors=_bounded(page_errors),
            output_errors_total=page_errors.total,
            resource_errors=_bounded(resource_errors),
            resource_errors_total=resource_errors.total,
            ignored_errors=_bounded(ignored_errors),
            ignored_errors_total=ignored_errors.total,
            expects=[ExpectResult(sel, done) for sel, done in state.items()],
            failure=failure,
            screenshot=shot,
        )

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except PlaywrightError as err:
            if "playwright install" in str(err) or "Executable doesn't exist" in str(err):
                raise BrowserMissingError(str(err)) from err
            raise

        interrupted = False
        try:
            page = browser.new_page()
            # Before the first navigation, so that every document this page loads
            # is entered with the scan's two methods already taken.
            page.add_init_script(_FRAME_SCAN_INIT)

            def on_console(msg: ConsoleMessage) -> None:
                if msg.type != "error":
                    return
                heard.said(msg.text, None if msg.args else msg.location.get("url"))

            def on_response(response: Response) -> None:
                if response.status >= 400:
                    heard.load_failed(response.url, response.status)

            def on_request_failed(request: Request) -> None:
                # An error status was already recorded on response. A failed
                # response body must not count the same request a second time.
                if request.failure == "net::ERR_ABORTED":
                    return  # deliberate cancellation is not a broken resource
                try:
                    response = request.response()
                except PlaywrightError:
                    # Teardown may close the protocol before the response can be
                    # read; the requestfailed event still establishes a failure.
                    response = None
                if response is None or response.status < 400:
                    heard.load_failed(request.url)

            page.on("pageerror", lambda err: heard.said(f"pageerror: {err}"))
            page.on("console", on_console)
            page.on("requestfailed", on_request_failed)
            page.on("response", on_response)

            _validate_selectors(page, expects)

            deadline = start + timeout
            try:
                response = page.goto(
                    url, wait_until="domcontentloaded", timeout=_remaining_millis(deadline)
                )
            except PlaywrightError as err:
                heard.said(str(err), from_page=False)
                return result(ok=False, failure="navigation", shot=_shoot(page, screenshot))
            if response is not None and response.status >= 400:
                heard.said(f"HTTP {response.status} for {url}", from_page=False)
                return result(ok=False, failure="http-status", shot=_shoot(page, screenshot))

            expect_state = dict.fromkeys(expects, False)
            scan = _Scan(outputs=0, output_errors=_Bucket([], 0), shiny_detected=False)

            while True:
                answering = _answering(page, deadline)
                scan = _scan_frames(answering)
                _scan_expects(answering, expect_state)
                failure = _fault(heard, scan, strict=fail_on_resource_error)

                if failure is None and _satisfied(scan, expect_state, expects):
                    # Let the page settle briefly: a failure right after the first
                    # render still fails the check instead of racing it. Console
                    # errors arrive as events, but an output that fails inside the
                    # window is only visible to another scan, so the page is
                    # re-scanned rather than just re-read.
                    page.wait_for_timeout(_SETTLE_MILLIS)
                    answering = _answering(page, deadline)
                    scan = _scan_frames(answering)
                    _scan_expects(answering, expect_state)
                    failure = _fault(heard, scan, strict=fail_on_resource_error)
                    if failure is None and not _satisfied(scan, expect_state, expects):
                        # It held, then stopped holding. That is not a pass, and it
                        # is not yet a failure either: the app may simply be
                        # mid-rerender, so the condition has to be met again -
                        # while there is budget left to meet it in. Waiting again
                        # without asking the clock is how a page that renders and
                        # unrenders on every scan outlasted its own timeout.
                        if time.monotonic() <= deadline:
                            continue
                        failure = "timeout"
                elif failure is None:
                    if time.monotonic() > deadline:
                        failure = "timeout"
                    else:
                        if progress is not None:
                            progress(time.monotonic() - start)
                        page.wait_for_timeout(_POLL_MILLIS)
                        continue

                ok = failure is None
                return result(
                    ok=ok,
                    outputs=scan.outputs,
                    shiny_detected=scan.shiny_detected,
                    output_errors=scan.output_errors,
                    expect_state=expect_state,
                    failure=failure,
                    shot=None if ok else _shoot(page, screenshot),
                )
        except KeyboardInterrupt:
            # Closing the browser is a request-and-reply over the connection the
            # interrupt just unwound, so the reply never comes and Ctrl-C hangs
            # instead of stopping the check. Leaving is safe: the driver exits
            # with this process and takes the browser with it.
            interrupted = True
            raise
        finally:
            if not interrupted:
                browser.close()


def _remaining_millis(deadline: float) -> int:
    """What is left of the caller's budget, as a playwright timeout.

    Never zero: playwright reads a zero timeout as "wait forever", so a budget
    that has just run out would become an unbounded wait, which is the opposite of
    what a timeout means. The CLI rejects a non-positive ``--timeout`` outright;
    this keeps the same promise for anyone calling ``run_check`` directly.
    """
    return max(1, int((deadline - time.monotonic()) * 1000))


def _kind(
    origin: bytes | None,
    exempt: bool,
    failed_loads: set[bytes],
    *,
    loads_complete: bool = True,
) -> str:
    """Which of the three lists a message belongs in: app, resource, or ignored.

    One function because it is asked twice: at the verdict for everything kept,
    and as a message arrives for everything past the retention cap. Two copies of
    this decision would let the counts disagree with the lists they stand beside.

    ``loads_complete`` is false once the failed-load set stopped accepting URLs.
    Absence from it then means nothing, so a message shaped like a failed load is
    read as one rather than promoted to the app's own error on the strength of a
    lookup that can no longer answer.
    """
    if origin is not None and (not loads_complete or origin in failed_loads):
        return "resource"
    if exempt:
        return "ignored"
    return "app"


def _partition(
    said: list[_Said], failed_loads: set[bytes], *, loads_complete: bool = True
) -> tuple[list[_Said], list[_Said], list[_Said]]:
    """Split what the page said into the app's own errors, failed loads, and exemptions.

    Matched by URL when a verdict is wanted rather than as each message arrives,
    so a network event landing between the console error and that verdict is
    still in evidence. The slack is not what makes the classification safe. A
    console error for a failed load is written by the renderer after it is told
    the load failed, and both events reach this process over one ordered
    connection, so the network event is seen first: measured at 250 of 250 failed
    resources, 404s (which arrive as a response) and refused connections (which
    arrive as `requestfailed`) alike. The slack covers the case where that ceases
    to hold, and covers it only as far as the next verdict, because a message
    still unmatched then is read as the app's own.

    Shape alone would not do instead of the URL match. A blocked inline script is
    the same shape, no console arguments and located at a URL, and no network
    event ever arrives to explain it, which is correct: the browser refused
    something in the page rather than failing to fetch it.

    An exemption is read only against what the page itself said, and only against
    what is left after that split. A pattern written for the app's own words
    cannot reach a failed load: those have their own switch, and the message they
    carry is the browser's wording rather than the app's. Nor can it reach the
    check's own account of a navigation failure or an error status, which is not
    a console error and is never ignored, since it is what ended the check. The
    match is a plain case-sensitive substring, so what is exempted is legible
    from the command line that exempted it.
    """
    lists: dict[str, list[_Said]] = {"app": [], "resource": [], "ignored": []}
    for item in said:
        kind = _kind(item.origin, item.exempt, failed_loads, loads_complete=loads_complete)
        lists[kind].append(item)
    return lists["app"], lists["resource"], lists["ignored"]


def _fault(heard: _Heard, scan: _Scan, *, strict: bool) -> str | None:
    """The failure the page is already showing, if any, in order of directness.

    An error the app raised says more about what is wrong than an output in an
    error state, which says more than a resource that would not load. A failed
    load only decides the verdict under ``strict``: a missing sourcemap, webfont
    or image is not the app being broken, and failing on one teaches a team to
    ignore the check.

    A message matching ``ignore`` decides nothing, which is the whole of what the
    flag does: it is still counted and still reported, and an output in an error
    state or a failed load under ``strict`` still fails the check around it.

    Read from the counts rather than from the lists, since a flooding page is
    counted without being kept and a verdict must not turn on which of its errors
    were the ones retained.
    """
    app_errors, _, _ = heard.buckets()
    if app_errors.total:
        return "error"
    if scan.output_errors.total:
        return "output-error"
    if strict and heard.resources().total:
        return "resource-error"
    return None


def _validate_selectors(page: Page, expects: tuple[str, ...]) -> None:
    """Reject a malformed --expect selector on the blank page, before anything loads.

    Inside the poll loop a bad selector cannot be told from a frame that detached
    mid-navigation: both arrive as a PlaywrightError, and matching on the message
    text misses every rejection Chromium words differently (an invalid XPath and
    an unsupported pseudo-class both come back as `SyntaxError: Failed to execute
    ...`). Such a selector then simply never matched, so the check waited out the
    full timeout and reported that the app had not rendered.

    Asking once, here, replaces the text match with a question that has no other
    answer. The page is still about:blank, so nothing can be transient and no
    frame can detach: an error is the selector. Being before navigation also
    means the caller's mistake is reported even when the site is unreachable,
    where the navigation failure would otherwise arrive first and bury it.
    """
    for selector in expects:
        try:
            page.locator(selector).first.is_visible()
        except PlaywrightError as err:
            raise SelectorError(f"invalid --expect selector: {selector}") from err


def _satisfied(scan: _Scan, expect_state: dict[str, bool], expects: tuple[str, ...]) -> bool:
    """Has the app met the pass condition?

    Every pass is gated on ``shiny_detected`` first, whatever the condition. An
    export serves its shell before pyodide boots, so a selector naming anything in
    that shell is satisfied by a page the runtime never reached: without this gate,
    ``--expect`` certified a fixture that raises at import. The default condition
    already implies it, since a rendered output is itself Shiny having run; making
    it explicit is what stops the two conditions from meaning different things.
    """
    if not scan.shiny_detected:
        return False
    if expects:
        return all(expect_state.values())
    return scan.outputs >= 1


def _answering(page: Page, deadline: float) -> list[Frame]:
    """The frames still answering, asked once for the whole sweep.

    ``evaluate`` is the one playwright call that takes no timeout, so a page
    running an infinite loop - or any long synchronous main-thread work, a heavy
    WASM step or a pathological layout - blocked the scan inside the browser,
    where the poll loop's deadline could not reach it. A ``--timeout 5`` check
    against such a page was measured still running at 45 seconds, with no verdict
    and not even the progress line, because the loop that prints it was never
    re-entered.

    Playwright enforces its timeouts in the driver rather than in the page, so a
    call that accepts one is bounded even when the page never answers. This asks
    the cheapest question there is through such a call. The predicate is constant,
    so a live frame resolves it on the first evaluation and is not slowed by being
    asked.

    Asked once per sweep, and not once per question the sweep goes on to ask.
    Gating each selector lookup on its own liveness check left the cost multiplied
    by however many selectors the caller had named: twenty of them turned an 8
    second budget into 46 seconds, so describing the page in more detail bought a
    weaker guarantee about how long checking that description would take.

    The sweep as a whole is bounded by what is left of the budget, which is what
    keeps a page from buying time by holding many frames rather than by wedging
    one. Frames are asked in order until that is spent; whatever is left unasked
    could not have been read in the time remaining anyway.

    This bounds the wait, which is what the budget promises; it does not make
    reading the page atomic. A frame that stops answering in the moment between
    this question and the scan that follows blocks that scan, because playwright
    offers no bounded way to read a value back. A page wedges once and stays
    wedged, so that window costs a check nothing it can notice.
    """
    ends = time.monotonic() + _sweep_millis(deadline) / 1000
    answering: list[Frame] = []
    for frame in page.frames:
        left = int((ends - time.monotonic()) * 1000)
        if left <= 0:
            break
        try:
            frame.wait_for_function("() => true", timeout=left)
        except PlaywrightError:
            continue  # wedged: nothing can be read from it, and reading is unbounded
        answering.append(frame)
    return answering


def _sweep_millis(deadline: float) -> int:
    """How long one liveness sweep may take, in the budget's terms."""
    return max(_RESPONSIVE_FLOOR_MILLIS, min(_RESPONSIVE_MILLIS, _remaining_millis(deadline)))


def _scan_frames(frames: list[Frame]) -> _Scan:
    """One sweep of every frame that is answering, summed.

    Each frame counts its own failed outputs and hands over only the head of
    them, so an app rendering thousands of errors is counted in full without its
    text crossing the browser connection.

    One budget is spent across the whole sweep rather than given to each frame,
    because the number of frames is the page's choice as much as the size of an
    error is: a ceiling every frame is handed separately is one the page
    multiplies, and 21 frames at that ceiling cost a single sweep 6.9 s and
    116 MB to keep the 500 items one frame already yields. A frame is asked for
    what the frames before it left, so a sweep that has its fill reads the rest
    of the page for its counts alone.
    """
    outputs = 0
    output_errors: list[_Said] = []
    total = 0
    shiny_detected = False
    for frame in frames:
        try:
            scan = frame.evaluate(_FRAME_SCAN, _MAX_RETAINED_ITEMS - len(output_errors))
        except PlaywrightError:
            continue  # the frame detached or is still navigating
        outputs += scan["rendered"]
        total += scan["errorTotal"]
        for error in scan["errors"]:
            # The budget and the slice are what a frame is ASKED for; these lines
            # are what it is held to. The script doing the cutting runs in the
            # page's context, where the page may replace anything it is built
            # from, so the object that comes back is composed by the thing being
            # judged and neither ceiling can be enforced by the asking alone.
            # Both are repeated here for the reason `_printable` repeats its own:
            # a bound that exists in one place only is one oversight away from
            # not existing. Stopping rather than skipping, so that a frame
            # answering with more than the sweep will keep is not read to the end
            # of what it chose to hand over.
            if len(output_errors) >= _MAX_RETAINED_ITEMS:
                break
            text, written = error["text"], error["length"]
            # A frame that reports no length is saying it handed the message over
            # whole, and the length of what arrived is then the true one and is
            # measured on this side. A frame that says so untruthfully is answered
            # by the cut below: whatever it kept back is described at the size that
            # reaches here, so its own claim cannot make an error look shorter than
            # the part being reported.
            if written is None:
                written = len(text)
                exact = True  # counted here, in the units the verdict is read in
            else:
                # Absent as well as false: a frame answering in a shape this does
                # not recognise has not said the size is a character count, and a
                # size reported as a bound is wrong by less than one reported as a
                # count.
                exact = bool(error.get("exact"))
            output_errors.append(_Said(text[:_MAX_SCANNED_CHARS], written, exact=exact))
        shiny_detected = shiny_detected or bool(scan["runtime"] or scan["bound"] or scan["inputs"])
    return _Scan(
        outputs=outputs,
        output_errors=_Bucket(output_errors, total),
        shiny_detected=shiny_detected,
    )


def _scan_expects(frames: list[Frame], expect_state: dict[str, bool]) -> None:
    """Record which selectors are visible in some frame, as of now.

    Deliberately not sticky. Naming several selectors is a caller describing a
    state the app reaches, and a state is one moment: sticky satisfaction let
    ``--expect "#loading" --expect "#chart"`` be satisfied by a page that showed a
    spinner and then failed, because each had been true at some point. The verdict
    reports the last scan for the same reason, so a selector listed as satisfied is
    one that was satisfied when the check ended.

    Every selector reaching here parses, having been checked before the page was
    loaded, so an error is a frame that went away and nothing else.

    Only frames known to be answering are asked, for the same reason the scan
    skips the rest: ``is_visible`` documents its own ``timeout`` argument as
    ignored, so against a page whose main thread never yields it waits
    playwright's 30 second default - per selector, per frame - which is a limit
    the caller's budget does not govern and cannot shorten.
    """
    for selector in expect_state:
        visible = False
        for frame in frames:
            try:
                if frame.locator(selector).first.is_visible():
                    visible = True
                    break
            except PlaywrightError:
                continue  # the frame detached or is still navigating
        expect_state[selector] = visible


def _shoot(page: Page, path: str | None) -> str | None:
    """Capture the failure screenshot when asked; never let it mask the verdict.

    Playwright writes the file itself, so a full disk or an unwritable directory
    raises OSError rather than a PlaywrightError. Either way the verdict is what
    the caller came for, and losing the screenshot must not cost them that.
    """
    if path is None:
        return None
    try:
        page.screenshot(path=path, full_page=True)
    except (PlaywrightError, OSError):
        return None
    return path
