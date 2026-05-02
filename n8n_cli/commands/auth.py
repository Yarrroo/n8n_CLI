"""`n8n-cli auth *` — API-key status + frontend session login/logout."""

from __future__ import annotations

import base64
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from n8n_cli.api.errors import ApiError, AuthError, MfaRequiredError, UserError
from n8n_cli.api.frontend import FrontendApi
from n8n_cli.api.public import PublicApi
from n8n_cli.api.transport import Transport
from n8n_cli.config import sessions, store
from n8n_cli.output.jsonout import emit

app = typer.Typer(help="Authenticate against an n8n instance.", no_args_is_help=True)


InstanceOpt = Annotated[
    str | None,
    typer.Option("--instance", help="Instance name (defaults to current)."),
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Log HTTP calls to stderr.")]


def _decode_jwt_exp(token: str) -> datetime | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    padding = "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(parts[1] + padding)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp")
    if not isinstance(exp, int | float):
        return None
    return datetime.fromtimestamp(exp, tz=UTC)


def _read_password(password_stdin: bool, email: str) -> str:
    """Resolve password from env → stdin → interactive prompt."""
    pw = os.environ.get("N8N_PASSWORD")
    if pw:
        return pw
    if password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
        if not pw:
            raise UserError("--password-stdin: no password read from stdin")
        return pw
    if not sys.stdin.isatty():
        raise UserError(
            "no password available",
            hint="set N8N_PASSWORD, use --password-stdin, or run interactively.",
        )
    return getpass.getpass(f"Password for {email}: ")


@app.command("login")
def login(
    instance_name: InstanceOpt = None,
    email: Annotated[
        str | None, typer.Option("--email", help="Override the instance's email.")
    ] = None,
    password_stdin: Annotated[
        bool, typer.Option("--password-stdin", help="Read password from stdin (for CI).")
    ] = False,
    mfa_code: Annotated[
        str | None,
        typer.Option(
            "--mfa-code",
            help="6-digit TOTP code (when account has MFA enabled). "
            "Falls back to $N8N_MFA_CODE; prompts interactively if neither is set.",
        ),
    ] = None,
    mfa_recovery_code: Annotated[
        str | None,
        typer.Option(
            "--mfa-recovery-code",
            help="MFA recovery code (one-time backup code). Falls back to $N8N_MFA_RECOVERY_CODE.",
        ),
    ] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Log in to the frontend API; persist the session cookie."""
    name, inst = store.resolve_active(instance_name)
    resolved_email = email or os.environ.get("N8N_EMAIL") or inst.email
    if not resolved_email:
        raise UserError(
            "no email configured for login",
            hint="pass --email, set N8N_EMAIL, or `n8n-cli instance patch <name> --email ...`.",
        )
    password = _read_password(password_stdin, resolved_email)

    if mfa_code and mfa_recovery_code:
        raise UserError("--mfa-code and --mfa-recovery-code are mutually exclusive")
    resolved_mfa_code = mfa_code or os.environ.get("N8N_MFA_CODE") or None
    resolved_mfa_recovery = mfa_recovery_code or os.environ.get("N8N_MFA_RECOVERY_CODE") or None

    with Transport(inst, instance_name=name, verbose=verbose) as t:
        fapi = FrontendApi(t)
        try:
            user = fapi.login(
                resolved_email,
                password,
                mfa_code=resolved_mfa_code,
                mfa_recovery_code=resolved_mfa_recovery,
            )
        except MfaRequiredError:
            # Account has MFA enabled and no code was supplied. If we can
            # prompt, do so; otherwise re-raise so CI sees a clean error.
            if not sys.stdin.isatty():
                raise UserError(
                    "MFA required",
                    hint="pass --mfa-code <TOTP> / --mfa-recovery-code <code>, "
                    "or set $N8N_MFA_CODE / $N8N_MFA_RECOVERY_CODE.",
                ) from None
            entered = getpass.getpass(f"MFA code for {resolved_email} (TOTP, 6 digits): ").strip()
            if not entered:
                raise UserError("MFA code required") from None
            user = fapi.login(resolved_email, password, mfa_code=entered)

    emit(
        {
            "instance": name,
            "logged_in": True,
            "user_id": user.get("id"),
            "email": user.get("email"),
            "role": user.get("role"),
        }
    )


@app.command("logout")
def logout(
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Log out (POST /rest/logout) and delete the cached session file."""
    name, inst = store.resolve_active(instance_name)
    with Transport(inst, instance_name=name, verbose=verbose) as t:
        FrontendApi(t).logout()
    emit({"instance": name, "logged_out": True})


@app.command("status")
def status(
    instance_name: InstanceOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Check authentication state (public API-key + frontend session)."""
    name, inst = store.resolve_active(instance_name)
    report: dict[str, Any] = {"instance": name, "url": str(inst.url)}

    # --- public (API key) ---
    public_block: dict[str, Any] = {"backend": "public", "authenticated": False}
    if inst.api_key is None:
        public_block["reason"] = "no API key configured"
    else:
        token = inst.api_key.get_secret_value()
        exp = _decode_jwt_exp(token)
        if exp is not None:
            public_block["expires_at"] = exp.isoformat()
            remaining_days = (exp - datetime.now(tz=UTC)).days
            public_block["expires_in_days"] = remaining_days
            if remaining_days <= 7:
                public_block["warning"] = f"API key expires in {remaining_days} day(s)"
        try:
            with Transport(inst, instance_name=name, verbose=verbose) as t:
                PublicApi(t).ping()
            public_block["authenticated"] = True
        except (AuthError, ApiError) as exc:
            public_block["error"] = exc.message
    report["public"] = public_block

    # --- frontend (session cookie) ---
    frontend_block: dict[str, Any] = {"backend": "frontend", "authenticated": False}
    sess = sessions.load(name)
    if sess is None:
        frontend_block["reason"] = "no session — run `n8n-cli auth login`"
    else:
        if sess.user_id:
            frontend_block["user_id"] = sess.user_id
        if sess.personal_project_id:
            frontend_block["personal_project_id"] = sess.personal_project_id
        try:
            with Transport(inst, instance_name=name, verbose=verbose) as t:
                user = FrontendApi(t).session_user()
            if user:
                frontend_block["authenticated"] = True
                frontend_block["email"] = user.get("email")
            else:
                frontend_block["reason"] = "session expired"
        except (AuthError, ApiError) as exc:
            frontend_block["error"] = exc.message
    report["frontend"] = frontend_block

    emit(report)
