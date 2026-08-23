"""Operator-approved repository trust for a provider launch (#215).

Provider CLIs gate whether a *repository's own files* may configure them —
project-local config, hooks and exec policies — behind an interactive
workspace-trust dialog. That gate sits upstream of every approval/sandbox
flag: it is settled before the layers those flags live in are assembled, so
``--ask-for-approval never``, ``--sandbox workspace-write`` and even
``--dangerously-bypass-approvals-and-sandbox`` do not suppress it. An
unattended launch in a linked worktree therefore parks on the dialog forever
(#204 measurement).

What this module owns is the *authority* half of the answer, as pure domain
values:

* :class:`ApprovedRepositoryTrust` — the absolute repository root a human
  approved, plus the identity of the document that carries the approval.
* :class:`LaunchWorkspace` — where one launch runs, and which approval (if
  any) it carries.
* :class:`RepositoryTrustGrant` — a verified decision. It cannot be
  constructed unless the resolved common repository root *equals* the
  approved root, so the type itself is the proof; a mismatch raises rather
  than yielding a weaker grant.

Three invariants are deliberate and load-bearing:

* **Default absent = deny.** ``LaunchWorkspace.approved_trust`` defaults to
  ``None`` and every consumer must fail closed on it. A launch that carries
  no approval gets no grant.
* **No travelling boolean.** The approval is an absolute path, never
  "the current repository is trusted". A boolean (or anything derived from
  ``Path.cwd()``, a repo-root walk, or the running orchestrator's own
  checkout) silently widens the grant to whatever checkout executes it.
* **Resolution is not this layer's job.** Turning a worktree into its
  repository root touches the filesystem, and rendering the grant into a CLI
  argument is provider vocabulary. Both live in the provider adapter
  (``execution.agent_runner_providers.codex_trust``); the domain stays pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ApprovedRepositoryTrust",
    "LaunchWorkspace",
    "RepositoryTrustGrant",
    "TrustAuthoritySource",
    "WorkspaceTrustError",
    "launch_attribution",
]

# Characters that must never appear in an approved root. A quote would have to
# survive a provider's ``key=value`` config-override quoting, and a newline
# would split the recorded argv line the grant is audited from; rather than
# guess how a CLI resolves either ambiguity, an approval carrying one is
# rejected as malformed authority state. ``=`` is deliberately NOT here: the
# root travels inside the *value* of one override pair, which is split on its
# first ``=`` only, so a path containing one is unambiguous.
_FORBIDDEN_ROOT_CHARACTERS = ('"', "\n", "\r")


class WorkspaceTrustError(RuntimeError):
    """Raised when a launch cannot be proven to run in an approved repository.

    Every failure mode is fail-closed and reaches the caller as this one
    error: absent approval state, malformed approval state, an unreadable or
    non-git working directory, and a resolved repository root that is not the
    approved one. The launch must not spawn.
    """


@dataclass(frozen=True, slots=True)
class TrustAuthoritySource:
    """Identity of the document that carries the approval.

    Recorded so launch evidence answers *why* a repository was trusted rather
    than only *that* it was: ``path`` names the authority document and
    ``fingerprint`` pins the bytes that were read from it.
    """

    path: Path
    fingerprint: str

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise WorkspaceTrustError(
                f"Workspace-trust authority source must be an absolute path "
                f"(got {self.path})"
            )
        if not self.fingerprint.strip():
            raise WorkspaceTrustError(
                "Workspace-trust authority source must carry a fingerprint of "
                f"the bytes it was read from ({self.path})"
            )


@dataclass(frozen=True, slots=True)
class ApprovedRepositoryTrust:
    """One human-approved repository root, and where that approval came from.

    ``repository_root`` is the *canonical repository root* — the owner of the
    git common directory, which is what a provider keys trust to — and never a
    worktree, a parent directory, or a relative/home-anchored path.
    """

    repository_root: Path
    source: TrustAuthoritySource

    def __post_init__(self) -> None:
        root = self.repository_root
        raw = str(root)
        if not root.is_absolute():
            raise WorkspaceTrustError(
                "Approved repository root must be absolute — a relative root "
                f"would resolve differently per checkout (got {raw})"
            )
        if root.parent == root:
            raise WorkspaceTrustError(
                "Approved repository root must not be the filesystem root"
            )
        if ".." in root.parts or "~" in raw:
            raise WorkspaceTrustError(
                "Approved repository root must be a normalized absolute path "
                f"with no '..' segment and no '~' (got {raw})"
            )
        for character in _FORBIDDEN_ROOT_CHARACTERS:
            if character in raw:
                raise WorkspaceTrustError(
                    "Approved repository root must not contain "
                    f"{character!r} (got {raw})"
                )


@dataclass(frozen=True, slots=True)
class LaunchWorkspace:
    """Where one launch runs, and the approval it carries.

    ``approved_trust`` defaults to ``None`` — *absent approval state* — which
    every consumer must treat as a denial rather than as "unspecified".
    """

    working_directory: Path
    approved_trust: ApprovedRepositoryTrust | None = None


@dataclass(frozen=True, slots=True)
class RepositoryTrustGrant:
    """A verified per-launch grant: this workspace *is* the approved repository.

    Construction is the verification. The constructor rejects any grant whose
    resolved common repository root differs from the approved root, so holding
    an instance is proof that the check passed — there is no path that yields
    a grant with a weaker meaning.
    """

    approved: ApprovedRepositoryTrust
    resolved_common_root: Path
    mechanism: str

    def __post_init__(self) -> None:
        if self.resolved_common_root != self.approved.repository_root:
            raise WorkspaceTrustError(
                "Refusing to grant workspace trust: the launch resolves to "
                f"repository root {self.resolved_common_root}, which is not "
                f"the approved root {self.approved.repository_root} "
                f"(authority: {self.approved.source.path})"
            )
        if not self.mechanism.strip():
            raise WorkspaceTrustError(
                "A workspace-trust grant must name the mechanism that "
                "materializes it"
            )

    @property
    def repository_root(self) -> Path:
        """The root the grant is scoped to (approved and resolved agree)."""
        return self.approved.repository_root

    def evidence(self) -> dict[str, str]:
        """Launch evidence: why this repository was trusted, in full.

        Carries the approved root, the root actually resolved from the
        launch's working directory, the identity and fingerprint of the
        authority document, the materialization mechanism, and the
        verification result.
        """
        return {
            "approved_repository_root": str(self.approved.repository_root),
            "resolved_common_root": str(self.resolved_common_root),
            "authority_source": str(self.approved.source.path),
            "authority_fingerprint": self.approved.source.fingerprint,
            "mechanism": self.mechanism,
            "verified": "true",
        }


def launch_attribution(approval: ApprovedRepositoryTrust | None) -> dict[str, str]:
    """How a session's launch record names the approval it carried.

    Written for every launch, approved or not, so a record distinguishes "this
    session carried no approval" from "this record predates the field". The
    keys are always present; empty values mean absent approval state.
    """
    return {
        "workspace_trust_approved_root": (
            str(approval.repository_root) if approval else ""
        ),
        "workspace_trust_authority": str(approval.source.path) if approval else "",
        "workspace_trust_authority_fingerprint": (
            approval.source.fingerprint if approval else ""
        ),
    }
