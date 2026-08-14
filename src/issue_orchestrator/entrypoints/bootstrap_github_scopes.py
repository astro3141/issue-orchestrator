"""GitHub token scope policy checked by the composition root at startup.

``github.required_scopes`` / ``github.allowed_scopes`` are parsed in
:mod:`..infra.github_config`; this is where they are enforced, so the two
halves of that setting stay one hop apart instead of sitting in the wiring
body of :mod:`.bootstrap`.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..infra.config import Config

logger = logging.getLogger(__name__)


class TokenScopeReader(Protocol):
    """The narrow slice of the repository host this check needs."""

    def get_token_scopes(self) -> list[str]:
        ...


def check_github_token_scopes(config: Config, github: TokenScopeReader) -> None:
    """Fail fast when the configured token's scopes are wrong for this repo."""
    if getattr(github, "auth_kind", None) == "github_app":
        logger.info("Skipping OAuth scope check for GitHub App installation auth")
        return
    required = {scope.strip() for scope in (config.github_required_scopes or []) if scope.strip()}
    allowed = {scope.strip() for scope in (config.github_allowed_scopes or []) if scope.strip()}
    try:
        scopes = set(github.get_token_scopes())
    except Exception as exc:
        logger.warning("Failed to fetch GitHub token scopes: %s", exc)
        return

    if required and not required.issubset(scopes):
        missing = sorted(required - scopes)
        raise ValueError(f"GitHub token missing required scopes: {missing}")

    if allowed and not scopes.issubset(allowed):
        extra = sorted(scopes - allowed)
        raise ValueError(f"GitHub token has disallowed scopes: {extra}")

    if scopes:
        logger.info("GitHub token scopes: %s", ", ".join(sorted(scopes)))
    else:
        logger.info("GitHub token scopes unavailable (fine-grained token or missing header)")


__all__ = ["TokenScopeReader", "check_github_token_scopes"]
