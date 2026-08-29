"""Tech Lead manifest - defines what data to fetch for tech_lead sessions.

The orchestrator creates a manifest listing PRs to review, then a downloader
fetches the data (diffs, metadata) and writes it locally. The tech lead agent
reads the manifest to find its work.

Flow:
1. Orchestrator: build_tech_lead_manifest() -> TechLeadManifest
2. Downloader: download_manifest_data() -> writes files, updates manifest
3. Agent: reads manifest.json, reads local files, reports via coding-done
4. Orchestrator: adds tech-lead-reviewed label to all PRs in manifest
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from .tech_lead_candidate import (
    CandidatePassPrerequisite,
    CandidatePrerequisiteGap,
    TechLeadCandidate,
)

logger = logging.getLogger(__name__)


class PRFilesDict(TypedDict):
    """Serialized form of PRFiles."""

    diff: str
    metadata: str


class PRToReviewDict(TypedDict):
    """Serialized form of PRToReview."""

    number: int
    title: str
    url: str
    branch: str
    head_sha: str
    review_established: bool
    review_gap: str
    contract_established: bool
    contract_gap: str
    files: PRFilesDict


class TechLeadManifestDict(TypedDict):
    """Serialized form of TechLeadManifest."""

    session_type: str
    generated_at: str
    data_dir: str
    prs: list[PRToReviewDict]


@dataclass
class PRFiles:
    """Local file paths for a PR's data."""
    diff: str = ""  # Relative path to diff file
    metadata: str = ""  # Relative path to metadata JSON


@dataclass
class PRToReview:
    """A PR that needs tech_lead review.

    ``head_sha`` is the exact commit the orchestrator OBSERVED at this pull
    request's head when it selected the PR for review (#345). It is the
    candidate identity every merge-facing tech-lead fact is bound to; the
    branch name, the labels and the PR body are not. Empty means the head could
    not be read, which is carried rather than dropped so the audited set stays
    exactly the set that tripped the threshold — a candidate that names no
    commit simply cannot receive a merge-facing disposition.

    Note: Full PR metadata (additions, deletions, merged_at, etc.) is available
    in the metadata JSON file referenced by files.metadata.
    """
    number: int
    title: str
    url: str
    branch: str
    head_sha: str = ""
    # Whether an independent Reviewer's approval of THIS exact commit was
    # established when the session's inputs were staged (#345). Written by the
    # downloader from the durable candidate record, never by the agent, and
    # copied into the orchestrator-owned launch authority before the session
    # spawns — so completion can refuse a PASS on an unreviewed candidate
    # without re-reading anything the agent could have touched.
    review_established: bool = False
    # WHY it was not, in the staging owner's own words, when it was not. The
    # bool says a PASS may not rest on this candidate; this says which of the
    # several ways that happened — no verdict at all, one about another commit,
    # a rejection, an uncertified publication — so the refusal receipt the
    # operator acts on can name the condition that was actually observed. Empty
    # beside ``review_established=False`` means nothing was recorded, which the
    # receipt reports as the prerequisite's own fixed sentence rather than a
    # guess.
    review_gap: str = ""
    # Whether the bounded contract of the executable issue this pull request
    # implements — and the governing sources that issue itself declares — were
    # staged for this run (#345). Written by the leaf-contract staging owner,
    # never by the agent, and carried into the launch authority for the same
    # reason ``review_established`` is: a PASS on a candidate whose governing
    # contract nobody could resolve is authority for nothing.
    contract_established: bool = False
    # The leaf-contract half of the same answer: the staged contract's own
    # ``gap`` when there is none, recorded for the reason ``review_gap`` is.
    contract_gap: str = ""
    files: PRFiles = field(default_factory=PRFiles)

    def candidate(self) -> TechLeadCandidate:
        """This entry's exact-candidate identity (#345)."""
        return TechLeadCandidate(pr_number=self.number, head_sha=self.head_sha)

    def prerequisite_gaps(self) -> tuple[CandidatePrerequisiteGap, ...]:
        """The recorded reason for each prerequisite this entry does not hold.

        Derived from the two established/gap pairs together, so a reason can
        never be carried for a prerequisite the entry holds — the drift that
        would put a false cause on a pull request. A missing prerequisite with
        no recorded reason yields nothing here rather than an empty record: the
        gap record's contract is that it explains something.
        """
        return tuple(
            CandidatePrerequisiteGap(
                candidate=self.candidate(), prerequisite=prerequisite, reason=gap
            )
            for prerequisite, established, gap in (
                (
                    CandidatePassPrerequisite.INDEPENDENT_REVIEW,
                    self.review_established,
                    self.review_gap,
                ),
                (
                    CandidatePassPrerequisite.LEAF_CONTRACT,
                    self.contract_established,
                    self.contract_gap,
                ),
            )
            if not established and gap.strip()
        )


@dataclass
class TechLeadManifest:
    """Manifest for a tech_lead session.

    Created by orchestrator, populated by downloader, read by agent.
    """
    session_type: str = "tech_lead"
    generated_at: str = ""
    data_dir: str = ""  # Relative path from worktree root
    prs: list[PRToReview] = field(default_factory=list)

    def to_dict(self) -> TechLeadManifestDict:
        """Convert to JSON-serializable dict."""
        return {
            "session_type": self.session_type,
            "generated_at": self.generated_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "data_dir": self.data_dir,
            "prs": [
                {
                    "number": pr.number,
                    "title": pr.title,
                    "url": pr.url,
                    "branch": pr.branch,
                    "head_sha": pr.head_sha,
                    "review_established": pr.review_established,
                    "review_gap": pr.review_gap,
                    "contract_established": pr.contract_established,
                    "contract_gap": pr.contract_gap,
                    "files": {
                        "diff": pr.files.diff,
                        "metadata": pr.files.metadata,
                    }
                }
                for pr in self.prs
            ]
        }

    @classmethod
    def from_dict(cls, data: TechLeadManifestDict) -> "TechLeadManifest":
        """Load from dict."""
        prs = []
        for pr_data in data.get("prs", []):
            files_data = pr_data.get("files", {})
            prs.append(PRToReview(
                number=pr_data["number"],
                title=pr_data["title"],
                url=pr_data["url"],
                branch=pr_data["branch"],
                head_sha=pr_data.get("head_sha", ""),
                review_established=bool(pr_data.get("review_established", False)),
                review_gap=str(pr_data.get("review_gap", "")),
                contract_established=bool(
                    pr_data.get("contract_established", False)
                ),
                contract_gap=str(pr_data.get("contract_gap", "")),
                files=PRFiles(
                    diff=files_data.get("diff", ""),
                    metadata=files_data.get("metadata", ""),
                ),
            ))
        return cls(
            session_type=data.get("session_type", "tech_lead"),
            generated_at=data.get("generated_at", ""),
            data_dir=data.get("data_dir", ""),
            prs=prs,
        )

    def write(self, path: Path) -> None:
        """Write manifest to file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        logger.info("[tech_lead] Manifest written: %s (%d PRs)", path, len(self.prs))

    @classmethod
    def read(cls, path: Path) -> "TechLeadManifest":
        """Read manifest from file."""
        data = json.loads(path.read_text())
        return cls.from_dict(data)

    def get_pr_numbers(self) -> list[int]:
        """Get list of PR numbers for completion handling."""
        return [pr.number for pr in self.prs]

    def candidates(self) -> tuple[TechLeadCandidate, ...]:
        """Every audited PR bound to the head commit that was observed (#345)."""
        return tuple(pr.candidate() for pr in self.prs)

    def reviewed_candidates(self) -> tuple[TechLeadCandidate, ...]:
        """The candidates that arrived with an exact-commit reviewer approval."""
        return tuple(pr.candidate() for pr in self.prs if pr.review_established)

    def contracted_candidates(self) -> tuple[TechLeadCandidate, ...]:
        """The candidates whose executable-leaf contract was staged (#345)."""
        return tuple(pr.candidate() for pr in self.prs if pr.contract_established)

    def prerequisite_gaps(self) -> tuple[CandidatePrerequisiteGap, ...]:
        """Every recorded reason a candidate misses a merge prerequisite (#345).

        Carried into the launch authority beside the two subsets above, so the
        refusal receipt written at completion — long after this manifest's
        worktree is gone — can name the condition that was observed rather than
        the one the prerequisite is usually about.
        """
        return tuple(gap for pr in self.prs for gap in pr.prerequisite_gaps())
