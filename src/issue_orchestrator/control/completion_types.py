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
# A COMPLETED tech_lead session whose MANDATORY completion validation — the one
# #385 moved off the model session onto a trusted owner, because running it
# needed shared-repository writes a bounded Tech Lead sandbox does not grant —
# is missing, failed, timed out, unavailable, or bound to another
# candidate/run. Critical like the two prefixes above: the completion protocol
# is still gated, so an ungated completion must be a FAILED session rather than
# a quiet success that could project a merge-facing PASS.
ERROR_PREFIX_TECH_LEAD_COMPLETION_VALIDATION = "tech_lead_completion_validation"
# A label that only NeedsHumanBlock may write reached an ordinary label writer
# (#6999 F2 round 5). Its own prefix, and NOT one of the publish prefixes,
# because the two consequences differ: a publish error is retried by the
# publish-recovery lane, whereas re-running this one would refuse again. What it
# must do instead is force the completion to FAIL, so an untrusted block request
# that was dropped can never be reported as a success.
ERROR_PREFIX_GOVERNED_LABEL = "governed_label"
# A run settled onto the zero-code lane whose issue comment — the ONLY thing it
# had to deliver — did not reach the issue (#337 round 2). Critical, because on
# that lane the comment IS the publication: the branch write was dropped, so
# nothing else carries the result, and a quiet success would close a work item
# whose deliverable never arrived.
#
# Its own prefix, and deliberately NOT one of the publish prefixes
# ``PublishRecoveryService`` reads: those arm durable retry locators for a
# Retry Publish that pushes a branch and opens a PR, and this run has no commit
# to push — offering that retry would send the operator back to the exact
# create_pr refusal #336 measured. What this prefix must do instead is route the
# completion to the bounded publish-failure owner, which counts the failures and
# escalates to needs-human.
ERROR_PREFIX_RESULT_UNDELIVERED = "result_undelivered"
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

    There are TWO producers, not one: the tech_lead completion owner at the
    pre-action seam, and the ordinary zero-code publication settler downstream
    of the review exchange (#337), which proves the same fact from stronger
    evidence — clean tracked content AND zero commits over the base a pull
    request would target. They are combined with :meth:`narrowed_by` rather
    than by letting a later phase overwrite an earlier one.
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

    def narrowed_by(
        self, later: "CodeCandidateSettlement"
    ) -> "CodeCandidateSettlement":
        """This settlement, or ``later`` when this one proved nothing.

        Combining rather than assigning is what keeps the direction fail-safe
        in BOTH orders: a phase that proved nothing returns
        :meth:`presented`, and that neutral answer must never erase a proof an
        earlier owner already made — nor may an earlier neutral answer suppress
        a later proof.
        """
        return later if self.offers_code_candidate else self


@dataclass(frozen=True, slots=True)
class ResultOnlyDelivery:
    """Whether the comment a finished run posted is its WHOLE delivery.

    An ordinary completion delivers through a pull request, and that chain is
    what gives its success a terminal disposition: the branch is pushed, the PR
    is opened, ``pr-pending`` takes over the ownership claim — which is
    precisely why the completion planner may RELEASE ``in-progress`` — and
    merging the PR closes the issue.

    A run PROVEN to offer no code candidate (#337) has no such carrier. Its
    result IS the issue comment: no PR url exists, so no ``pr-pending`` is
    stamped and no merge will ever arrive to close the issue. Without this
    fact the planner would release the claim label and leave a finished work
    item with no label at all — open, unclaimed, and immediately schedulable
    again — so the next tick would launch the same measurement, run the same
    review exchange, and post a second RESULT, unbounded.

    ``delivered`` is a fact about what LANDED, not about what the run had to
    give. Those are two different statements and the difference is the whole
    finding of #337 round 2: the settler decides the lane BEFORE the actions
    run — it has to, it shapes them — so at that moment it can only prove "this
    run has nothing but a comment to deliver". Consuming that as "the comment
    was delivered" closes an issue whose ``add_comment`` was rejected by the
    forge, or whose record carried no comment body at all: the RESULT never
    arrives and the work item is closed anyway, which is the exact inverse of
    what this lane exists to fix. So the claim is withdrawn unless the posting
    is confirmed, and the run takes the bounded publish-failure routing
    instead.

    False for every completion no owner settled this way, which is the
    fail-safe direction: exactly today's behaviour, including every refusal,
    which proves nothing about a checkout. ``detail`` is the settling owner's
    own sentence, so the record of a terminal disposition says which run proved
    what.
    """

    delivered: bool
    detail: str

    @classmethod
    def none(cls) -> "ResultOnlyDelivery":
        """Nothing settled this completion as result-only."""
        return cls(delivered=False, detail="")

    @classmethod
    def settled(cls, detail: str) -> "ResultOnlyDelivery":
        """A trusted owner PROVED the posted result is the whole delivery."""
        return cls(delivered=True, detail=detail)

    def narrowed_by(self, later: "ResultOnlyDelivery") -> "ResultOnlyDelivery":
        """This delivery, or ``later`` when this one settled nothing."""
        return later if not self.delivered else self


@dataclass(frozen=True, slots=True)
class CompletionSettlement:
    """Everything the completion phases PROVED about one finished run.

    One value rather than two loose arguments because both facts come out of
    the SAME proof and travel to two different readers — the code-validation
    gate (:class:`CodeCandidateSettlement`, #328) and the completion action
    planner (:class:`ResultOnlyDelivery`, #337). Split apart, a newly added
    exit carries whichever of the two it happens to remember; that is the
    silent drop :class:`ActionExecutionOutcome` already exists to prevent, one
    fact further in.
    """

    code_candidate: CodeCandidateSettlement
    result_only: ResultOnlyDelivery

    @classmethod
    def unsettled(cls) -> "CompletionSettlement":
        """No owner proved anything: today's ordinary behaviour, fail-safe."""
        return cls(
            code_candidate=CodeCandidateSettlement.presented(),
            result_only=ResultOnlyDelivery.none(),
        )

    def narrowed_by(self, later: "CompletionSettlement") -> "CompletionSettlement":
        """Both halves of this settlement, narrowed by a later phase's proof.

        Completion processing has two settling seams — the pre-action owner and
        the publication settler downstream of the review exchange — and they
        never both fire for one run. Narrowing rather than assigning means
        neither seam's neutral answer can erase the other's proof.
        """
        return CompletionSettlement(
            code_candidate=self.code_candidate.narrowed_by(later.code_candidate),
            result_only=self.result_only.narrowed_by(later.result_only),
        )

    def undelivered(self) -> "CompletionSettlement":
        """This settlement with its result-only half withdrawn.

        Only that half. The two are proven by different evidence at different
        times: ``code_candidate`` is proven by reads of the checkout before the
        actions run and is untouched by what they did — a comment that failed
        to post does not put commits on the branch — while ``result_only``
        claims a delivery landed, which only executing the plan can establish.
        Withdrawing both would hand the quick gate back a run with nothing to
        validate, reopening the #328 drift on a failure path.
        """
        return replace(self, result_only=ResultOnlyDelivery.none())

    def carried_by(self, result: "ProcessingResult") -> "ProcessingResult":
        """``result``, naming this settlement for its downstream readers.

        Applied at every terminal exit of completion processing rather than at
        the one that happens to be the common case, for the same reason
        :meth:`ActionExecutionOutcome.of` re-stamps its early result: a fact
        that only ONE exit carries is the easiest thing in the pipeline to drop
        silently.
        """
        return replace(
            result,
            code_candidate=self.code_candidate,
            result_only=self.result_only,
        )


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
    # Whether this run's posted comment is the whole delivery, so no pull
    # request will ever arrive to give the issue a terminal state (#337).
    # Defaulted to "nothing settled it", so every producer that never met a
    # settling owner keeps the ordinary PR-carried lifecycle exactly as it is.
    result_only: ResultOnlyDelivery = field(default_factory=ResultOnlyDelivery.none)

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


@dataclass(frozen=True, slots=True)
class ExecutedActions:
    """What running one completion's ordered actions actually produced.

    Named rather than returned as a tuple because ``result_delivered`` is the
    kind of fact a positional slot loses: it is the POSITIVE evidence that the
    issue comment reached the issue, recorded by the action that posted it, and
    it is what a terminal disposition may be planned from. Derived from the
    absence of an error instead, it would be wrong in both directions — an
    absent ``comment_body`` posts nothing and raises nothing, and an error from
    a later action says nothing about an earlier comment that did land.
    """

    branch: str | None
    pr_url: str | None
    review_exchange_completed: bool
    early_result: "ProcessingResult | None"
    result_delivered: bool


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

    ``settlement`` is what this phase PROVED about the run on its way through —
    the publication settler that decides the zero-code lane sits inside it, and
    both of its answers are read after it returns (#337). It is a required
    argument, not a defaulted one, so an exit that settled nothing has to say
    so with :meth:`CompletionSettlement.unsettled` rather than by omission.

    The discipline is deliberately strictest HERE and defaulted on the hops
    that follow (``handle_session_completion``,
    ``CompletionHandler.process_completion``,
    ``CompletionActionPlanner.generate_completion_actions``), and the asymmetry
    is about who can forget what. This type has ONE producer — the phase that
    holds the settler — and several exits out of it, so omission is the live
    risk and requiring the argument is what closes it. The hops downstream have
    a single live caller each and many test callers; requiring the argument
    there would buy no protection this constructor does not already give and
    would make every existing caller restate a fail-safe default. If a second
    production caller of any of those hops ever appears, the argument should
    become required at that hop too — its failure mode is silent (the finished
    issue simply reappears in the schedulable pool).

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
    settlement: CompletionSettlement

    @classmethod
    def of(
        cls,
        *,
        branch: str | None,
        pr_url: str | None,
        review_exchange_completed: bool,
        review_exchange_run: ReviewExchangeRunAssets | None,
        settlement: CompletionSettlement,
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
            settlement=settlement,
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
