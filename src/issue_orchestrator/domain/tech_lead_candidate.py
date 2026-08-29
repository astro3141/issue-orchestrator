"""The exact PR candidate a Tech Lead review is authority for (#345).

Before this module a batch review knew only PR *numbers*. A number names a
moving target: the head commit behind it can advance between the moment the
manifest was built and the moment the review's disposition is applied, and the
`tech-lead-reviewed` projection would follow it there. So every merge-facing
tech-lead fact is bound here to a :class:`TechLeadCandidate` — one pull request
AND the exact commit that was audited.

Four value objects, each with exactly one job:

* :class:`TechLeadCandidate` — the identity. ``pr_number`` plus the head SHA the
  orchestrator OBSERVED, never a branch name, a label, or the tech-lead scratch
  checkout's own ``launch_base_sha``.
* :class:`TechLeadCandidateEvidence` — what the independent Reviewer and the
  publication gate already decided about THAT commit, staged for the agent to
  read so the session never has to reconstruct it with a network call. Its
  :attr:`~TechLeadCandidateEvidence.gap` is what makes missing evidence loud:
  incomplete evidence is not permissive evidence.
* :class:`TechLeadCandidateVerdict` — the per-candidate disposition the tech
  lead renders (PASS / REWORK / HUMAN_A). Per candidate, not per session: a
  multi-PR batch may not transfer one candidate's answer to another.
* :class:`CandidateStanding` — what a completion-time re-read of the live head
  found. A verdict is authority only while the candidate it names is still the
  candidate.
* :class:`CandidateOutcome` — what the run actually concluded about the
  candidate once standing and the review prerequisite are folded into the
  disposition. This is the value the watch-set owner keys its label effects on,
  so "what happened" and "which labels say so" stay one decision apart.

``head_sha`` may be the empty string, and that is a real state rather than a
default: it means the orchestrator never observed a usable commit for this pull
request. An unbound candidate can never be PASSed — the fail-closed direction —
but it is still carried in the manifest so the set the session audits stays
exactly the set that tripped the threshold (#6768's lesson).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any, cast

from .commit_sha import normalize_commit_sha

#: Where the staged per-candidate reviewer/publication evidence is written,
#: inside the run's ``tech-lead-data`` directory. One owner for the name, asked
#: by the launcher that writes it and by every test that reads it back.
TECH_LEAD_CANDIDATE_EVIDENCE_FILENAME = "candidate-evidence.json"

TECH_LEAD_CANDIDATE_SCHEMA_VERSION = 1

#: Bound on the agent-authored rationale a candidate verdict carries. The
#: decision file is untrusted input, so this is a contract violation rather
#: than something to truncate — same stance as every other artifact bound.
MAX_CANDIDATE_RATIONALE_CHARS = 20_000


def normalize_candidate_sha(value: object, *, field_name: str = "head_sha") -> str:
    """A candidate's commit, canonicalised — or ``""`` for "never observed".

    The empty string is admitted here and nowhere else in the SHA vocabulary:
    :func:`~.commit_sha.normalize_commit_sha` rejects it, correctly, because a
    record that cannot name its commit is not evidence. A *manifest entry*,
    though, must be able to say "this pull request is in the batch and its head
    could not be read", or the batch set and the threshold set would diverge.
    Every merge-facing reader asks :attr:`TechLeadCandidate.is_bound` before it
    treats the value as an identity.
    """
    if isinstance(value, str) and not value.strip():
        return ""
    return normalize_commit_sha(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class TechLeadCandidate:
    """One pull request bound to the exact commit a tech lead audited."""

    pr_number: int
    head_sha: str = ""

    def __post_init__(self) -> None:
        number: Any = self.pr_number
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError(
                f"TechLeadCandidate pr_number must be a positive int, got {number!r}"
            )
        object.__setattr__(self, "head_sha", normalize_candidate_sha(self.head_sha))

    @property
    def is_bound(self) -> bool:
        """Whether this candidate names an exact observed commit."""
        return bool(self.head_sha)

    def covers(self, head_sha: str | None) -> bool:
        """Whether ``head_sha`` is this candidate's own commit.

        An unbound candidate covers nothing, and an unreadable observation
        covers nothing either: both answer False rather than raising, because
        the caller's question is "may this disposition still apply", and the
        answer to that is no in both directions.
        """
        if not self.is_bound:
            return False
        try:
            return normalize_commit_sha(head_sha, field_name="head_sha") == self.head_sha
        except (TypeError, ValueError):
            return False

    @property
    def short_sha(self) -> str:
        """The candidate's commit as a human writes it, or ``"unknown"``."""
        return self.head_sha[:12] if self.is_bound else "unknown"

    def to_payload(self) -> dict[str, Any]:
        return {"pr_number": self.pr_number, "head_sha": self.head_sha}

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "TechLeadCandidate":
        """Parse a stored candidate; every malformed shape raises ValueError."""
        raw_number = payload.get("pr_number")
        if isinstance(raw_number, bool) or not isinstance(raw_number, int):
            raise ValueError(
                f"tech_lead candidate pr_number must be an int, got {raw_number!r}"
            )
        raw_sha = payload.get("head_sha", "")
        if not isinstance(raw_sha, str):
            raise ValueError(
                f"tech_lead candidate head_sha must be a string, got {raw_sha!r}"
            )
        return cls(pr_number=raw_number, head_sha=raw_sha)


class CandidateStanding(Enum):
    """What a completion-time re-read found the candidate's live head to be.

    ``permits_authority`` is a property of the member rather than a set held
    beside the enum, for the reason :class:`~.continuation_phase.
    ContinuationPhase` does the same: a new standing forces its author to say
    whether a candidate holding it may still receive a merge-facing effect.
    """

    #: The live head is still the audited commit. Only this permits authority.
    CURRENT = ("current", True)
    #: The live head is a different commit: the review is about other work.
    MOVED = ("moved", False)
    #: The live head could not be read. Unknown is not unchanged.
    UNREADABLE = ("unreadable", False)
    #: The manifest never bound this pull request to an observed commit.
    UNBOUND = ("unbound", False)

    def __init__(self, value: str, permits_authority: bool) -> None:
        self._value_ = value
        self._permits_authority = permits_authority

    @property
    def permits_authority(self) -> bool:
        """Whether a disposition may still be applied to this candidate."""
        return self._permits_authority


class TechLeadCandidateDisposition(StrEnum):
    """The per-candidate answer a Tech Lead contract review renders.

    Fixed semantics (#335, via #345):

    * ``PASS`` — the candidate conforms to the governing contract; the merge
      gate may consume this verdict. Informational findings may coexist with it.
    * ``REWORK`` — a bounded implementation/process defect inside already
      settled Spec/TD/policy. No human authority decision is required.
    * ``HUMAN_A`` — a genuinely new Spec/TD/policy/authority decision is
      required. STOP: neither merge nor ordinary rework authority follows.
    """

    PASS = "pass"
    REWORK = "rework"
    HUMAN_A = "human_a"

    @property
    def projects_reviewed_label(self) -> bool:
        """Whether this disposition may project the merge-facing label."""
        return self is TechLeadCandidateDisposition.PASS


class CandidateOutcome(Enum):
    """What ONE batch run actually concluded about ONE candidate.

    The disposition is what the tech lead *rendered*; this is what the
    orchestrator *concluded* after folding in the two facts the agent cannot
    be trusted for — whether the candidate is still the candidate, and whether
    an independent reviewer approved that exact commit. Every merge-facing
    effect and every watch-set label effect keys on this, so "a PASS the
    orchestrator refused" can never be spelled the same way as a PASS.

    :attr:`settles_membership` is a property of the member for the same reason
    :attr:`CandidateStanding.permits_authority` is: adding an outcome forces
    its author to answer whether a candidate holding it is still awaiting a
    tech-lead answer. Exactly one member answers "yes" — the one where the run
    could not audit the candidate at all — and that keep is deliberate: a
    candidate whose head moved must be re-audited at what it now proposes.
    """

    #: PASS on a still-current, independently reviewed candidate. Merge-facing.
    AUTHORITY = ("authority", True)
    #: REWORK: a bounded defect. Back to the ordinary rework lane.
    REWORK = ("rework", True)
    #: HUMAN_A: stop. A new Spec/TD/policy decision is required.
    HUMAN = ("human", True)
    #: A disposition the orchestrator refused for want of an exact-candidate
    #: reviewer approval. The run answered, and the answer is "not on this
    #: batch's authority".
    UNSETTLED = ("unsettled", True)
    #: The run could not audit this candidate (moved, unreadable or unbound
    #: head), so it concluded nothing about it and must see it again.
    DEFERRED = ("deferred", False)

    def __init__(self, value: str, settles_membership: bool) -> None:
        self._value_ = value
        self._settles_membership = settles_membership

    @property
    def settles_membership(self) -> bool:
        """Whether this outcome takes the candidate out of the watch set."""
        return self._settles_membership

    @classmethod
    def resolve(
        cls,
        *,
        disposition: "TechLeadCandidateDisposition | None",
        standing: CandidateStanding,
        review_established: bool,
    ) -> "CandidateOutcome":
        """Fold one candidate's three facts into the run's conclusion.

        Order is the contract. Standing is asked first because a verdict about
        a commit that is no longer proposed is not a verdict about anything
        this pull request currently holds — including a PASS whose reviewer
        approval WAS established, since that approval is about the same
        superseded commit. A missing disposition lands here too: a run that
        rendered nothing for a candidate concluded nothing about it.
        """
        if disposition is None or not standing.permits_authority:
            return cls.DEFERRED
        if disposition is TechLeadCandidateDisposition.PASS:
            return cls.AUTHORITY if review_established else cls.UNSETTLED
        if disposition is TechLeadCandidateDisposition.REWORK:
            return cls.REWORK
        return cls.HUMAN


@dataclass(frozen=True, slots=True)
class TechLeadCandidateVerdict:
    """One candidate's disposition, as the tech lead rendered it.

    ``rationale`` is required for every disposition and not merely for the two
    that route somewhere: a PASS whose reason nobody recorded is a projection
    with no receipt, which is the thing #345 exists to remove. For REWORK it is
    the actionable, candidate-bound feedback #295 requires *before* any
    ``needs-rework`` projection; for HUMAN_A it is the decision question.
    """

    candidate: TechLeadCandidate
    disposition: TechLeadCandidateDisposition
    rationale: str
    finding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Runtime re-check: ``from_mapping`` feeds this dataclass untrusted
        # agent JSON, and a declared annotation carries no runtime guarantee.
        # Cast through ``object`` so the check is real rather than narrowed
        # away by the declared type (the pattern ``StoredTechLeadOp`` uses).
        disposition = cast(object, self.disposition)
        if not isinstance(disposition, TechLeadCandidateDisposition):
            raise ValueError(
                "TechLeadCandidateVerdict disposition must be a"
                f" TechLeadCandidateDisposition, got {disposition!r}"
            )
        rationale: Any = self.rationale
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(
                f"candidate verdict for PR #{self.candidate.pr_number} requires a"
                " non-empty rationale (the PASS reason, the actionable rework"
                " feedback, or the human decision question)"
            )
        if len(rationale) > MAX_CANDIDATE_RATIONALE_CHARS:
            raise ValueError(
                f"candidate verdict for PR #{self.candidate.pr_number} rationale"
                f" exceeds {MAX_CANDIDATE_RATIONALE_CHARS} characters"
                f" ({len(rationale)})"
            )
        findings: Any = self.finding_ids
        if not isinstance(findings, tuple) or any(
            not isinstance(item, str) or not item.strip() for item in findings
        ):
            raise ValueError(
                f"candidate verdict for PR #{self.candidate.pr_number} finding_ids"
                f" must be a tuple of non-empty strings, got {findings!r}"
            )
        object.__setattr__(self, "rationale", rationale.strip())

    @property
    def pr_number(self) -> int:
        return self.candidate.pr_number

    @classmethod
    def from_mapping(cls, data: Any, *, index: int) -> "TechLeadCandidateVerdict":
        """Parse one agent-authored verdict; malformed content raises ValueError."""
        if not isinstance(data, dict):
            raise ValueError(
                f"candidate verdict #{index} must be an object, got"
                f" {type(data).__name__}"
            )
        raw_number = data.get("pr_number")
        if isinstance(raw_number, bool) or not isinstance(raw_number, int):
            raise ValueError(
                f"candidate verdict #{index} requires an int pr_number, got"
                f" {raw_number!r}"
            )
        raw_sha = data.get("candidate_sha")
        if not isinstance(raw_sha, str) or not raw_sha.strip():
            raise ValueError(
                f"candidate verdict #{index} (PR #{raw_number}) requires the"
                " candidate_sha it is a verdict about; a verdict that does not"
                " name its commit is authority for nothing"
            )
        raw_disposition = data.get("disposition")
        try:
            disposition = TechLeadCandidateDisposition(raw_disposition)
        except ValueError:
            raise ValueError(
                f"candidate verdict #{index} (PR #{raw_number}) has unknown"
                f" disposition {raw_disposition!r} (expected one of"
                f" {[member.value for member in TechLeadCandidateDisposition]})"
            ) from None
        rationale = data.get("rationale")
        raw_findings = data.get("finding_ids", [])
        if not isinstance(raw_findings, list) or any(
            not isinstance(item, str) for item in raw_findings
        ):
            raise ValueError(
                f"candidate verdict #{index} (PR #{raw_number}) finding_ids must"
                f" be a list of strings, got {raw_findings!r}"
            )
        return cls(
            candidate=TechLeadCandidate(
                pr_number=raw_number,
                head_sha=normalize_commit_sha(raw_sha, field_name="candidate_sha"),
            ),
            disposition=disposition,
            rationale=rationale if isinstance(rationale, str) else "",
            finding_ids=tuple(item.strip() for item in raw_findings if item.strip()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_number": self.candidate.pr_number,
            "candidate_sha": self.candidate.head_sha,
            "disposition": self.disposition.value,
            "rationale": self.rationale,
            "finding_ids": list(self.finding_ids),
        }


@dataclass(frozen=True, slots=True)
class TechLeadCandidateEvidence:
    """The exact-candidate evidence staged for ONE manifest pull request.

    Everything here was decided by the orchestrator about the *candidate*, not
    about the pull request: the independent Reviewer's verdict and the commit
    it was rendered against, and whether that same commit cleared the
    publication contract. Nothing is agent-authored, and nothing is inferred
    from a PR-level label — ``code-reviewed`` outlives the candidate that
    earned it, which is exactly why it is not evidence.

    ``gap`` is non-empty when the evidence does not establish an independent
    reviewer verdict for this exact commit. It is the whole reason this record
    exists rather than a bare optional verdict: "no attempt record", "the
    verdict is about another commit" and "the reviewer requested changes" are
    three different facts, and a reader that saw only ``None`` would treat them
    alike.
    """

    candidate: TechLeadCandidate
    issue_number: int = 0
    reviewer_verdict: str = ""
    reviewed_sha: str = ""
    decided_at: str = ""
    completed_rounds: int = 0
    reviewer_principal: str = ""
    actor_principal: str = ""
    publication_certified: bool = False
    publication_reason: str = ""
    gap: str = ""

    @property
    def establishes_independent_review(self) -> bool:
        """Whether an exact-candidate reviewer approval is proven here."""
        return not self.gap

    def to_payload(self) -> dict[str, Any]:
        return {
            "pr_number": self.candidate.pr_number,
            "candidate_sha": self.candidate.head_sha,
            "issue_number": self.issue_number,
            "reviewer_verdict": self.reviewer_verdict,
            "reviewed_sha": self.reviewed_sha,
            "decided_at": self.decided_at,
            "completed_rounds": self.completed_rounds,
            "reviewer_principal": self.reviewer_principal,
            "actor_principal": self.actor_principal,
            "publication_certified": self.publication_certified,
            "publication_reason": self.publication_reason,
            "gap": self.gap,
        }


@dataclass(frozen=True, slots=True)
class TechLeadCandidateEvidenceSet:
    """Every manifest candidate's staged evidence, as ONE agent-readable file."""

    entries: tuple[TechLeadCandidateEvidence, ...] = ()
    schema_version: int = TECH_LEAD_CANDIDATE_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidates": [entry.to_payload() for entry in self.entries],
        }


__all__ = [
    "MAX_CANDIDATE_RATIONALE_CHARS",
    "TECH_LEAD_CANDIDATE_EVIDENCE_FILENAME",
    "TECH_LEAD_CANDIDATE_SCHEMA_VERSION",
    "CandidateOutcome",
    "CandidateStanding",
    "TechLeadCandidate",
    "TechLeadCandidateDisposition",
    "TechLeadCandidateEvidence",
    "TechLeadCandidateEvidenceSet",
    "TechLeadCandidateVerdict",
    "normalize_candidate_sha",
]
