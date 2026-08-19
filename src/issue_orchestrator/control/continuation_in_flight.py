"""Which control operations THIS engine is executing right now (#149).

Everything else about a continuation is derived from durable records, and
deliberately so. This one fact cannot be, and leaving it out was a hole rather
than a simplification.

#139's allowance is a *start* budget: ``PublicationRevalidation.revalidate``
spends it before any gate work begins, then runs the suite synchronously inside
the background job. Throughout that window the durable facts read ``allowance
spent, latest publication evaluation still non-PASS`` — byte-identical to a
revalidation that finished and failed. So the phase derives ``EXHAUSTED``, the
reconciliation drops the key from the live set, the lease is released, and the
queue re-admits the issue *while the operation is executing* — the precise
window #148 measured and the continuation exists to close.

No durable marker can distinguish the two states honestly. A "reserved, not yet
reported" row cannot say whether the process that wrote it still exists, so
after a crash it would pin the operation live forever — worse than the bug, and
the opposite of the fail-closed direction #139 chose deliberately. Process-local
memory answers exactly the question being asked, and answers it correctly by
construction on both sides: while the engine runs, it knows what it started;
when the engine dies, so does the claim, and the phase falls back to the durable
facts.

The claim is therefore also the runner's duplicate guard. It is taken on the
tick thread before the job is submitted, not inside the job, because a job
runner may queue work: between "submitted" and "started" the operation would
otherwise be unclaimed, which is the same window in miniature.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.control_operation import ControlOperationKey


class ContinuationsInFlight:
    """The set of control operations this engine has running, at any instant.

    Written by the runner (tick thread claims, job thread releases) and read by
    live truth (tick thread), so the set is guarded by a lock rather than
    relying on which individual set operations happen to be atomic.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executing: set["ControlOperationKey"] = set()

    def claim(self, key: "ControlOperationKey") -> bool:
        """Take the execution claim for ``key``, or report it already taken.

        ``False`` is not an error: it means this engine already has a run in
        flight for that exact candidate, which is precisely when a second must
        not start. Atomic, so two ticks cannot both be told they claimed it.
        """
        with self._lock:
            if key in self._executing:
                return False
            self._executing.add(key)
            return True

    def release(self, key: "ControlOperationKey") -> None:
        """Give up the execution claim for ``key``.

        Idempotent: releasing something not held is how a submit that never
        started unwinds its own claim, and it must not be able to raise inside
        the ``finally`` that guarantees a finished run releases.
        """
        with self._lock:
            self._executing.discard(key)

    def is_executing(self, key: "ControlOperationKey") -> bool:
        """Whether this engine has a run in flight for ``key``."""
        with self._lock:
            return key in self._executing


__all__ = ["ContinuationsInFlight"]
