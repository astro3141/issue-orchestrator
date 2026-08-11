"""Domain vocabulary for named validation profiles (#7059).

Only the *name* of the contract lives here. A profile name appears in durable
run state — run manifests, validation records, validation retry state — which
domain and ports types describe, so the name is domain vocabulary.

Parsing ``validation.profiles`` YAML, resolving role bindings and holding the
resolved commands stay in :mod:`issue_orchestrator.infra.validation_profiles`;
those depend on config models and are infrastructure.
"""

from __future__ import annotations

DEFAULT_VALIDATION_PROFILE = "default"
"""The profile named by ``validation.quick`` / ``validation.publish``.

A repository that never mentions profiles runs entirely under this name, so
records and manifests written before profiles existed read back as ``default``
rather than as "unknown".
"""

__all__ = ["DEFAULT_VALIDATION_PROFILE"]
