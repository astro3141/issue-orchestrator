"""Composition of the bounded same-SHA revalidation route (#139).

Extracted from ``bootstrap`` so the composition root stays navigable — the same
split as ``bootstrap_completion`` and ``bootstrap_tech_lead``. Owns nothing but
the wiring: every collaborator it assembles is built by the function that
already owns building it, and ``bootstrap`` is its only caller. A factory
nothing at the root calls would be unreachable production code however
carefully it were assembled, so the route is held on ``OrchestratorDeps``
beside the other owners an execution-plane consumer reaches for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..infra.config import Config
from .bootstrap_completion import _validation_attempt_key_factory

if TYPE_CHECKING:
    from ..control.publication_revalidation import PublicationRevalidation
    from ..ports.attempt_store import AttemptStore
    from ..ports.command_runner import CommandRunner
    from ..ports.session_output import SessionOutput
    from ..ports.working_copy import WorkingCopy


def build_publication_revalidation(
    config: Config,
    *,
    attempt_store: "AttemptStore",
    session_output: "SessionOutput",
    command_runner: "CommandRunner",
    working_copy: "WorkingCopy",
) -> "PublicationRevalidation":
    """The one way to assemble the same-SHA revalidation route (#139).

    Assembled here rather than beside the completion pipeline because it is not
    part of it: the route is entered with a durable candidate identity alone,
    after the session that produced the candidate — and its worktree — is gone.

    The publication gate it composes is built by ``build_publication_gate``,
    the same function every other composition of that gate calls, so the
    contract a revalidation runs is the contract publication runs. Nothing here
    substitutes, relaxes or re-times any part of it.

    Checkouts are detached worktrees of the exact recorded commit, created
    beside the primary checkout so a revalidation never touches — or is
    confused with — an issue's own worktree.
    """
    from ..control.publication_gate import build_publication_gate
    from ..control.publication_revalidation import PublicationRevalidation
    from ..execution.git_candidate_checkouts import build_candidate_checkouts

    repo_root = config.repo_root
    return PublicationRevalidation(
        attempts=attempt_store,
        # A provider, not a registry: profiles are rebuilt from the current
        # config on every access, so a reloaded config must not leave the route
        # judging candidates against the contract it was constructed with.
        profiles=config.validation_profiles,
        checkouts=build_candidate_checkouts(
            repo_root=repo_root, command_runner=command_runner
        ),
        session_output=session_output,
        publication_gate=build_publication_gate(
            session_output=session_output,
            profiles=config.validation_profiles(),
            command_runner=command_runner,
            working_copy=working_copy,
            attempt_store=attempt_store,
            attempt_keys=_validation_attempt_key_factory(config),
            repo_root=repo_root,
        ),
    )
