"""Exit codes and exception hierarchy.

Exit code contract (stable across releases):
    0 = success
    1 = unimplemented (phase stub hit)
    2 = user error (bad flag, missing arg, validation)
    3 = API error (4xx/5xx from n8n)
    4 = auth error (missing creds, expired token, no session)
    5 = capability/license gated (feature not available on this instance)
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    UNIMPLEMENTED = 1
    USER_ERROR = 2
    API_ERROR = 3
    AUTH_ERROR = 4
    CAPABILITY_GATED = 5


class CliError(Exception):
    """Base for all CLI-raised errors that should map to a specific exit code."""

    exit_code: ExitCode = ExitCode.USER_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class UserError(CliError):
    exit_code = ExitCode.USER_ERROR


class ApiError(CliError):
    exit_code = ExitCode.API_ERROR

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        backend: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message, hint=hint)
        self.status_code = status_code
        self.backend = backend


class AuthError(CliError):
    exit_code = ExitCode.AUTH_ERROR


class MfaRequiredError(AuthError):
    """Raised when n8n returns 401 + code 998 — account has MFA enabled but
    no `mfaCode` / `mfaRecoveryCode` was supplied.
    """


class CapabilityError(CliError):
    """Feature not available on this instance (license gate, missing endpoint)."""

    exit_code = ExitCode.CAPABILITY_GATED


class UnimplementedError(CliError):
    """Raised by phase-N stubs before the command is implemented."""

    exit_code = ExitCode.UNIMPLEMENTED
