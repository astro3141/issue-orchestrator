"""Tech Lead manifest builder - creates manifests for tech_lead sessions.

Queries GitHub to find PRs that need tech_lead (currently OPEN, carrying the
tech_lead watch label but not tech-lead-reviewed or tech-lead-failed). Every
part of that question — which label selects a pull request, which lifecycle
states a candidate may be in, and whether a selected pull request is still a
candidate — comes from :class:`~.tech_lead_candidate_policy.
TechLeadCandidatePolicy`, the single owner shared with threshold fact gathering
and with the completion-time disposition planner, so the PR set that trips the
threshold is exactly the set the session audits and exactly the set the review
settles.

That the observation happens HERE and not at threshold time is the point of
re-making it (#352): a candidate that merged since it was counted is gone from
this manifest, and so cannot be audited or receive a disposition.
"""

import logging
import time

from ..domain.tech_lead_manifest import TechLeadManifest, PRToReview, PRFiles
from ..ports import RepositoryHost
from .tech_lead_candidate_policy import TechLeadCandidatePolicy

logger = logging.getLogger(__name__)


class TechLeadManifestBuilder:
    """Builds tech_lead manifests by querying for PRs that need review."""

    def __init__(
        self,
        repository_host: RepositoryHost,
        *,
        candidate_policy: TechLeadCandidatePolicy,
    ):
        self._host = repository_host
        # The selection is the POLICY's, not a second parameter beside it: a
        # builder that could be handed a watch label — or a pull-request
        # lifecycle scope — the policy does not know about is a builder that
        # can audit a set nobody counted.
        self._policy = candidate_policy

    def build(self, data_dir: str) -> TechLeadManifest:
        """Build a tech_lead manifest with PRs that need review.

        Args:
            data_dir: Relative path from worktree root where data files will go

        Returns:
            TechLeadManifest with PRs to review (data not yet downloaded)
        """
        # One call to the shared candidate owner: it asks GitHub for the open
        # pull requests carrying the watch label and drops the ones it no
        # longer considers candidates (already-triaged PRs, PRs that reached a
        # terminal lifecycle state since the threshold counted them and, on
        # filtered runs, PRs outside the filter label scope).
        prs = self._policy.open_candidates(self._host)
        logger.info(
            "[tech_lead] %d open PR(s) with '%s' need tech_lead review",
            len(prs), self._policy.watch_label
        )

        prs_to_review = [
            PRToReview(
                number=pr.number,
                title=pr.title,
                url=pr.url,
                branch=pr.branch,
                # The candidate identity, read from the SAME observation that
                # selected the PR (#345). An unreadable head stays "" — the PR
                # is still audited, it simply cannot receive a merge-facing
                # disposition later — rather than dropping out of the manifest
                # and re-opening the threshold/manifest divergence #6768 closed.
                head_sha=pr.head_sha or "",
                files=PRFiles(),
            )
            for pr in prs
        ]

        return TechLeadManifest(
            session_type="tech_lead",
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            data_dir=data_dir,
            prs=prs_to_review,
        )
