"""Is this deployment able to launch its trust-requiring agents? (#215)

An agent whose provider gates project configuration behind a workspace-trust
decision cannot launch at all without a recorded approval. Without this, that
fact is only discovered *at launch*: the launcher acquires the claim, adds the
in-progress label, provisions the worktree, and only then does the provider
fail closed — so the deployment loses the ability to launch that agent once
per attempt, and the operator's only signal is a stack trace.

The statement is knowable before anything launches, from config plus the
provider's own answer about its configured launch shape. This module is its
one owner; :mod:`..infra.doctor.checks.workspace` is the surface that reports
it, where the other "this machine cannot launch that agent" findings are
reported.

Why doctor and not config validation: ``security.workspace_trust.approved_repository_root``
is a **host-absolute** path, so it cannot live in a config document that is
committed and shared across machines (this repo's own ``main.yaml`` and
``z-codespaces.yaml`` are exactly that). Treating its absence as a malformed
document would make those configs unloadable everywhere rather than
unlaunchable here. A doctor error still blocks the launcher
(``LaunchStatus.DOCTOR_ERROR``), so the operator learns before a claim is
spent — which is the whole point.

Deliberately **not** self-healing: nothing here writes an approved root. Which
root a human approved is the authority this mechanism exists to record, and a
component that invented one would be exactly the self-approval the design
forbids. Surfacing the question is the fix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config_workspace_trust import APPROVED_ROOT_KEY, WORKSPACE_TRUST_KEY

if TYPE_CHECKING:
    from ..domain.models import AgentConfig
    from .config import Config

__all__ = [
    "MISSING_APPROVAL_REMEDY",
    "agents_needing_workspace_trust",
    "unlaunchable_agents_without_workspace_trust",
]

MISSING_APPROVAL_REMEDY = (
    f"add security.{WORKSPACE_TRUST_KEY}.{APPROVED_ROOT_KEY}: "
    "<absolute path to the repository root a human approved> to this repo's "
    "config"
)


def agents_needing_workspace_trust(config: "Config") -> list[str]:
    """Labels of the agents whose configured launch shape needs an approval.

    Answers per agent, independent of whether an approval exists, so callers
    can report "these agents depend on the recorded root" as readily as
    "these agents cannot launch".
    """
    return [
        label
        for label, agent in config.agents.items()
        if _requires_workspace_trust(agent)
    ]


def unlaunchable_agents_without_workspace_trust(config: "Config") -> list[str]:
    """Labels of the agents that cannot launch at all, as configured.

    Empty when an approval is recorded: one approval covers every agent, and
    whether it is the *right* root is a launch-time question the provider
    settles against the worktree it actually resolves.
    """
    if config.workspace_trust is not None:
        return []
    return agents_needing_workspace_trust(config)


def _requires_workspace_trust(agent: "AgentConfig") -> bool:
    """Ask the provider, from the args the launch will build its command from.

    Going through the provider is what keeps this from drifting away from what
    ``build_command`` actually does. An unset or unknown provider is not this
    module's finding to report — config validation owns it.

    When the args cannot be interpreted (an ``execution_mode`` the provider
    does not recognise, a key that is not even passable), the question is put
    to the provider again with no args at all. That keeps "I could not tell"
    a denial for a provider whose *default* launch needs trust, without
    inventing a requirement for one that never needs it whatever its args say.
    """
    from issue_orchestrator.agent_runner import get_provider, is_valid_provider

    name = agent.provider
    if name is None or not is_valid_provider(name):
        return False
    provider = get_provider(name)
    try:
        return bool(provider.requires_workspace_trust(**agent.provider_args))
    except (TypeError, ValueError):
        return bool(provider.requires_workspace_trust())
