"""Tech Lead manifest downloader - fetches PR data for tech_lead sessions.

Takes a TechLeadManifest and fetches the actual data (diffs, metadata)
from GitHub, writing files to the session directory.

Materialized content is BOUND to the manifest's candidate commit (#345). The
diff transport names a pull request, not a commit, so the binding is proved by
bracketing: the manifest recorded the head the orchestrator observed when it
selected the PR, and the metadata read that immediately follows the diff fetch
observes it again. Equal heads mean the bytes on disk are that candidate's;
unequal (or unreadable) means they are not, and then NO diff is written under
the candidate's name at all. A tech lead reading a diff for a commit that had
already moved is exactly the evidence this leaf refuses to manufacture, and the
completion-time re-read refuses the disposition for the same candidate anyway.

Nothing but a SUCCESSFUL read is ever written under a candidate's name (#359).
The diff comes from the repository host's own supported transport — the same
authenticated GitHub client every other read here uses — and its outcome is a
typed answer, not a body to inspect: readable bytes, or a reason there are
none. This module used to shell out to ``gh pr diff`` and write whatever came
back, so when the repository's direct-``gh`` guard refused the invocation, the
refusal text itself was filed as ``pr-<n>-<sha12>-diff.txt`` and the manifest
advertised it as the candidate's diff. A transport failure is an explicit
unavailable FACT, recorded on the manifest entry as
``diff_established=False`` with the reason observed, and carried from there
into the launch authority so a ``pass`` on that candidate is refused.

Filenames carry the candidate's short SHA for the same reason: a file called
``pr-123-diff.txt`` claims to be about PR #123 forever, while
``pr-123-a1b2c3d4e5f6-diff.txt`` claims only what it can prove.

Beside the per-candidate files it also stages ``candidate-evidence.json``: what
the independent Reviewer already decided about each candidate commit. That is
the prerequisite the merge contract assumes, and the Tech Lead's own
data-source contract forbids the agent from fetching it — so an unstaged
prerequisite would be an unprovable one. The POLICY of what counts as complete
evidence is not here; it arrives through
:class:`~..ports.tech_lead_candidate_evidence.TechLeadCandidateEvidenceSource`,
so this adapter never learns where the answer is stored. Writing the assembled
set to disk IS here (:func:`write_candidate_evidence`), for the same reason the
diffs and metadata are.

This is an adapter implementing the ManifestDownloader port.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..control.tech_lead_candidate_evidence import build_candidate_evidence
from ..domain.tech_lead_candidate import (
    TECH_LEAD_CANDIDATE_EVIDENCE_FILENAME,
    TechLeadCandidate,
    TechLeadCandidateEvidenceSet,
)
from ..domain.tech_lead_manifest import TechLeadManifest, PRFiles, PRToReview
from ..ports import RepositoryHost
from ..ports.tech_lead_candidate_evidence import (
    NO_TECH_LEAD_CANDIDATE_EVIDENCE,
    TechLeadCandidateEvidenceSource,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CandidateMaterialization:
    """What ONE candidate's staging pass actually produced (#359).

    The files that were written, and — when the candidate's own diff was not
    among them — the reason it was not. ``gap`` is the whole point: a
    materialization that produced no diff must say so in words the refusal
    receipt can print, long after this run's ``tech-lead-data`` directory is
    gone. An empty ``gap`` and an empty ``files.diff`` together are
    unrepresentable, which is what stops "we wrote nothing and recorded no
    reason" from reading as a met prerequisite.
    """

    files: PRFiles
    gap: str = ""

    def __post_init__(self) -> None:
        if bool(self.files.diff) == bool(self.gap):
            raise ValueError(
                "CandidateMaterialization either staged a diff or recorded why"
                f" it did not; got diff={self.files.diff!r}, gap={self.gap!r}"
            )

    @property
    def establishes_candidate_diff(self) -> bool:
        """Whether a merge-facing PASS may rest on this candidate's diff."""
        return not self.gap


def write_candidate_evidence(
    data_dir: Path, evidence: TechLeadCandidateEvidenceSet
) -> Path:
    """Write the staged evidence beside the manifest, and return its path.

    Beside the downloader rather than beside the policy that builds the set:
    this is ``path.write_text`` and nothing else, and materializing files into
    ``tech-lead-data`` is already this adapter's job.

    Fail-fast like the board snapshot and unlike the evidence map: the Tech
    Lead contract gate cannot render an exact-candidate verdict without this
    file, so a launch that cannot write it must fail rather than spawn a
    session that will be refused at completion for a reason it could not see.
    """
    path = data_dir / TECH_LEAD_CANDIDATE_EVIDENCE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence.to_payload(), indent=2) + "\n")
    logger.info(
        "[tech_lead] Staged exact-candidate review evidence for %d PR(s): %s",
        len(evidence.entries),
        path,
    )
    return path


class TechLeadDownloader:
    """Downloads PR data based on a tech_lead manifest.

    Implements ManifestDownloader port.

    Every external read goes through :class:`~..ports.RepositoryHost` — the
    metadata, and (since #359) the diff. There is deliberately no command
    runner here: a downloader that cannot spawn a subprocess cannot spawn a
    forbidden one, and cannot turn what a refused subprocess printed into
    candidate evidence.
    """

    def __init__(
        self,
        repository_host: RepositoryHost,
        candidate_evidence: TechLeadCandidateEvidenceSource = (
            NO_TECH_LEAD_CANDIDATE_EVIDENCE
        ),
    ):
        self._host = repository_host
        # Defaults to the explicitly-named "nothing was wired" source, which
        # stages that FACT for every candidate — refusing every merge-facing
        # PASS — instead of staging silence (#345).
        self._candidate_evidence = candidate_evidence

    def download(self, manifest: TechLeadManifest, worktree_path: Path) -> TechLeadManifest:
        """Fetch all PR data and update manifest with local file paths.

        Args:
            manifest: The manifest with PRs to fetch data for
            worktree_path: Path to the worktree where data should be written

        Returns:
            Updated manifest with file paths populated
        """
        if not manifest.data_dir:
            raise ValueError("Manifest data_dir must be set before downloading")

        data_path = worktree_path / manifest.data_dir
        data_path.mkdir(parents=True, exist_ok=True)

        for pr in manifest.prs:
            try:
                staged = self._download_pr_data(pr, data_path)
                if staged.establishes_candidate_diff:
                    # Only on the path that actually staged a diff: the refusal
                    # path logs its own reason at WARNING, and a run that says
                    # "downloaded" beside it reads as a success it was not.
                    logger.info(
                        "[tech_lead] Downloaded data for PR #%d @ %s",
                        pr.number,
                        pr.candidate().short_sha,
                    )
            except Exception as e:
                logger.warning("[tech_lead] Failed to download PR #%d: %s", pr.number, e)
                # Continue with other PRs even if one fails — and record the
                # failure AS the candidate's own, so an unstaged candidate is a
                # refused one rather than a silent one (#359). Isolation is the
                # point: this candidate's gap says nothing about its siblings,
                # and cannot erase what they staged.
                staged = CandidateMaterialization(
                    files=PRFiles(),
                    gap=(
                        f"staging PR #{pr.number} failed before its candidate"
                        f" diff could be materialized: {e}"
                    ),
                )
            pr.files = staged.files
            pr.diff_established = staged.establishes_candidate_diff
            pr.diff_gap = staged.gap

        evidence = build_candidate_evidence(
            manifest.prs, source=self._candidate_evidence, repository_host=self._host
        )
        write_candidate_evidence(data_path, evidence)
        # The orchestrator's own answer, copied onto the manifest entries so the
        # launch authority can carry it out of reach of the agent (#345) — and
        # with it the reason, because the file written above dies with this
        # session's worktree while the refusal receipt it explains is published
        # from the completion lane afterwards.
        # Zipped rather than looked up by number: ``build_candidate_evidence``
        # answers the entries it was given, in order and one for one, so a
        # mapping here would introduce a "not found" branch for a state that
        # cannot occur.
        for pr, answer in zip(manifest.prs, evidence.entries, strict=True):
            pr.review_established = answer.establishes_independent_review
            pr.review_gap = answer.gap
        return manifest

    def _download_pr_data(
        self, entry: PRToReview, data_path: Path
    ) -> CandidateMaterialization:
        """Materialize one candidate's diff and metadata.

        Two independent questions, asked in that order and both able to refuse:

        1. did the supported transport actually return a diff (#359), and
        2. is the pull request still at the commit the manifest bound (#345).

        A diff file is written, and the manifest names it, only when both
        answer yes. Either "no" produces no file at all and a recorded reason —
        both of them when both failed, because an operator reading the receipt
        has to know which conditions were observed, not merely that one was.
        """
        candidate = entry.candidate()
        stem = f"pr-{candidate.pr_number}-{candidate.short_sha}"
        diff_read = self._host.read_pr_diff(candidate.pr_number)

        # The closing half of the bracket, and the metadata read in one call:
        # what the head is NOW, right after the bytes above were produced.
        meta_filename = f"{stem}-meta.json"
        meta_path = data_path / meta_filename
        pr = self._host.get_pr(candidate.pr_number)
        binding = self._binding_detail(candidate, pr.head_sha if pr else None)
        gap = "; ".join(reason for reason in (diff_read.reason, binding) if reason)
        if pr:
            metadata: dict[str, object] = {
                "number": pr.number,
                "title": pr.title,
                "body": pr.body or "",
                "branch": pr.branch,
                "url": pr.url,
                "state": pr.state,
                "labels": pr.labels,
            }
        else:
            metadata = {"error": f"PR #{candidate.pr_number} not found"}
        metadata["candidate_sha"] = candidate.head_sha
        metadata["candidate_bound"] = not binding
        if binding:
            metadata["candidate_binding_gap"] = binding
        # The agent reads this file, so it must be able to see that a candidate
        # it was asked to audit has no code to audit — rather than inferring it
        # from a filename that is simply not there.
        metadata["diff_staged"] = not gap
        if gap:
            metadata["diff_gap"] = gap
        meta_path.write_text(json.dumps(metadata, indent=2))

        if gap:
            # Refusing to file anything under the candidate's name: an
            # unreadable diff is not a diff, and unbound bytes are evidence
            # about some other commit. Either way a review of what would be
            # written here could not be authority for this candidate.
            logger.warning(
                "[tech_lead] No diff staged for PR #%d @ %s: %s",
                candidate.pr_number,
                candidate.short_sha,
                gap,
            )
            return CandidateMaterialization(
                files=PRFiles(diff="", metadata=meta_filename), gap=gap
            )
        diff_filename = f"{stem}-diff.txt"
        (data_path / diff_filename).write_text(diff_read.diff)
        return CandidateMaterialization(
            files=PRFiles(diff=diff_filename, metadata=meta_filename)
        )

    @staticmethod
    def _binding_detail(
        candidate: TechLeadCandidate, observed_head: str | None
    ) -> str:
        """Why the fetched content is not this candidate's, or ``""``."""
        if not candidate.is_bound:
            return (
                f"PR #{candidate.pr_number} was selected without an observable"
                " head commit, so nothing can be materialized as its candidate"
            )
        if not observed_head:
            return (
                f"the head of PR #{candidate.pr_number} could not be re-read"
                " after the fetch, so the content cannot be bound to"
                f" {candidate.short_sha}"
            )
        if not candidate.covers(observed_head):
            return (
                f"PR #{candidate.pr_number} moved from {candidate.short_sha} to"
                f" {observed_head[:12]} during the fetch"
            )
        return ""
