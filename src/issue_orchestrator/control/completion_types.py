"""Shared completion-processing result types."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..domain.review_exchange_run import ReviewExchangeRunAssets

ERROR_PREFIX_PUSH = "push_branch"
ERROR_PREFIX_CREATE_PR = "create_pr"
ERROR_PREFIX_PUBLISH_BLOCKED = "publish_blocked"
# A COMPLETED tech_lead session whose decision artifact pair is missing or
# rejected. Classified critical so the session's authoritative outcome is
# FAILED (ADR-0031 / #6761 finding 3), not a quiet success.
ERROR_PREFIX_TECH_LEAD_DECISION = "tech_lead_decision"
# A COMPLETED tech_lead session whose orchestrator-owned launch authority is
# missing, or whose agent-writable worktree copies (assignment / manifest)
# no longer match it — tamper evidence. Critical like the decision prefix
# (#6761 re-review finding 1).
ERROR_PREFIX_TECH_LEAD_AUTHORITY = "tech_lead_authority"
# A label that only NeedsHumanBlock may write reached an ordinary label writer
# (#6999 F2 round 5). Its own prefix, and NOT one of the publish prefixes,
# because the two consequences differ: a publish error is retried by the
# publish-recovery lane, whereas re-running this one would refuse again. What it
# must do instead is force the completion to FAIL, so an untrusted block request
# that was dropped can never be reported as a success.
ERROR_PREFIX_GOVERNED_LABEL = "governed_label"
REVIEW_EXCHANGE_ERROR_PREFIX = "review_exchange:"


@dataclass(frozen=True, slots=True)
class CodeCandidateSettlement:
    """Whether a settled completion still offers a code candidate to validate.

    A code-candidate validation gate may run only when the trusted completion
    settlement still presents a code candidate. The seam that KNOWS that — the
    tech_lead completion owner, which proved a planning run left its checkout
    at the commit it was launched on (#202) — is not the seam that decides
    whether to run the gate. This is the fact that carries the answer between
    them.

    Before #328 the answer was logged and dropped: ``SessionController`` saw
    only a ``COMPLETED`` status, ran the ordinary quick gate over an unchanged
    base commit, and wrote candidate-shaped PASS evidence for a run that had
    offered no candidate. Downstream code must CARRY this settlement; it must
    never re-derive it from a role name, a task kind, a session-name prefix, or
    the shape of the actions the completion asked for.

    ``offers_code_candidate`` is True for every completion no owner has settled
    out of the code lane. That is the ordinary Actor/Rework case, and it is also
    the only fail-safe direction: a missing, malformed, ambiguous, or refused
    settlement keeps today's gate. ``detail`` is the settling owner's own
    sentence, so the log of a skipped gate says which run proved what.
    """

    offers_code_candidate: bool
    detail: str

    @classmethod
    def presented(cls) -> "CodeCandidateSettlement":
        """The ordinary lane: nothing has settled this completion out of it."""
        return cls(offers_code_candidate=True, detail="")

    @classmethod
    def settled_zero_code(cls, detail: str) -> "CodeCandidateSettlement":
        """A trusted owner PROVED this completion offers no code candidate."""
        return cls(offers_code_candidate=False, detail=detail)

    def carried_by(self, result: "ProcessingResult") -> "ProcessingResult":
        """``result``, naming this settlement for its downstream readers.

        Applied at every terminal exit of completion processing rather than at
        the one that happens to be the common case, for the same reason
        :meth:`ActionExecutionOutcome.of` re-stamps its early result: a fact
        that only ONE exit carries is the easiest thing in the pipeline to drop
        silently.
        """
        return replace(result, code_candidate=self)


@dataclass
class ProcessingResult:
    """Result of processing a completion record."""

    success: bool
    message: str
    failure_kind: str | None = None
    pr_url: str | None = None
    actions_taken: list[str] | None = None
    diagnostic_path: str | None = None
    completion_record_path: str | None = None
    errors: list[str] | None = None
    review_exchange_completed: bool = False
    review_exchange_halted: bool = False
    # True when the review exchange is running asynchronously and completion
    # processing for this record must retry on a future tick. Callers must NOT
    # treat the session as terminated while this flag is set — the completion
    # record is intentionally left on disk so the next observation re-enters
    # the pipeline.
    review_exchange_deferred: bool = False
    # True when a post-review validation failure was preserved and rerouted
    # back into coder rework via the review-exchange path. Callers should keep
    # the session running but still surface validation-failure evidence.
    validation_failed_rerouted: bool = False
    # WHERE this completion's review exchange put its artifacts, when one ran
    # and reached an outcome (#180). Every other review-exchange field here
    # reports *that* something happened; this one is the only fact that says
    # where the evidence for it landed, and it cannot be derived from anything
    # else a caller holds: the exchange allocates a run of its own, a sibling
    # of the session run rather than a directory beneath it. Without it the one
    # caller that must read an exchange's verdict back — the control
    # continuation, promoting ``CHANGES_REQUESTED(A)`` onto its attempt — has
    # to guess a directory, and guessed the session's.
    #
    # ``None`` means no exchange outcome was reached on this pass: none was
    # required, one is still running in the background, or the exchange halted
    # before a run existed.
    review_exchange_run: ReviewExchangeRunAssets | None = None
    # What the trusted completion settlement leaves for a downstream code gate
    # to judge (#328). Defaulted to the ordinary lane so every producer that
    # never met a settling owner — and every refusal, which proves nothing
    # about a checkout — keeps the gate exactly as it is today.
    code_candidate: CodeCandidateSettlement = field(
        default_factory=CodeCandidateSettlement.presented
    )

    @classmethod
    def for_review_exchange_deferred(cls) -> "ProcessingResult":
        """Typed constructor for the async review-exchange deferral result."""
        return cls(
            success=True,
            message="Review exchange running in background; will resume on next tick",
            completion_record_path=None,
            review_exchange_deferred=True,
        )

    @property
    def is_non_terminal(self) -> bool:
        """True when completion has NOT finished for this record.

        The review exchange is running in the background (``review_exchange_deferred``)
        and/or a post-review validation failure was rerouted into coder rework
        (``validation_failed_rerouted``). The live session path leaves such a
        completion pending — ``SessionController`` maps it to ``SessionStatus.RUNNING``
        and resumes publishing on a later tick. Other consumers of a
        ``ProcessingResult`` (e.g. retry-publish reconciliation) must not treat a
        non-terminal result as terminal success, or they would clear recovery
        state before publish actually completes.
        """
        return self.review_exchange_deferred or self.validation_failed_rerouted


@dataclass(frozen=True)
class ActionExecutionOutcome:
    """What executing one completion record's requested actions produced.

    Named rather than returned as a tuple because ``review_exchange_run`` is a
    fact no other field implies and no caller can re-derive: the phase that
    runs the exchange is the only one that ever holds the run it allocated, so
    a positional slot for it would be the easiest thing in the pipeline to drop
    silently on the way out (#180).

    ``deferred`` and ``early_result`` are the two ways this phase can end
    without having finished the record: the exchange is running in the
    background, or an action produced a result the caller must return as-is.

    Build one with :meth:`of`, never by calling this constructor: an
    ``early_result`` is returned to the caller VERBATIM and never passes the
    place the outcome's own ``review_exchange_run`` is read, so a result that
    did not carry the run itself would lose it. That is the same drop the type
    exists to prevent, one exit further out.
    """

    branch: str | None
    pr_url: str | None
    review_exchange_completed: bool
    deferred: bool
    early_result: "ProcessingResult | None"
    review_exchange_run: ReviewExchangeRunAssets | None

    @classmethod
    def of(
        cls,
        *,
        branch: str | None,
        pr_url: str | None,
        review_exchange_completed: bool,
        review_exchange_run: ReviewExchangeRunAssets | None,
        deferred: bool = False,
        early_result: "ProcessingResult | None" = None,
    ) -> "ActionExecutionOutcome":
        """The outcome, with ``early_result`` made to name the same run.

        One derivation of "which run did this pass's exchange use", applied to
        both places it can be read from — this outcome, and the early result
        that bypasses it. A result that already names a run keeps it: the
        post-review validation reroute allocates a SECOND exchange run, and its
        result is evidence about that one, not about the exchange the reroute
        superseded.
        """
        if early_result is not None and early_result.review_exchange_run is None:
            early_result = replace(
                early_result, review_exchange_run=review_exchange_run
            )
        return cls(
            branch=branch,
            pr_url=pr_url,
            review_exchange_completed=review_exchange_completed,
            deferred=deferred,
            early_result=early_result,
            review_exchange_run=review_exchange_run,
        )


RepublicationCheck = Callable[[], ProcessingResult | None]
"""Re-runs the publication gate for a commit a push retry rewrote (#45).

Bound at the completion boundary that owns the run — the only place holding
the candidate's canonical identity and its frozen run assets — so the retry
deep inside the push path can ask "is the commit I am about to publish
certified?" without carrying five loose values down to ask it. ``None`` means
publication may proceed; a ``ProcessingResult`` is the refusal, already
reported and labelled by the ordinary gate-failure handler.
"""
