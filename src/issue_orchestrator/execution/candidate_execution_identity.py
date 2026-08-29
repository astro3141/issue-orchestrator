"""Binds one exchange's durable evidence to the candidate it reviewed.

Two records, one observation: the two execution identities (§4's I2c half) and
the reviewer's verdict bound to the same commit (#345). They were already
"halves of one admission" in the failure directions below; since #345 they are
also halves of one recorder, because the thing that makes either of them
evidence is the SAME orchestrator observation of what was put in front of the
reviewer. A caller that could file one without the other would be a caller able
to record who reviewed a commit without recording what they decided.


The identities themselves are fixed for the whole exchange — they are the
launcher's own configuration for the two roles. What is *not* fixed is the
candidate: the coder's branch moves between rounds, so the commit the evidence
is about has to come from the orchestrator's observation at the
candidate/review authority boundary, which is
:meth:`~.reviewer_worktree.ReviewerCandidatePresentation.present` — the act
that put the commit in front of the reviewer, reporting what it checked out.

That is why this recorder takes the presented SHA per call rather than holding
one. A session-start HEAD is a different fact: it is what the worktree held
before the actor committed anything, and accepting it here would reproduce
exactly the stale-SHA defect #15 removed from the verdict binding.

Failure directions mirror the verdict binding, because the two records are
halves of one admission:

* an unusable observation records nothing and never fails the review it
  describes — an unbound identity is one no gate can admit, which is the safe
  direction;
* a failing *write* raises, per the repository's fail-fast stance. An
  unwritable authority artifact is not an absent one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domain.attempt import AttemptKey
from ..domain.commit_sha import normalize_commit_sha
from ..domain.execution_identity import (
    AgentExecutionIdentity,
    CandidateExecutionIdentities,
)
from ..domain.issue_key import IssueKey
from ..domain.review_verdict_binding import BoundReviewVerdict
from ..ports.candidate_review_verdicts import CandidateReviewVerdictStore
from ..ports.execution_identity_store import CandidateExecutionIdentityStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CandidateExecutionIdentityRecorder:
    """One exchange's durable evidence, awaiting a candidate to bind to."""

    store: CandidateExecutionIdentityStore
    issue_key: IssueKey
    actor: AgentExecutionIdentity
    reviewer: AgentExecutionIdentity
    review_verdicts: CandidateReviewVerdictStore

    def record(
        self, presented_head_sha: str | None
    ) -> CandidateExecutionIdentities | None:
        """Bind both identities to the commit presented for review.

        Returns ``None`` — writing nothing — when the orchestrator could not
        observe the presented commit as a canonical SHA.
        """
        try:
            candidate_sha = normalize_commit_sha(
                presented_head_sha, field_name="candidate_sha"
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[EXECUTION_IDENTITY] no usable presented HEAD for %s; recording "
                "no execution identities: %s",
                self.issue_key,
                exc,
            )
            return None
        identities = CandidateExecutionIdentities(
            candidate_sha=candidate_sha,
            actor=self.actor,
            reviewer=self.reviewer,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.record(AttemptKey(self.issue_key, candidate_sha), identities)
        return identities

    def record_verdict(self, binding: BoundReviewVerdict | None) -> None:
        """File what the reviewer decided about this candidate, durably (#345).

        ``binding`` is the record the exchange just wrote into its own
        directory, and it already carries the commit — read from the
        orchestrator's presentation, never from the reviewer's claim — so this
        needs no second observation and cannot disagree with the identities
        filed beside it.

        ``None`` is the exchange's own "no usable presented HEAD", and it writes
        nothing for the same reason the identities do: an unbound verdict is a
        verdict no gate can admit, which is the safe direction. A failing WRITE
        raises, per the repository's fail-fast stance — an unwritable authority
        artifact is not an absent one.
        """
        if binding is None:
            return
        self.review_verdicts.record(
            AttemptKey(self.issue_key, binding.reviewed_sha), binding
        )
