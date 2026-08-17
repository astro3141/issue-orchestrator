"""The publication-gate verdict for an issue, and what it authorizes (#45).

The ordinary order of an issue's lifecycle is fixed (#21)::

    actor -> validation (publication gate) -> review -> PR -> human merge

So a publication gate that did *not* pass cannot authorize the review step.
The gate already recorded its failure as an issue label, but nothing read it
back: ``needs-code-review`` left on a PR by an earlier candidate stayed
review-eligible, and a review launched against a candidate the gate had just
rejected.

Two halves of the issue-scoped verdict live here, deliberately in one module:

* :class:`PublicationAuthority` writes it. ``revoke`` on a failed gate,
  ``grant`` when a candidate clears every publication precondition. Both are
  needed: a revocation with no matching grant would outlive the candidate that
  earned it and hold a later, genuinely validated candidate out of review
  forever.
* :func:`publication_gate_failed` reads it, and is what
  :mod:`.review_validity` — the one seam the scanner, startup recovery and
  the launcher all consult — asks before treating a review as valid.

The verdict is durable GitHub label state rather than in-process memory
because the orchestrator recovers its state from labels after a restart
(AGENTS.md, "Labels as Source of Truth"). A crash between the gate and the
review must not lose the refusal.

A label write can nonetheless fail, and that is the asymmetry
:class:`UnrecordedRefusals` closes. ``grant`` failing is safe on its own — the
marker survives and review stays blocked — but ``revoke`` failing used to leave
a refused candidate review-eligible, which is exactly the state this module
exists to prevent, reachable through one failed write. A refusal that cannot be
proved recorded is therefore held here instead, and read back beside the label,
so "not provably recorded" withholds review rather than silently allowing it.

The third record is the candidate-scoped receipt, and it lives in
:mod:`.publication_evidence` because it is about one commit rather than one
issue. :class:`PublicationVerdictReader` binds all three into the single
collaborator every reader of the verdict is given, so no consumer can read a
subset of them and reach a different answer.

That hold is itself durable (#51). Held only in process, it was lost to a
restart, and review became eligible again for a candidate the gate had refused
— the same hole one step further out. It is now latched in the
orchestrator-owned ledger behind ``PublicationRefusalLatch``, and rebuilt from
it at construction, so the fact that a refusal could not be recorded remotely
outlives the process that observed it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..ports.publication_refusal_latch import PublicationRefusalLatch
from .completion_ports import LabelAdapter
from .publication_evidence import (
    CandidatePublicationEvidence,
    PublicationCertification,
)

if TYPE_CHECKING:
    from ..domain.issue_key import IssueKey
    from ..infra.validation_profiles import ValidationProfileRegistry
    from ..ports.attempt_store import AttemptStore
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


class _ProcessLocalLatch:
    """A latch that remembers nothing across a restart.

    The pre-#51 behaviour, kept as an explicitly named null object for
    compositions that genuinely have no ledger to write to (unit tests, and
    control-layer defaults for collaborators a composition root always injects).
    Production wires the durable latch; this one is never a silent substitute
    for it, because a caller has to ask for it by name.
    """

    def __init__(self) -> None:
        self._issue_numbers: set[int] = set()

    def latch_publication_refusal(self, issue_number: int) -> None:
        self._issue_numbers.add(issue_number)

    def release_publication_refusal(self, issue_number: int) -> None:
        self._issue_numbers.discard(issue_number)

    def latched_publication_refusals(self) -> frozenset[int]:
        return frozenset(self._issue_numbers)


class UnrecordedRefusals:
    """Publication-gate refusals whose durable write did not commit (#45, #51).

    One instance per orchestrator, shared by the writer
    (:class:`PublicationAuthority`) and by every reader of the verdict, so a
    refusal the label write lost still withholds review from the candidate it
    refused. Held here rather than on the issue precisely because the issue is
    what could not be written; the label remains the primary record and this
    only ever *adds* refusals, never removes one the label already proves.

    A hold survives a restart because it is latched in the orchestrator-owned
    ledger and re-read here at construction (#51). The in-memory set is a
    mirror, not a second source of truth: it exists so a read can never fail
    open, and it is only ever narrower than the latch when a release commits.
    It fails in the same direction as the rest of the verdict — a refusal is
    only ever forgotten when a later candidate is proved to have cleared the
    gate.

    Completions run off the tick thread, so the holder and the readers are
    different threads. No lock: every in-memory operation is one atomic set
    operation with no read-modify-write to tear, and the latch owns its own
    concurrency.
    """

    def __init__(self, latch: PublicationRefusalLatch) -> None:
        self._latch = latch
        # Rebuilt, not started empty: this is the whole of #51. Anything the
        # previous process could not record remotely is still refused.
        self._issue_numbers: set[int] = set(latch.latched_publication_refusals())

    @classmethod
    def process_local(cls) -> "UnrecordedRefusals":
        """An instance with no durable latch: holds die with the process.

        For compositions that have no ledger. A composition root that means to
        survive a restart passes the real latch instead.
        """
        return cls(_ProcessLocalLatch())

    def hold(self, issue_number: int) -> None:
        """Withhold review from this issue: its refusal is not recorded.

        The in-memory hold is taken FIRST so no reader can observe the issue as
        unrefused while the latch is being written, and a latch write that
        fails is logged rather than raised: it degrades to the process-bounded
        cover, which is still strictly better than leaving the candidate
        review-eligible, and raising here would destroy the caller's ability to
        report the gate failure at all.
        """
        self._issue_numbers.add(issue_number)
        try:
            self._latch.latch_publication_refusal(issue_number)
        except Exception as exc:
            logger.error(
                "Could not latch the unrecorded publication-gate refusal for "
                "issue #%d durably; review stays withheld in this process, but "
                "a restart before a later candidate clears the gate will lose "
                "it: %s",
                issue_number,
                exc,
            )

    def release(self, issue_number: int) -> None:
        """Drop the hold, because the verdict is now settled elsewhere.

        The durable release commits first, and the in-memory hold is only
        dropped once it has. A release that did not commit has settled nothing,
        so keeping both fails closed: review stays withheld until the next
        settlement succeeds, exactly as an uncommitted label removal does.
        """
        try:
            self._latch.release_publication_refusal(issue_number)
        except Exception as exc:
            logger.warning(
                "Could not release the durable publication-refusal latch for "
                "issue #%d; review stays withheld until a later candidate "
                "clears the gate: %s",
                issue_number,
                exc,
            )
            return
        self._issue_numbers.discard(issue_number)

    def holds(self, issue_number: int) -> bool:
        """Whether an unrecorded refusal still stands against this issue."""
        return issue_number in self._issue_numbers


def publication_gate_failed(
    label_manager: "LabelManager",
    labels: Sequence[str],
    *,
    issue_number: int,
    unrecorded: UnrecordedRefusals,
) -> bool:
    """Whether this issue's publication gate refused its current candidate.

    Reads the resolved (prefix-aware) marker from the label registry, which is
    the same name :class:`PublicationAuthority` writes — the two cannot drift
    apart into "written under a prefix, read without one" — and then the
    refusals that could not be written at all, which are just as much a
    refusal and must read as one.
    """
    if label_manager.validation_failed in labels:
        return True
    return unrecorded.holds(issue_number)


@dataclass(frozen=True, slots=True)
class PublicationVerdictReader:
    """One candidate's publication verdict, however it happens to be recorded.

    Three records answer the same question and none of them answers it alone:
    the refusal marker on the issue, the refusals whose write to that issue did
    not commit, and the receipt filed against ``Attempt(issue, A)``. They are
    bundled into one collaborator because a reader that held only some of them
    would answer *differently*, and the three consumers of the verdict — the PR
    scanner, startup recovery and the launcher — must not be able to do that.

    That is not hypothetical: reading only the marker is the pre-#45 behaviour
    that let a review launch against a rejected candidate, and reading only the
    marker plus the unrecorded refusals still admits a candidate no gate ever
    saw. Passing the halves separately makes forgetting one a valid call.
    """

    unrecorded: UnrecordedRefusals
    candidate_evidence: CandidatePublicationEvidence

    @classmethod
    def over(
        cls, unrecorded: UnrecordedRefusals, attempts: "AttemptStore"
    ) -> "PublicationVerdictReader":
        """Build the reader from the stores the verdict actually lives in.

        The composition root names the two durable homes — the refusal latch
        and the attempt store — and nothing else. That the candidate half is
        read by wrapping the attempt store is this module's business, not the
        wiring's.
        """
        return cls(unrecorded, CandidatePublicationEvidence(attempts))

    def refuses_issue(
        self,
        label_manager: "LabelManager",
        labels: Sequence[str],
        *,
        issue_number: int,
    ) -> bool:
        """Whether a refusal stands against this issue (recorded or lost)."""
        return publication_gate_failed(
            label_manager,
            labels,
            issue_number=issue_number,
            unrecorded=self.unrecorded,
        )

    def certifies_candidate(
        self,
        *,
        issue_key: "IssueKey | None",
        head_sha: str | None,
        profiles: "ValidationProfileRegistry",
    ) -> PublicationCertification:
        """Whether this exact candidate proved it cleared the gate."""
        return self.candidate_evidence.certification(
            issue_key=issue_key,
            head_sha=head_sha,
            profiles=profiles,
        )


class PublicationAuthority:
    """The one owner of an issue's publication-gate verdict.

    Constructed with the resolved marker label so callers never spell it
    themselves; :attr:`label` is what a failure comment should name.
    """

    def __init__(
        self,
        labels: LabelAdapter,
        marker_label: str,
        unrecorded: UnrecordedRefusals,
    ) -> None:
        self._labels = labels
        self._label = marker_label
        self._unrecorded = unrecorded

    @property
    def label(self) -> str:
        """The label a recorded publication-gate failure wears."""
        return self._label

    def revoke(self, issue_number: int, *, reason: str) -> None:
        """Record that this issue's publication gate refused its candidate.

        Fails CLOSED. A write failure is not raised — the caller still reports
        the gate failure through its result and its issue comment, and losing
        the label must not turn a refused publication into an exception that
        hides the reason for the refusal — but "not raised" is not "not
        recorded": the refusal is held in :class:`UnrecordedRefusals` instead,
        which withholds review from this candidate exactly as the label would.
        Reporting the refusal and enforcing it are two obligations, and this
        method owes both.
        """
        try:
            self._labels.add_label(issue_number, self._label)
        except Exception as exc:
            # Held BEFORE the log line: nothing between here and the reader may
            # observe the issue as unrefused.
            self._unrecorded.hold(issue_number)
            logger.error(
                "Failed to add '%s' label to issue #%d (%s); the refusal is "
                "held in this process instead, so review stays withheld until "
                "a later candidate clears the gate: %s",
                self._label,
                issue_number,
                reason,
                exc,
            )
            return
        # The label now proves the refusal on its own, so any earlier
        # in-process hold for this issue is redundant.
        self._unrecorded.release(issue_number)
        logger.info(
            "Added '%s' label to issue #%d due to validation failure: %s",
            self._label,
            issue_number,
            reason,
        )

    def grant(self, issue_number: int) -> None:
        """Record that a candidate cleared every publication precondition.

        Removal is idempotent, so this is safe on the ordinary path where no
        failure was ever recorded. A write failure leaves the marker in place,
        which fails closed — review stays blocked until the next candidate
        clears the gate — so it is logged rather than raised, and any
        in-process hold is kept for the same reason.
        """
        try:
            self._labels.remove_label(issue_number, self._label)
        except Exception as exc:
            logger.warning(
                "Failed to clear '%s' label from issue #%d; review stays "
                "blocked until a later candidate clears the gate: %s",
                self._label,
                issue_number,
                exc,
            )
            return
        # Only now, with the durable removal committed, is the refusal over.
        self._unrecorded.release(issue_number)
        logger.debug(
            "Cleared '%s' from issue #%d: publication preconditions passed",
            self._label,
            issue_number,
        )


__all__ = [
    "PublicationAuthority",
    "PublicationVerdictReader",
    "UnrecordedRefusals",
    "publication_gate_failed",
]
