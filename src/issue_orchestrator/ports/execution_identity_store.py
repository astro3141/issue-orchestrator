"""Port for durable candidate execution-identity evidence.

Foundation admission (``docs/foundation/VALIDATED_WORK_DISPOSITION.md`` §4)
reads this evidence *after* the sessions that produced it are gone, so the
contract the port promises is durability, not convenience: what
:meth:`CandidateExecutionIdentityStore.record` accepts must still be readable
after an orchestrator restart and after the issue worktree has been removed.

The key is :class:`~..domain.attempt.AttemptKey` — ``(issue, commit)`` — rather
than a new identity type, because that is exactly the contract's ``(issue, A)``
and IO already has a durable owner for it. Keying evidence by the candidate is
what makes "bound to the exact A" a storage property instead of a field two
writers could disagree about.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.attempt import AttemptKey
from ..domain.execution_identity import CandidateExecutionIdentities


@runtime_checkable
class CandidateExecutionIdentityStore(Protocol):
    """Durable read/write of who executed a candidate, keyed by that candidate."""

    def record(
        self, key: AttemptKey, identities: CandidateExecutionIdentities
    ) -> None:
        """Persist ``identities`` as the evidence for ``key``.

        Implementations MUST reject identities naming a commit other than
        ``key.head_sha``; evidence filed under a candidate it does not describe
        is the stale-SHA defect #15 removed, wearing a different hat.
        """
        ...

    def read(self, key: AttemptKey) -> CandidateExecutionIdentities | None:
        """The recorded evidence for ``key``, or ``None`` if none was recorded.

        Reconstructed from durable storage alone. ``None`` means "nothing was
        ever recorded for this candidate"; a damaged record raises instead, so
        a gate cannot read corruption as absence.
        """
        ...
