"""Typed harness failures, so the workflow (B10) routes on `kind` rather than on messages."""

from typing import Literal

HarnessErrorKind = Literal[
    "not_allowed",  # the tool gate refused (agent not allowlisted)
    "rejected",  # a pre-hook refused (e.g. unapproved or stale send)
    "unavailable",  # server down / timing out (after bounded retries, or circuit open)
    "failed",  # the tool answered with a structured error (e.g. stale_vehicle)
    "invalid_result",  # the result did not match its schema
    "budget",  # step or time budget exhausted
]


class HarnessError(Exception):
    kind: HarnessErrorKind = "failed"

    def __init__(self, message: str, *, tool: str, code: str | None = None) -> None:
        super().__init__(message)
        self.tool = tool
        self.code = code  # the server's ToolErrorBody.code, or the hook/budget reason


class ToolNotAllowed(HarnessError):
    kind = "not_allowed"


class HookRejected(HarnessError):
    kind = "rejected"


class ToolUnavailable(HarnessError):
    kind = "unavailable"


class ToolFailed(HarnessError):
    kind = "failed"


class ToolResultInvalid(HarnessError):
    kind = "invalid_result"


class BudgetExceeded(HarnessError):
    kind = "budget"
