"""Smoke-check an exported Shinylive site in a real browser."""

from importlib.metadata import version

from .check import BrowserMissingError, CheckResult, ExpectResult, SelectorError, run_check

__version__ = version("shinylive-check")

__all__ = (
    "BrowserMissingError",
    "CheckResult",
    "ExpectResult",
    "SelectorError",
    "__version__",
    "run_check",
)
