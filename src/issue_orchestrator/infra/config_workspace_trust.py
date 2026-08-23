"""The human-approved repository-root workspace trust, as config (#215).

The grant is operator authority rather than a tunable, so its parsing lives in
its own module: one place decides what a usable approval is, what identifies
the document that carries it, and what happens to anything malformed (nothing
launches). ``security.workspace_trust`` is deliberately absent from the
settings schema, so the web settings dialog cannot edit a human's approval —
which also means this loader, and :func:`security_section`'s round-trip, are
the only code that reads or writes the key.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.workspace_trust import (
    ApprovedRepositoryTrust,
    TrustAuthoritySource,
    WorkspaceTrustError,
)

__all__ = [
    "APPROVED_ROOT_KEY",
    "WORKSPACE_TRUST_KEY",
    "parse_workspace_trust",
    "reject_workspace_trust_overrides",
    "security_section",
]

if TYPE_CHECKING:
    from .config import Config

# The security-section keys that carry the grant. Named once so the loader, its
# error messages, and serialization cannot drift.
WORKSPACE_TRUST_KEY = "workspace_trust"
APPROVED_ROOT_KEY = "approved_repository_root"


def reject_workspace_trust_overrides(overrides: list[str]) -> None:
    """Refuse a CLI override that would rewrite the approval.

    A ``--set security.workspace_trust...=`` would change *which root* is
    trusted while the recorded authority still names the config document and
    its fingerprint — evidence that says one thing while the launch does
    another. The approval is a document decision, so it is edited in the
    document.
    """
    prefix = f"security.{WORKSPACE_TRUST_KEY}"
    for override in overrides:
        path = override.split("=", 1)[0].strip()
        if path == prefix or path.startswith(f"{prefix}."):
            raise ValueError(
                f"security.{WORKSPACE_TRUST_KEY} cannot be set by a CLI "
                "override: the approved repository root is a recorded, "
                "fingerprinted decision and must be edited in the config file"
            )


def parse_workspace_trust(
    trust_section: object,
    *,
    config_path: Path | None,
) -> ApprovedRepositoryTrust | None:
    """Parse the human-approved repository-root trust grant, or ``None``.

    ``None`` — the section absent — is *absent approval state*: launches that
    need workspace trust deny. Anything present but malformed raises instead of
    degrading to that default, so a typo cannot read as "approved" and cannot
    read as "silently unapproved" either; the engine refuses to start.

    The authority's identity travels with the grant: the config document that
    carries it, and a fingerprint of the exact bytes read from it, so launch
    evidence can name *which* document approved the root.

    The approved root is recorded **canonicalized** (see
    :func:`_canonical_approved_root`). Resolved-path equality is the assumption
    the whole mechanism rests on: the provider resolves the launch's common
    repository root, the grant compares the two with ``!=``, and the resolved
    spelling is what is handed to Codex as the ``projects`` key.
    """
    if trust_section is None:
        return None
    if not isinstance(trust_section, dict):
        raise ValueError(
            f"security.{WORKSPACE_TRUST_KEY} must be a mapping with "
            f"'{APPROVED_ROOT_KEY}' (got {type(trust_section).__name__})"
        )
    unknown = sorted(set(trust_section) - {APPROVED_ROOT_KEY})
    if unknown:
        raise ValueError(
            f"Unknown security.{WORKSPACE_TRUST_KEY} field(s): "
            f"{', '.join(unknown)}. Supported: {APPROVED_ROOT_KEY}"
        )
    raw_root = trust_section.get(APPROVED_ROOT_KEY)
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise ValueError(
            f"security.{WORKSPACE_TRUST_KEY}.{APPROVED_ROOT_KEY} must be a "
            "non-empty absolute path to the repository root a human approved"
        )
    if config_path is None:
        raise ValueError(
            f"security.{WORKSPACE_TRUST_KEY} requires a config file on disk: "
            "the grant records which document approved the root, and an "
            "in-memory config cannot be identified"
        )
    try:
        # Validate the document's spelling exactly as written, *then*
        # canonicalize. The order is load-bearing in both directions.
        #
        # Validate first: ``Path.resolve`` would launder a relative root, a
        # ``..`` segment, or a home-anchored path into an innocent-looking
        # absolute one — a relative root would silently mean "wherever the
        # engine happened to start". ``ApprovedRepositoryTrust`` rejects all
        # three, so it sees the operator's own text.
        #
        # Canonicalize second: the provider resolves the launch's common
        # repository root and the grant compares the two with ``!=``. An
        # approval written through a symlink (macOS ``/tmp`` →
        # ``/private/tmp``, a symlinked home or checkout parent) would
        # otherwise be a *correct* approval that denies every launch. It
        # denies safely, but a human who approved the right directory deserves
        # it to work, so both sides are brought to one spelling here — once,
        # at the only place an approval enters the system. ``replace`` re-runs
        # the domain's validation, so the canonical form is checked too.
        declared = ApprovedRepositoryTrust(
            repository_root=Path(raw_root.strip()),
            source=TrustAuthoritySource(
                path=config_path,
                fingerprint=_config_document_fingerprint(config_path),
            ),
        )
        return replace(
            declared, repository_root=declared.repository_root.resolve()
        )
    except WorkspaceTrustError as exc:
        raise ValueError(
            f"security.{WORKSPACE_TRUST_KEY}.{APPROVED_ROOT_KEY} is not a "
            f"usable approval: {exc}"
        ) from exc


def _config_document_fingerprint(config_path: Path) -> str:
    """SHA-256 of the authority document's bytes, as read from disk."""
    try:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError(
            f"Cannot fingerprint the workspace-trust authority document "
            f"{config_path}: {exc}"
        ) from exc


def security_section(config: "Config") -> dict[str, object]:
    """Serialize the security section, grant included, for a config rewrite.

    The grant must survive a save: rewriting the file without it would revoke a
    human's approval silently, and the next Codex launch would fail closed with
    nothing to explain why.
    """
    section: dict[str, object] = {}
    if not config.enforce_hooks:
        section["enforce_hooks"] = False
    if config.dangerous.allow_unsupported_agents:
        section["dangerous"] = {"allow_unsupported_agents": True}
    if config.workspace_trust is not None:
        section[WORKSPACE_TRUST_KEY] = {
            APPROVED_ROOT_KEY: str(config.workspace_trust.repository_root),
        }
    return section
