"""The run one control operation currently has open (#149).

A continuation pass is not a run, and treating it as one silently defeats four
mechanisms the completion pipeline it composes depends on.

``CompletionProcessor.process`` does not necessarily finish. When a background
job supervisor is wired — which is precisely when the continuation runs at all,
because its own execution is submitted through the same supervisor — the review
exchange is submitted as its own job and ``process`` returns
``review_exchange_deferred``. :attr:`~.completion_types.ProcessingResult.is_non_terminal`
states the contract that follows: *the session is not terminated, and the
completion record is intentionally left on disk so the next observation
re-enters the pipeline*. A pass that materialised a worktree and deleted it on
the way out would delete it out from under the exchange still using it as its
working directory — its run dir, its exchange dir, and the ``summary.json`` the
resume path reads.

The identity that has to survive is not just the directory. Everything
downstream is keyed to a RUN:

===========================================  =================================
``run_id`` in the exchange's job identity     collapses repeated completion
                                              checks onto one exchange thread
``summary.json`` under the run dir            the resume signal
the review-exchange cache in the worktree     ``decide_review_exchange_resumption``
the same cache's no-completion budget         bounds exchange retries
===========================================  =================================

Minting a fresh worktree and a fresh ``run_id`` every pass does not disagree
with any of those — it never presents the same identity twice, so none of them
can recognise the work they are about, and each pass spawns another exchange.

So the run is owned here, for as long as the operation has one open, and
disposed exactly once. Process-local, like
:class:`~.continuation_in_flight.ContinuationsInFlight` and for the same
reason: an open run is a claim about a background thread that exists, and no
such claim survives the process that made it. A restart re-materialises under
the same deterministic worktree name and mints a new run, which is correct —
the exchange it would have resumed died with the engine.

Kept SEPARATE from the in-flight claim rather than folded into it, because the
two have different lifetimes on purpose. The claim spans one job submission and
must be released the moment it ends, or the next pass could never resume the
deferred exchange. The run spans however many passes the pipeline needs, and is
released only when the pipeline says it has finished.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Container

    from ..domain.control_operation import ControlOperationKey
    from ..domain.session_run import SessionRunAssets
    from ..ports.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContinuationRun:
    """One disposable worktree, its run assets, and where its intent is written.

    A frozen aggregate rather than four values threaded separately, because the
    four belong to the same run by construction: ``completion_path`` is derived
    from ``agent_label`` and this run's own directory name, and the assets name
    the ``run_id`` the exchange's job identity is built from. A caller that held
    only some of them could re-enter the pipeline under an identity it had
    partly re-minted, which is exactly the failure this module exists to stop.
    """

    worktree: Path
    agent_label: str
    assets: "SessionRunAssets"
    completion_path: str


class ContinuationRuns:
    """Every operation's open run, and the single place one is disposed of."""

    def __init__(self, worktrees: "WorktreeManager") -> None:
        self._lock = threading.Lock()
        self._open: dict["ControlOperationKey", ContinuationRun] = {}
        self._worktrees = worktrees

    def resume(self, key: "ControlOperationKey") -> ContinuationRun | None:
        """The run ``key`` already has open, or ``None`` if it has none.

        ``None`` is the instruction to mint one. It is never a fallback for a
        run this engine has but could not find: nothing here searches the
        filesystem for a previous run directory, which is the rediscovery the
        run-asset ownership rules forbid on an active path.
        """
        with self._lock:
            return self._open.get(key)

    def holds(self, key: "ControlOperationKey") -> bool:
        """Whether this engine is carrying an already-open run for ``key``.

        Asked by liveness derivation, which needs to tell "the continuation's
        run allowance is spent and the run that spent it is still going" from
        "it is spent and nothing came of it". The first must stay live; the
        second must return the candidate to ordinary rework.
        """
        with self._lock:
            return key in self._open

    def opened(self, key: "ControlOperationKey", run: ContinuationRun) -> None:
        """Record ``run`` as the run ``key`` now has open."""
        with self._lock:
            self._open[key] = run

    def close(self, key: "ControlOperationKey") -> None:
        """Dispose of ``key``'s open run, exactly once.

        Idempotent, and the removal happens outside the lock with the entry
        already taken: a second caller finds nothing to close rather than
        waiting on a checkout removal to decide it has nothing to do. A removal
        that fails raises — the run is already forgotten, so the next pass
        materialises a fresh one under the same deterministic name rather than
        re-entering the pipeline against a checkout that may be half-gone.
        """
        with self._lock:
            run = self._open.pop(key, None)
        if run is None:
            return
        logger.debug("[CONTINUATION] closing the run held for %s", key)
        self._worktrees.remove_checkout(run.worktree, force=True)

    def close_dropped(self, live: "Container[ControlOperationKey]") -> None:
        """Dispose of every open run whose operation is no longer live.

        An operation can stop being live without its run reaching a terminal
        result — a newer candidate supersedes its recorded intent, or a pull
        request arrives on the board some other way — and its run would then be
        held by an operation nothing will ever advance again. This is the only
        place that sees the whole live set, so it is the only place that can
        answer "held by nobody".

        Callers must pass a set derived from a READABLE reconciliation. "We
        could not tell what is live" is not "nothing is live", and closing on
        the strength of a broken instrument would delete the runs of every
        operation still going.

        **This can delete a worktree a review exchange is still working in.**
        The window is narrow — a newer candidate must file its own intent, which
        supersedes this one's, on an issue whose lane this very operation is
        excluding from ordinary work — but it is real, because the continuation
        releases its in-flight claim between passes by design and so
        ``EXECUTING`` does not cover a deferred exchange. It is accepted rather
        than closed: the operation is superseded, so its exchange is working on
        a candidate the issue no longer offers, and the alternative is a
        checkout no code path will ever dispose of. The exchange thread fails
        when its directory disappears, which is a loud end to obsolete work
        rather than a silent one.

        A removal that fails is caught PER KEY. This runs on the tick thread,
        inside ``advance``, ahead of every ``_start`` for the reconciliation: one
        undisposable checkout must not abort the sweep or stop other operations
        from being advanced, and there is nothing the tick could do about it
        anyway. The entry is already forgotten, so the failure cannot repeat.
        """
        with self._lock:
            dropped = [key for key in self._open if key not in live]
        for key in dropped:
            logger.info(
                "[CONTINUATION] %s is no longer live; closing the run it held",
                key,
            )
            try:
                self.close(key)
            except Exception as exc:
                logger.warning(
                    "[CONTINUATION] the run held for %s could not be closed: %s",
                    key,
                    exc,
                )


__all__ = ["ContinuationRun", "ContinuationRuns"]
