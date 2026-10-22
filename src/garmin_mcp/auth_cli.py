"""garmin-mcp-auth: log in to Garmin Connect interactively (email, password, MFA
code) and save the tokens where the server reads them (GARMINTOKENS, default
~/.garminconnect; /data/tokens in the image). The server itself never logs in
interactively, so this is the one place a person types a code.

    garmin-mcp-auth            # prompts; keeps valid tokens unless --force-reauth
    garmin-mcp-auth --check    # only verify the stored tokens
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .garmin.auth import is_cn, login_from_tokens, token_dir


def check(tokenstore: str, cn: bool) -> bool:
    try:
        t = login_from_tokens(tokenstore, cn)
    except Exception as err:  # noqa: BLE001
        print(f"Stored tokens in {tokenstore} do not work: {str(err)[:300]}", file=sys.stderr)
        return False
    print(f"Stored tokens in {tokenstore} work (display name {t.display_name}).")
    return True


def login(tokenstore: str, cn: bool, email: str | None, password: str | None) -> int:
    from garminconnect import Garmin

    email = email or input("Garmin Connect email: ").strip()
    password = password or getpass.getpass("Password: ")
    if not email or not password:
        print("Email and password are required.", file=sys.stderr)
        return 1

    def prompt_mfa() -> str:
        return input("MFA code (from your email or authenticator): ").strip()

    api = Garmin(email=email, password=password, is_cn=cn, prompt_mfa=prompt_mfa)
    try:
        api.login(tokenstore)
    except Exception as err:  # noqa: BLE001
        print(f"Login failed: {str(err)[:400]}", file=sys.stderr)
        return 1
    try:
        api.client.dump(tokenstore)
    except Exception as err:  # noqa: BLE001
        print(f"Logged in, but saving tokens to {tokenstore} failed: {err}", file=sys.stderr)
        return 1
    print(f"Logged in as {api.display_name}; tokens saved to {tokenstore}/garmin_tokens.json (owner-only). Restart garmin-mcp to pick them up.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="garmin-mcp-auth", description="Log in to Garmin Connect and store tokens for garmin-mcp")
    parser.add_argument("--tokens", default=None, help="token directory (default GARMINTOKENS or ~/.garminconnect)")
    parser.add_argument("--check", action="store_true", help="only verify the stored tokens")
    parser.add_argument("--force-reauth", action="store_true", help="log in again even if the stored tokens work")
    parser.add_argument("--email", default=None, help="email (else GARMIN_EMAIL, else prompt)")
    args = parser.parse_args(argv)
    tokenstore = os.path.expanduser(args.tokens) if args.tokens else token_dir()
    cn = is_cn()
    if args.check:
        return 0 if check(tokenstore, cn) else 1
    if not args.force_reauth and os.path.exists(os.path.join(tokenstore, "garmin_tokens.json")) and check(tokenstore, cn):
        print("Use --force-reauth to log in again.")
        return 0
    return login(tokenstore, cn, args.email or os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD"))


if __name__ == "__main__":
    sys.exit(main())
