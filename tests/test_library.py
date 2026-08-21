"""What the exported API owes a caller who never goes near the CLI.

`run_check` is in `__all__`, so the command line is one caller of this module
rather than its only entrance. An invariant enforced on the way through argparse
holds only for the callers who came that way, which is the narrower promise.
"""

from __future__ import annotations

import pytest

from shinylive_check import run_check


def test_an_empty_ignore_console_pattern_is_refused_by_the_library():
    """Every string contains the empty one, so accepting it exempts everything.

    Measured against a page that renders and then logs an app error: the same
    call returned `ok=False, failure="error"` normally and `ok=True` with
    `ignore_console=("",)`, the error moved to `ignored_errors` and the check
    reporting a broken app as healthy. A blanket exemption is the one thing this
    argument is deliberately not, so refusing it belongs to the function that
    honours it and not only to the parser in front of it.
    """
    with pytest.raises(ValueError) as raised:
        run_check("http://127.0.0.1:1/", ignore_console=("",))
    assert "ignore_console" in str(raised.value)


@pytest.mark.browser
def test_a_named_ignore_console_pattern_is_accepted_by_the_library():
    """The control: refusing the empty pattern must not refuse the real ones.

    A guard asking whether any pattern was given, rather than whether one of them
    is empty, satisfies the test above and rejects every use of the argument.
    Measured as a mutation: it fails here with the empty-pattern message quoted
    back at a pattern that is `known noise`.
    """
    result = run_check("http://127.0.0.1:1/", timeout=20, ignore_console=("known noise",))
    assert result.failure == "navigation"
