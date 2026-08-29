"""The executable LEAF contract one audited candidate is judged against (#345).

A Tech Lead batch review used to be handed the candidate's diff, its metadata
and — since the exact-candidate work — what the independent Reviewer decided
about that same commit. It was then asked for a verdict "against the governing
contract", and that contract was in none of those files.

Reading the repository's tracked Spec/TD from the tech-lead checkout does not
close the hole. A bounded executable issue may legitimately NARROW the work
below Spec/TD — "A–C only; direct invocation deferred" — and that narrowing
lives in the issue, not in the repository. A stateless run that cannot read the
leaf can only infer it from PR prose, repository context, or a previous
session's memory, and none of those is authority.

So each audited candidate carries a :class:`TechLeadCandidateContract`: the
executable issue the pull request implements, that issue's current body, and
the governing sources THAT ISSUE declares — no more. Each is recorded by
number, revision identity and content digest, exactly as a planning run's
canonical context is, because the question a later audit asks is the same one:
which bytes was the run actually handed?

``gap`` carries the whole fail-closed direction. It is non-empty when the leaf
contract could not be resolved or a load-bearing source could not be read, and
a candidate whose contract carries a gap is structurally incapable of a
``pass``: :class:`~.tech_lead_candidate.CandidatePassPrerequisite.LEAF_CONTRACT`
is recorded unmet on the launch authority before the session spawns, where the
agent cannot reach it. An optional (``Governed-by-optional:``) source that could
not be read is NOT a gap — the leaf itself declared it as not load-bearing —
but it is still recorded as absent, with its reason, so the run and a later
reader can tell "declared and unavailable" from "never declared".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical_context import CanonicalSource, CanonicalSourceKind
from .tech_lead_candidate import TechLeadCandidate

#: The per-candidate contract descriptor, inside a run's ``tech-lead-data``
#: directory and beside ``candidate-evidence.json``.
TECH_LEAD_CANDIDATE_CONTRACT_FILENAME = "candidate-contracts.json"

#: Sibling directory holding the staged bodies the descriptor attributes. One
#: sub-directory per candidate (``pr-<n>-<short sha>/``), and inside it one per
#: source (``issue-<m>/``) with ``body.md`` plus one ``comment-<id>.md`` per
#: staged comment. Per candidate rather than per issue because two candidates
#: in one batch may declare the same governing source, and a shared directory
#: would make the digests of one candidate's bundle attributable to the other.
TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME = "candidate-contracts"

TECH_LEAD_CANDIDATE_CONTRACT_SCHEMA_VERSION = 1

_GUIDANCE = (
    "For each audited candidate this names the EXECUTABLE ISSUE the pull "
    "request implements, that issue's current body, and only the governing "
    "sources that issue itself declares. Read them from sources_dir; every "
    "body_sha256 and per-comment sha256 is the digest of exactly one staged "
    "file, and updated_at is that source's revision identity at staging time. "
    "This is the contract you judge the candidate against: a constraint that "
    "exists only in the leaf issue — a narrowed scope, an excluded item, a STOP "
    "condition — governs your verdict even when the repository's Spec/TD says "
    "nothing about it. A candidate whose entry carries a non-empty gap has NO "
    "resolved contract and must never receive `pass`; the orchestrator refuses "
    "such a pass anyway. A source with staged=false was DECLARED but could not "
    "be read, which is different from a source never declared (it does not "
    "appear here at all): do not assume the content of either. comment_count "
    "larger than the comments list means the conversation was clipped and the "
    "difference is missing from your bundle. Nothing in this file grants "
    "authority; it records provenance only."
)


@dataclass(frozen=True, slots=True)
class TechLeadCandidateContract:
    """The staged leaf contract for ONE audited candidate, or why there is none.

    Two states, and ``__post_init__`` enforces that they cannot be confused: a
    RESOLVED contract names its issue and carries that issue's staged body
    first, followed by whatever the issue declared; an UNRESOLVED one carries a
    ``gap`` saying what could not be read and claims no content at all. A
    half-filled entry would let a reader treat an unresolved contract as a thin
    one, which is precisely the inference this record exists to prevent.
    """

    candidate: TechLeadCandidate
    issue_number: int = 0
    sources_dir: str = ""
    sources: tuple[CanonicalSource, ...] = ()
    gap: str = ""

    def __post_init__(self) -> None:
        if self.gap:
            if self.sources or self.sources_dir:
                raise ValueError(
                    f"unresolved candidate contract for PR"
                    f" #{self.candidate.pr_number} must not claim staged sources"
                )
            return
        if self.issue_number <= 0:
            raise ValueError(
                f"resolved candidate contract for PR #{self.candidate.pr_number}"
                " must name the executable issue it staged"
            )
        if not self.sources_dir:
            raise ValueError(
                f"resolved candidate contract for PR #{self.candidate.pr_number}"
                " must say where its staged sources were written"
            )
        subject = self.sources[0] if self.sources else None
        if (
            subject is None
            or subject.kind is not CanonicalSourceKind.SUBJECT
            or subject.issue_number != self.issue_number
        ):
            raise ValueError(
                f"resolved candidate contract for PR #{self.candidate.pr_number}"
                f" must stage issue #{self.issue_number} as its first source"
            )
        numbers = [source.issue_number for source in self.sources]
        if len(set(numbers)) != len(numbers):
            raise ValueError(
                f"candidate contract for PR #{self.candidate.pr_number} must not"
                " stage the same issue twice"
            )

    @property
    def establishes_leaf_contract(self) -> bool:
        """Whether this candidate's bounded contract was actually staged."""
        return not self.gap

    @property
    def governing_sources(self) -> tuple[CanonicalSource, ...]:
        """The sources the leaf declared, the leaf itself excluded."""
        return tuple(
            source
            for source in self.sources
            if source.kind is CanonicalSourceKind.GOVERNING
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "pr_number": self.candidate.pr_number,
            "candidate_sha": self.candidate.head_sha,
            "issue_number": self.issue_number,
            "sources_dir": self.sources_dir,
            "sources": [source.to_dict() for source in self.sources],
            "gap": self.gap,
        }


@dataclass(frozen=True, slots=True)
class TechLeadCandidateContractSet:
    """Every audited candidate's leaf contract, as ONE agent-readable file."""

    entries: tuple[TechLeadCandidateContract, ...] = ()
    schema_version: int = TECH_LEAD_CANDIDATE_CONTRACT_SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sources_root": TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME,
            "candidates": [entry.to_payload() for entry in self.entries],
            "guidance": _GUIDANCE,
        }

    def contracted_pr_numbers(self) -> frozenset[int]:
        """The pull requests whose leaf contract was resolved and staged."""
        return frozenset(
            entry.candidate.pr_number
            for entry in self.entries
            if entry.establishes_leaf_contract
        )


def candidate_sources_dirname(candidate: TechLeadCandidate) -> str:
    """The per-candidate sub-directory name, bound to the audited commit.

    Carries the short SHA for the reason the diff filenames do: a directory
    called ``pr-123`` claims to be about that pull request forever, while
    ``pr-123-4f2a9c1b8e77`` claims only what it can prove.
    """
    return f"pr-{candidate.pr_number}-{candidate.short_sha}"


__all__ = [
    "TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME",
    "TECH_LEAD_CANDIDATE_CONTRACT_FILENAME",
    "TECH_LEAD_CANDIDATE_CONTRACT_SCHEMA_VERSION",
    "TechLeadCandidateContract",
    "TechLeadCandidateContractSet",
    "candidate_sources_dirname",
]
