"""The single process-local owner of the HTTP bearer credentials.

Every HTTP surface this process serves — the Control API app and the
Web Dashboard app that mounts it at ``""`` — is gated by the same two
secrets:

- the **admin bearer**, which authorizes every non-public route;
- the **agent-callback bearer**, a scoped token accepted only on the
  allowlist in :func:`._auth_middleware.is_agent_callback_route`.

Before #269 the admin bearer lived in two independently mutable module
globals: ``control_api._admin_token`` and ``web._dashboard_admin_token``.
Startup wrote the same value into both, so they looked equivalent — and
then diverged inside a live runtime. #268 measured one unrotated
``~/.issue-orchestrator/api-token`` accepted by ``control_api``
(``GET /api/status`` → 200) and rejected by the dashboard surface in the
same process (401). #267 showed the cost: the supported repository-engine
stop delegates its graceful phase to the dashboard ``/api/shutdown``, so
a normal stop could not authenticate and fell back to signals, leaving a
half-dead engine behind.

Two mutable copies of one credential cannot be kept in step by writing
to both more carefully — the second copy is the defect. So there is one
owner, and both surfaces read it live. That is the same rule
``_auth_middleware`` already applies to the agent-callback route
allowlist (one owner, one answer), now applied to the credential itself.

This module owns the *values*. ``_auth_middleware`` owns the *rules* it
evaluates them against. Nothing here mints, persists, or rotates a
token: resolution stays with ``infra.api_token`` and the startup
entrypoints that call it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BearerTokens:
    """An immutable snapshot of the process's bearer credentials.

    A gate evaluates one request against one snapshot, so it cannot
    observe a half-applied reconfiguration (admin swapped, callback
    not yet).
    """

    admin: str | None
    agent_callback: str | None

    @property
    def gate_enabled(self) -> bool:
        """Whether any auth is enforced at all.

        False only when *both* tokens are unset — the ``TestClient``
        default and the explicit ``--dev-no-auth`` operator flag.
        ``evaluate_request`` applies the same rule, so surfaces asking
        "is auth on?" for their login-form decision get the same answer
        the middleware acts on.
        """
        return not (self.admin is None and self.agent_callback is None)


class ProcessBearerTokens:
    """Process-local owner of the admin + agent-callback bearers.

    Configured once per process by whichever entrypoint serves HTTP
    (``ControlAPIServer.start``, ``control_center.main``,
    ``EngineStartup.configure_auth``) and read live by every gate. There
    is deliberately no per-surface copy to keep in sync: callers hold a
    reference to this owner, never to the token values.
    """

    def __init__(self) -> None:
        self._tokens = BearerTokens(admin=None, agent_callback=None)

    def configure(
        self, admin: str | None, *, agent_callback: str | None
    ) -> None:
        """Replace both bearers.

        Both are always written together — passing ``None`` for either
        clears it. There is no partial update, so a reconfiguration
        cannot leave a stale value authoritative on one surface.
        """
        self._tokens = BearerTokens(admin=admin, agent_callback=agent_callback)

    def current(self) -> BearerTokens:
        """Return the snapshot a single request should be judged against."""
        return self._tokens

    @property
    def admin(self) -> str | None:
        """The admin bearer, or ``None`` when admin auth is off."""
        return self._tokens.admin

    @property
    def agent_callback(self) -> str | None:
        """The scoped agent-callback bearer, or ``None`` when unset."""
        return self._tokens.agent_callback

    @property
    def gate_enabled(self) -> bool:
        """Whether either surface enforces anything — see :class:`BearerTokens`."""
        return self._tokens.gate_enabled


PROCESS_BEARER_TOKENS = ProcessBearerTokens()
"""The one owner. Import this, not a copy of what it holds."""
