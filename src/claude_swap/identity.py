"""Human-facing account identity, honoring ``ui.showEmail``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from claude_swap.settings import load_ui_settings

_Style = Callable[[str], str]


def show_email_enabled(backup_root: Path) -> bool:
    return load_ui_settings(backup_root).show_email


def account_parts(
    alias: str | None,
    email: str,
    org: str | None,
    *,
    show_email: bool,
) -> tuple[str | None, str | None, str | None]:
    """(alias, email, org) to render, each ``None`` when omitted. With
    ``show_email`` off an aliased account keeps only its alias; an account
    without an alias keeps ``email`` and ``org``, since nothing else would
    identify the row."""
    if alias and not show_email:
        return alias, None, None
    return alias or None, email, org or None


def masked_email(alias: str | None, email: str, *, show_email: bool) -> str:
    """The text to print where a message names an account by e-mail: the
    e-mail, or the alias when ``show_email`` is off and one exists."""
    name, mail, _ = account_parts(alias, email, None, show_email=show_email)
    return email if mail else name or email


def account_display(
    alias: str | None,
    email: str,
    org: str | None,
    *,
    show_email: bool,
    alias_style: _Style | None = None,
    org_style: _Style | None = None,
) -> str:
    """``alias (email) [org]`` with ``show_email`` on; just ``alias`` with it
    off (see :func:`account_parts`)."""
    name, mail, tag = account_parts(alias, email, org, show_email=show_email)
    pieces: list[str] = []
    if name:
        pieces.append(alias_style(name) if alias_style else name)
        if mail:
            pieces.append(f"({mail})")
    elif mail:
        pieces.append(mail)
    if tag:
        bracket = f"[{tag}]"
        pieces.append(org_style(bracket) if org_style else bracket)
    return " ".join(pieces)
