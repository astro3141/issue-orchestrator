"""Review-exchange mode helpers shared by control-layer policy."""

from __future__ import annotations

from typing import TypeGuard

FINAL_REVIEW_EXCHANGE_MODES = frozenset({"via-mcp", "via-local-loop"})


def is_final_review_exchange_mode(exchange_mode: str | None) -> TypeGuard[str]:
    """Return whether the mode completes review before publish.

    A :class:`~typing.TypeGuard`, not a plain ``bool``: the inline membership
    tests this replaces also narrowed ``str | None`` to ``str``, and every
    caller relies on it — a mode that completes review before publish is a
    configured mode, never the ``None`` that means "no exchange here". The
    narrowing is deliberately one-way. Failing the test proves nothing about
    the mode being absent; ``via-draft-pr`` is a perfectly good ``str``.
    """
    return exchange_mode in FINAL_REVIEW_EXCHANGE_MODES
