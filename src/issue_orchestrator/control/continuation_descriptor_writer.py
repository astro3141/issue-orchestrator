"""Copying the agent's recorded intent while it still exists (#143, #149).

There is exactly one moment at which every fact a continuation needs is
simultaneously durable-in-reach and about to be destroyed: the publication gate
has just filed a verdict for candidate ``A``, and it filed it *while the
completion record that produced ``A`` was being processed*. Seconds later the
worktree is reaped and the record — the only place the agent said whether it
wanted a pull request, and what it claimed to have built — goes with it.

So this writer runs there, and it runs only when the verdict is a REFUSAL.
That bound is not thrift, it is the boundary of the problem:

* A candidate the gate PASSED continues down the ordinary path it is already
  on. Its review and its pull request are being driven by the live session
  right now, and a descriptor would invite a second, terminal-less driver to
  race the first for the same work.
* A candidate the gate REFUSED is the one whose ordinary path has just ended.
  If it is ever re-evaluated (#139) and passes, nothing will be left to say
  what to do with it.

**One descriptor per issue.** Writing supersedes: the intent recorded for an
older candidate of the same issue is cleared as the newer one is filed. That
keeps "which candidate is this issue currently offering" a fact the durable
record states rather than one a reader has to infer from an ordering the
sidecars do not carry — and it is what makes at most one control operation per
issue derivable. Only the *intent* is superseded; the evaluation history #139
exists to preserve is never touched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.attempt import Attempt, AttemptKey
from ..domain.continuation_descriptor import ContinuationDescriptor

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.issue_key import IssueKey
    from ..domain.models import CompletionRecord
    from ..ports.attempt_store import AttemptStore
    from ..ports.session_output import ValidationRecord
    from .publication_gate import PublicationGateOutcome

logger = logging.getLogger(__name__)


class ContinuationDescriptorWriter:
    """Files one candidate's copied completion intent on its attempt record."""

    def __init__(self, attempts: "AttemptStore") -> None:
        self._attempts = attempts

    def record_gate_outcome(
        self,
        *,
        issue_key: "IssueKey | None",
        completion: "CompletionRecord",
        outcome: "PublicationGateOutcome",
    ) -> ContinuationDescriptor | None:
        """Record intent for the candidate this gate outcome is about, if any.

        The seam the completion pipeline calls, so the pipeline states only
        WHAT happened and this decides whether it is worth recording. Two
        outcomes carry no candidate to file intent under, and both record
        nothing:

        * a run with no verdict record executed no contract, so there is no
          commit it decided about;
        * a caller with no canonical issue identity cannot bind the intent to
          ``(issue, A)``, and a descriptor nothing could find again is worse
          than none. This is the answer
          :meth:`~.publication_gate.PublicationGate._record_verdict` gives to
          the same question, for the same reason.
        """
        gate_record = outcome.record
        if issue_key is None or gate_record is None:
            return None
        return self.record_refused_candidate(
            issue_key=issue_key, completion=completion, gate_record=gate_record
        )

    def record_refused_candidate(
        self,
        *,
        issue_key: "IssueKey",
        completion: "CompletionRecord",
        gate_record: "ValidationRecord",
    ) -> ContinuationDescriptor | None:
        """Copy this refused candidate's intent, or record nothing at all.

        Args:
            issue_key: The candidate's canonical issue identity — the same one
                the gate filed its verdict under.
            completion: The agent's completion record, still readable. Its
                ``requested_actions``, ``implementation`` and ``problems`` are
                copied verbatim; nothing here consults issue text, labels,
                logs, diagnostics, URLs or branch names.
            gate_record: The verdict the gate just reached. Supplies the
                contract identity and, decisively, the commit: the descriptor
                is filed under the SHA the gate actually evaluated, never under
                a branch's current tip.

        Returns:
            The descriptor filed, or ``None`` when nothing was written — a
            passing verdict (the ordinary path owns that candidate), or a store
            that refused the write. ``None`` is not a degraded success: it
            means no continuation will ever run for this candidate, which is
            the safe direction and the one #143 requires.
        """
        if gate_record.passed:
            return None
        descriptor = ContinuationDescriptor(
            requested_actions=tuple(completion.requested_actions),
            implementation=completion.implementation or "",
            problems=completion.problems or "",
            suite=gate_record.suite,
            command=gate_record.command,
            profile=gate_record.profile,
        )
        try:
            key = AttemptKey(issue_key, gate_record.head_sha)
        except ValueError as exc:
            # A gate record whose head_sha does not name one exact commit
            # cannot bind intent to a candidate. Refusing is the whole
            # contract: a descriptor filed under an identity that compares
            # unequal to a real HEAD later would be intent about nothing.
            logger.warning(
                "[CONTINUATION] no descriptor for %s: gate record names no "
                "exact commit: %s",
                issue_key,
                exc,
            )
            return None
        try:
            self._supersede_other_candidates(issue_key, keep=key)
            self._attempts.update(
                key, lambda attempt: attempt.with_continuation_descriptor(descriptor)
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "[CONTINUATION] no descriptor for %s@%s: %s",
                issue_key,
                key.head_sha[:12],
                exc,
            )
            return None
        logger.info(
            "[CONTINUATION] recorded intent for %s@%s: actions=%s profile=%s",
            issue_key,
            key.head_sha[:12],
            ",".join(action.value for action in descriptor.requested_actions) or "none",
            descriptor.profile,
        )
        return descriptor

    def _supersede_other_candidates(
        self, issue_key: "IssueKey", *, keep: AttemptKey
    ) -> None:
        """Drop the recorded intent from this issue's older candidates.

        Raises whatever the store raises: a supersession that could not be
        established must abort the write it precedes, or the issue would end up
        with two candidates both claiming to be what it currently offers.
        """
        for attempt in self._attempts.for_issue(issue_key):
            if attempt.continuation_descriptor is None:
                continue
            if attempt.key.head_sha == keep.head_sha:
                continue
            self._attempts.update(attempt.key, _without_descriptor)
            logger.info(
                "[CONTINUATION] superseded intent for %s@%s: %s now carries it",
                issue_key,
                attempt.key.head_sha[:12],
                keep.head_sha[:12],
            )


def _without_descriptor(attempt: Attempt) -> Attempt:
    """The same record with its recorded intent cleared, and nothing else."""
    return attempt.without_continuation_descriptor()


class _RecordsNoIntent(ContinuationDescriptorWriter):
    """A writer for a composition with no attempt store to file intent in.

    A null OBJECT rather than an optional collaborator, so no call site
    branches on whether intent is being recorded. What it does is the safe
    direction by construction: recording nothing means no continuation can ever
    run for these candidates, which is refusal, not permission.
    """

    def __init__(self) -> None:
        """Deliberately holds no store: there is nothing for it to write to."""

    def record_gate_outcome(self, **kwargs: object) -> None:
        return None

    def record_refused_candidate(self, **kwargs: object) -> None:
        return None


NO_CONTINUATION_DESCRIPTORS = _RecordsNoIntent()
"""The writer a composition without an attempt store gets."""


__all__ = [
    "NO_CONTINUATION_DESCRIPTORS",
    "ContinuationDescriptorWriter",
]
