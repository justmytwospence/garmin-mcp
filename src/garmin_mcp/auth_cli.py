"""garmin-mcp-auth: log in to Garmin Connect interactively (email, password, MFA
code) and save the tokens where the server reads them (GARMINTOKENS, default
~/.garminconnect; /data/tokens in the image). The server itself never logs in
interactively, so this is the one place a person types a code.

    garmin-mcp-auth            # prompts; keeps valid tokens unless --force-reauth
    garmin-mcp-auth --check    # only verify the stored tokens

In Docker: docker compose run --rm garmin-mcp garmin-mcp-auth [--check]
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from .garmin.auth import (
    AUTH_COMMAND,
    TOKEN_FILE,
    LoginProblem,
    classify_login_error,
    domain,
    has_tokens,
    is_cn,
    login_from_tokens,
    region_mismatch,
    token_dir,
    write_marker,
)
from .garmin.client import RESTART_COMMAND


def say(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def explain(problem: LoginProblem) -> None:
    say(f"Problem ({problem.kind}): {problem.message}")
    say(f"Fix: {problem.fix}")


def check(tokenstore: str, cn: bool, *, quiet: bool = False) -> LoginProblem | None:
    """Verify the stored tokens against Garmin. Returns the problem, or None when they work."""
    if not has_tokens(tokenstore):
        return LoginProblem("no_tokens", f"No tokens in {tokenstore} ({TOKEN_FILE} is missing).", f"Run `{AUTH_COMMAND}` to log in.")
    mismatch = region_mismatch(tokenstore, cn)
    if mismatch is not None:
        return mismatch
    try:
        t = login_from_tokens(tokenstore, cn)
    except Exception as err:  # noqa: BLE001
        return classify_login_error(err, tokenstore=tokenstore, cn=cn)
    if not quiet:
        say(f"Stored tokens in {tokenstore} work on {domain(cn)} (display name {t.display_name}).")
    return None


def login(tokenstore: str, cn: bool, email: str | None, password: str | None) -> int:
    from garminconnect import Garmin

    interactive = sys.stdin.isatty()
    if not interactive and not (email and password):
        say("No terminal attached and no GARMIN_EMAIL/GARMIN_PASSWORD: nothing to prompt on.")
        say(f"Run it attached to a terminal: `{AUTH_COMMAND}` (docker compose run allocates one), or `docker compose exec -it garmin-mcp garmin-mcp-auth`.")
        return 2
    say(f"Logging in to Garmin Connect ({domain(cn)}); tokens will be saved to {tokenstore}/{TOKEN_FILE}.")
    if not cn:
        say("Account on Garmin Connect China (garmin.cn)? Stop and rerun with GARMIN_IS_CN=true.")
    try:
        email = email or input("Garmin Connect email: ").strip()
        password = password or getpass.getpass("Password: ")
    except (EOFError, KeyboardInterrupt):
        say("\nCancelled.")
        return 2
    if not email or not password:
        say("Email and password are required.")
        return 1

    prompted = {"mfa": False}

    def prompt_mfa() -> str:
        prompted["mfa"] = True
        say("Garmin asks for a multi-factor code: check the email (or authenticator app) tied to the account. Codes expire after a few minutes.")
        try:
            return input("MFA code: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    Path(tokenstore).mkdir(parents=True, exist_ok=True)
    api = Garmin(email=email, password=password, is_cn=cn, prompt_mfa=prompt_mfa)
    try:
        api.login(tokenstore)
    except Exception as err:  # noqa: BLE001
        explain(classify_login_error(err, tokenstore=tokenstore, cn=cn, mfa_prompted=prompted["mfa"]))
        return 1
    try:
        api.client.dump(tokenstore)
    except Exception as err:  # noqa: BLE001
        say(f"Logged in, but saving tokens to {tokenstore} failed: {err}")
        say("Check that the directory is writable by this user (in Docker it is the /data volume, owned by uid 1000).")
        return 1
    try:
        os.chmod(os.path.join(tokenstore, TOKEN_FILE), 0o600)
    except OSError:
        pass
    write_marker(tokenstore, cn, getattr(api, "display_name", None))
    say(f"Logged in as {api.display_name} on {domain(cn)}; tokens saved to {tokenstore}/{TOKEN_FILE} (owner-only).")
    say(f"The library refreshes them in place for months. Now start or restart the server: `{RESTART_COMMAND}`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="garmin-mcp-auth", description="Log in to Garmin Connect and store tokens for garmin-mcp", epilog=f"In Docker: {AUTH_COMMAND} [--check]")
    parser.add_argument("--tokens", default=None, help="token directory (default GARMINTOKENS or ~/.garminconnect)")
    parser.add_argument("--check", action="store_true", help="only verify the stored tokens (exit 0 when they work)")
    parser.add_argument("--force-reauth", action="store_true", help="log in again even if the stored tokens work")
    parser.add_argument("--email", default=None, help="email (else GARMIN_EMAIL, else prompt)")
    args = parser.parse_args(argv)
    tokenstore = os.path.expanduser(args.tokens) if args.tokens else token_dir()
    try:
        cn = is_cn()
    except ValueError as err:
        say(f"ERROR: {err}")
        return 2
    if args.check:
        problem = check(tokenstore, cn)
        if problem is None:
            return 0
        explain(problem)
        return 1
    if not args.force_reauth and has_tokens(tokenstore):
        problem = check(tokenstore, cn)
        if problem is None:
            say("Use --force-reauth to log in again.")
            return 0
        if problem.kind in ("rate_limited", "unreachable"):
            # Not a token problem: a credential login now would not help and may lock the account.
            explain(problem)
            return 1
        say(f"Stored tokens cannot be used ({problem.kind}): {problem.message}")
        say("Logging in again.")
    return login(tokenstore, cn, args.email or os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD"))


if __name__ == "__main__":
    sys.exit(main())
