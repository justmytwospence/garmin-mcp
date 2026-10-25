"""Reading settings from the environment, the same way everywhere: one spelling of
true and false, one error message for a value that cannot be parsed."""

from __future__ import annotations

import os
from collections.abc import Mapping

TRUE = frozenset({"1", "true", "yes", "on"})
FALSE = frozenset({"0", "false", "no", "off"})


def env_bool(name: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    """An on/off setting. Unset or blank means `default`; anything other than
    1/0, true/false, yes/no, on/off (any case) is a ValueError."""
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in TRUE:
        return True
    if raw in FALSE:
        return False
    raise ValueError(f"{name} must be one of on/off, true/false, yes/no or 1/0, got {raw!r}")


def env_int(name: str, default: int, env: Mapping[str, str] | None = None, *, minimum: int | None = None) -> int:
    """An integer setting. Unset or blank means `default`."""
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as err:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from err
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}, got {value}")
    return value
