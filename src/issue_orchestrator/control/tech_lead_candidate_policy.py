"""Who is in the Tech Lead watch set, and what takes them back out (#345).

The threshold trigger and the manifest builder already shared ONE owner for the
question "is this watch-labelled pull request still a batch candidate", because
answering it twice is how the count that fires a batch and the set that batch
audits drift apart (#6768 round 5). That owner only ever answered the ENTRY
half.

The exit half had no owner at all, and it showed. Before #345 a landed batch
review projected ``tech-lead-reviewed`` onto every manifest pull request — wrong
as merge authority, but it was also the only thing that ever removed a pull
request from the watch set. Replacing it with a per-candidate disposition left
the exit rule scattered: partly spelled by hand in the disposition planner,
partly in the failure projection, and for a candidate the tech lead stopped or
the orchestrator refused, nowhere — so that candidate kept counting toward the
threshold and an identical batch re-fired over identical evidence, forever.

So both halves live here, on one type:

* :meth:`TechLeadCandidatePolicy.is_candidate` — entry, unchanged;
* :meth:`TechLeadCandidatePolicy.settle` — exit, one
  :class:`CandidateWatchExit` per :class:`~..domain.tech_lead_candidate.
  CandidateOutcome`.

The invariant they jointly hold is: **the set that trips the threshold is
exactly the set a review settles.** Two consequences are worth stating outright.

The watch label comes from ``Config.tech_lead_watch_label`` in BOTH directions.
It has a single owner (:func:`~..infra.config_value_rules.
resolve_tech_lead_watch_label`) precisely because a locally-derived spelling is
how the two sets diverge: a repository that sets ``tech_lead_review_label``
without changing ``code_reviewed_label`` watches one label while a hand-written
removal clears another, and the "sent back for work" candidate re-trips the
threshold it just left. ``review_approved_label`` is removed BESIDE it on a
rework — a review that no longer stands should not keep saying it does — and
the two collapse to one action in the default configuration where they are the
same label.

Only one outcome deliberately keeps membership: a candidate the run could not
audit (its head moved, could not be read, or was never observed) concluded
nothing and must be seen again at whatever it now proposes. Every outcome the
run COULD reach leaves the set, including the two that produce no merge
authority — ``tech-lead-failed`` there means "this batch produced no tech-lead
authority for this candidate", which is the same thing it means when a session
dies, and it is the label
:meth:`~TechLeadCandidatePolicy.is_candidate` already reads as terminal.

The two ways out are not the same door, and :attr:`CandidateWatchExit.
readmission` is where that is said out loud. Clearing the watch label is
reversible by the ordinary review lane, which re-adds it on the next approval.
Adding ``tech_lead_failed_label`` is NOT: nothing in this codebase removes it,
so a stopped or refused candidate stays out until an operator takes it off.
That was already true of the whole-session failure projection; what this module
adds is candidates reaching it one at a time, which is exactly why every such
receipt has to name the label and the manual step rather than leave the reader
to discover the door only swings one way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from ..domain.tech_lead_candidate import CandidateOutcome

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..infra.config import Config


@dataclass(frozen=True, slots=True)
class CandidateWatchExit:
    """The label effects that settle ONE candidate's watch-set membership.

    Label NAMES rather than actions: the planner owns the reason strings, the
    ordering its own contracts require (#295 puts rework feedback before any
    projection), and the ``ExpectedState`` every action carries. What it does
    not own — and could not own without re-deriving the watch label — is which
    labels say "this candidate is settled".

    ``readmission`` travels with them because leaving the set and getting back
    into it are one rule, and only this owner knows both halves. The two exits
    are not symmetric: clearing the watch label is undone by the next review
    that approves the pull request, while adding a terminal label is a ONE-WAY
    door — nothing in this codebase removes ``tech_lead_failed_label``, so an
    operator has to. A receipt that did not say which of those happened would
    leave the reader to guess whether anything will ever pick the pull request
    up again.
    """

    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    keeps_membership: bool = False
    readmission: str = ""


@dataclass(frozen=True)
class TechLeadCandidatePolicy:
    """Single owner of tech_lead batch watch-set membership, both directions.

    Threshold fact gathering (``FactGatherer._fetch_tech_lead_prs``), manifest
    construction (:class:`~.tech_lead_manifest_builder.TechLeadManifestBuilder`)
    and the completion-time disposition planner
    (``tech_lead_candidate_disposition``) all go through this one type, so the
    count that trips a batch, the set the session audits and the set the review
    settles cannot disagree.
    """

    #: The label that SELECTS pull requests into the batch. Always
    #: ``Config.tech_lead_watch_label`` in production; see the module docstring.
    watch_label: str = "code-reviewed"
    #: The marker an independent review left on the pull request, as the
    #: orchestrator actually applies it (prefix-resolved). Cleared on a rework.
    review_approved_label: str = "code-reviewed"
    tech_lead_reviewed_label: str = "tech-lead-reviewed"
    tech_lead_failed_label: str = "tech-lead-failed"
    required_label: str | None = None

    @classmethod
    def from_config(cls, config: "Config") -> "TechLeadCandidatePolicy":
        """Derive the policy from configuration (custom labels + filter scope)."""
        from .label_manager import LabelManager

        reviewed, failed = cls.terminal_labels_for(config)
        return cls(
            watch_label=config.tech_lead_watch_label,
            # Resolved through the label registry rather than read raw: this is
            # the marker an approval actually WROTE, and under a configured
            # ``label_prefix`` the raw spelling is not it.
            review_approved_label=LabelManager(config).code_reviewed,
            tech_lead_reviewed_label=reviewed,
            tech_lead_failed_label=failed,
            required_label=config.filtering.label,
        )

    @classmethod
    def terminal_labels_for(cls, config: "Config") -> tuple[str, str]:
        """The terminal label pair, for callers that need only that.

        The deferred-cleanup gate asks this and nothing else, so it is spelled
        without building a whole policy — but it is spelled HERE, once, and
        :meth:`from_config` reads it too, so the pair cannot acquire a second
        derivation the way the watch label once did.
        """
        return (
            config.tech_lead_reviewed_label or "tech-lead-reviewed",
            config.tech_lead_failed_label or "tech-lead-failed",
        )

    @property
    def terminal_labels(self) -> tuple[str, ...]:
        """The labels that mean "tech_lead has settled this pull request".

        Both of them, and read by everything that waits on a settled tech-lead
        answer — :meth:`is_candidate` and the deferred-cleanup gate. A gate that
        waited on the merge-facing label alone would wait forever on every
        candidate a batch stopped or refused.
        """
        return (self.tech_lead_reviewed_label, self.tech_lead_failed_label)

    def is_candidate(self, labels: Sequence[str]) -> bool:
        """True when a watch-labelled PR still needs a tech_lead batch review."""
        label_set = set(labels)
        terminalized = bool(set(self.terminal_labels) & label_set)
        in_scope = self.required_label is None or self.required_label in label_set
        return not terminalized and in_scope

    def settle(self, outcome: CandidateOutcome) -> CandidateWatchExit:
        """The watch-set label effects for one concluded candidate.

        Exhaustive over :class:`CandidateOutcome` by construction: the mapping
        below has an entry per member and this raises on a member it has no
        answer for, so a new outcome cannot silently inherit "leave the
        candidate where it is" — which is the failure mode this module exists
        to close.
        """
        if outcome not in _WATCH_EXITS:  # pragma: no cover - defensive
            raise ValueError(
                f"no tech_lead watch-set exit is defined for outcome {outcome!r}"
            )
        exit_rule = _WATCH_EXITS[outcome]
        return exit_rule(self)


def _authority_exit(policy: TechLeadCandidatePolicy) -> CandidateWatchExit:
    return CandidateWatchExit(add=(policy.tech_lead_reviewed_label,))


def _rework_exit(policy: TechLeadCandidatePolicy) -> CandidateWatchExit:
    # Ordered dedup: the default configuration spells both of these the same
    # way, and asking twice to remove one label is noise on the pull request.
    removed = dict.fromkeys((policy.watch_label, policy.review_approved_label))
    return CandidateWatchExit(
        remove=tuple(removed),
        readmission=(
            f"`{policy.watch_label}` has been cleared, so this pull request has"
            " left the Tech Lead batch set. It re-enters automatically when a"
            " review approves it again."
        ),
    )


def _no_authority_exit(policy: TechLeadCandidatePolicy) -> CandidateWatchExit:
    return CandidateWatchExit(
        add=(policy.tech_lead_failed_label,),
        readmission=(
            f"`{policy.tech_lead_failed_label}` has been applied, so this pull"
            " request has left the Tech Lead batch set. Nothing removes that"
            " label automatically: once the condition above is resolved, an"
            " operator must remove it to re-admit the pull request to batch"
            " review."
        ),
    )


def _deferred_exit(_policy: TechLeadCandidatePolicy) -> CandidateWatchExit:
    return CandidateWatchExit(keeps_membership=True)


_WATCH_EXITS = {
    CandidateOutcome.AUTHORITY: _authority_exit,
    CandidateOutcome.REWORK: _rework_exit,
    # A stop and a refusal differ in what they SAY — the receipts are not the
    # same sentence — but not in what they leave behind: neither produced
    # tech-lead authority for this candidate, and neither may leave it counting
    # toward the threshold that would re-run this identical audit.
    CandidateOutcome.HUMAN: _no_authority_exit,
    CandidateOutcome.UNSETTLED: _no_authority_exit,
    CandidateOutcome.DEFERRED: _deferred_exit,
}


__all__ = ["CandidateWatchExit", "TechLeadCandidatePolicy"]
