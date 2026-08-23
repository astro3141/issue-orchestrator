"""What a session's launch record says about the launch's identity.

``session-identity.json`` is the durable per-run answer to "what was this
session launched as, and under whose configuration". It is written by five
launch paths (issue, validation retry, review, retrospective review, rework),
so the fields belong to one owner rather than to whichever launcher happened
to write them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.workspace_trust import launch_attribution

if TYPE_CHECKING:
    from ..domain.models import AgentConfig
    from ..infra.config import Config


def session_identity_launch_metadata(
    config: "Config",
    agent_config: "AgentConfig",
    *,
    extra_provider_args: dict[str, str] | None,
) -> dict[str, object]:
    """The launch-identity fields every session record carries.

    ``workspace_trust_*`` names which repository-root approval this launch
    carried and the document that granted it (#215). The launch argv records
    the grant that was *materialized*; this records the authority behind it, so
    the two together answer why the repository was trusted — and a launch that
    carried no approval says so explicitly rather than by omission.
    """
    return {
        "provider": str(agent_config.provider or ""),
        "model": str(agent_config.model or ""),
        "permission_mode": agent_config.effective_permission_mode,
        "timeout_minutes": int(agent_config.timeout_minutes),
        "extra_provider_args": dict(extra_provider_args or {}),
        "configuration_mode": config.configuration_mode,
        "config_name": config.config_name,
        "config_fingerprint": config.config_fingerprint,
        **launch_attribution(agent_config.workspace_trust),
    }
